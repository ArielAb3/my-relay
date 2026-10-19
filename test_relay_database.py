import json
import os
import tempfile
import unittest
import asyncio
import copy
import threading
import certifi
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import relay_server as relay
from security_test_support import MAC, OTHER_MAC, machine


class DatabaseFixture:
    """libpq boundary double; exercises the real adapter and SQL/CAS arguments."""
    def __init__(self):
        self.payload = {'schema': 1, 'machines': {}, 'invites': {}}
        self.revision = 0
        self.calls = []
        self.connections = []
        self.error = None
        self.table_exists = True
        self.row_exists = True
        self.setup_error = None

    def connect(self, **options):
        self.calls.append((threading.get_ident(), options))
        if self.error:
            raise self.error
        database = self
        class Connection:
            pgconn = SimpleNamespace(ssl_in_use=True)
            closed = False
            def __init__(self):
                self.statements = []
            def close(self):
                self.closed = True
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.close()
            def execute(self, sql, params=None):
                self.statements.append(sql)
                if sql.startswith('SET LOCAL '):
                    if database.setup_error:
                        raise database.setup_error
                    row = None
                elif sql.startswith('SELECT to_regclass'):
                    row = ('remote_desktop_state' if database.table_exists else None,)
                elif sql.startswith('SELECT revision, payload'):
                    row = (database.revision, copy.deepcopy(database.payload)) if database.row_exists else None
                elif sql.startswith('CREATE TABLE public.remote_desktop_state'):
                    database.table_exists = True
                    row = None
                elif sql.startswith('INSERT INTO public.remote_desktop_state'):
                    database.row_exists = True
                    database.payload = copy.deepcopy(params[0].obj)
                    row = None
                elif sql.startswith('UPDATE public.remote_desktop_state'):
                    payload, expected = params
                    if expected != database.revision:
                        row = None
                    else:
                        database.revision += 1
                        database.payload = copy.deepcopy(payload.obj)
                        row = (database.revision,)
                else:
                    raise AssertionError('Unexpected SQL')
                return SimpleNamespace(fetchone=lambda: row)
        connection = Connection()
        self.connections.append(connection)
        return connection


class DatabaseAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = DatabaseFixture()
        p = patch('psycopg.connect', side_effect=self.db.connect)
        p.start()
        self.addCleanup(p.stop)
        self.store = relay.PostgresState('postgresql://test@localhost/test')
        self.addAsyncCleanup(self.store.close)

    async def test_tls_timeouts_worker_and_unchanged_snapshot_no_queries(self):
        snapshot = await self.store.open()
        self.assertTrue(all(c.closed for c in self.db.connections))
        self.assertNotEqual(self.db.calls[0][0], threading.get_ident())
        options = self.db.calls[0][1]
        self.assertEqual(options['sslmode'], 'verify-full')
        self.assertEqual(options['sslrootcert'], certifi.where())
        self.assertEqual(options['channel_binding'], 'require')
        self.assertEqual(options['connect_timeout'], 5)
        self.assertNotIn('options', options)
        await self.store.save(snapshot)
        self.assertEqual(len(self.db.calls), 1)
        await self.store.save({'schema': 1, 'machines': {MAC: machine()}, 'invites': {}})
        self.assertEqual(self.db.payload['machines'][MAC]['desired_code'], '654321')
        self.assertEqual(self.db.revision, 1)
        self.assertTrue(all(c.closed for c in self.db.connections))
        for connection in self.db.connections:
            self.assertEqual(connection.statements[:2], [
                "SET LOCAL statement_timeout = '5s'", "SET LOCAL lock_timeout = '2s'"])

    async def test_timeout_setup_failure_closes_connection_and_poisoned_store(self):
        await self.store.open()
        self.db.setup_error = OSError('transaction setup failed')
        with self.assertRaises(OSError):
            await self.store.save({'schema': 1, 'machines': {MAC: machine()}, 'invites': {}})
        self.assertTrue(self.store.failed)
        self.assertTrue(self.db.connections[-1].closed)
        self.assertFalse(self.db.payload['machines'])

    async def test_url_startup_options_are_removed_for_pooler(self):
        store = relay.PostgresState('postgresql://test@localhost/test?options=-c%20statement_timeout%3D5000')
        self.addAsyncCleanup(store.close)
        await store.open()
        self.assertNotIn('options', self.db.calls[-1][1])

    async def test_conflict_poisoned_writer_never_overwrites_or_retries(self):
        await self.store.open()
        self.db.revision = 7
        with self.assertRaises(OSError):
            await self.store.save({'schema': 1, 'machines': {MAC: machine()}, 'invites': {}})
        self.assertFalse(self.db.payload['machines'])
        calls = len(self.db.calls)
        with self.assertRaises(OSError):
            await self.store.save(self.db.payload)
        self.assertEqual(len(self.db.calls), calls)

    async def test_runtime_metadata_cannot_mutate_cached_durable_snapshot(self):
        self.db.payload['machines'][MAC] = machine()
        loaded = await self.store.open()
        loaded['machines'][MAC]['online'] = True
        loaded['machines'][MAC]['last_seen'] = 123
        await self.store.save(self.db.payload)
        self.assertEqual(len(self.db.calls), 1)

    async def test_corrupt_database_does_not_initialize_empty_state(self):
        self.db.payload = {'machines': [], 'invites': {}}
        with self.assertRaises(OSError):
            await self.store.open()
        self.assertEqual(self.db.payload, {'machines': [], 'invites': {}})

    async def test_normal_startup_never_bootstraps_missing_database(self):
        self.db.table_exists = False
        with self.assertRaises(OSError):
            await self.store.open()
        self.assertFalse(self.db.table_exists)

    async def test_explicit_bootstrap_creates_initial_row(self):
        self.assertIn('initialize', __import__('inspect').signature(self.store.open).parameters)
        self.db.table_exists = self.db.row_exists = False
        self.assertEqual(await self.store.open(initialize=True),
                         {'schema': 1, 'machines': {}, 'invites': {}})
        self.assertTrue(self.db.table_exists)
        self.assertTrue(self.db.row_exists)

    async def test_missing_row_fails_closed_even_during_bootstrap(self):
        self.assertIn('initialize', __import__('inspect').signature(self.store.open).parameters)
        self.db.row_exists = False
        with self.assertRaises(OSError):
            await self.store.open(initialize=True)
        self.assertFalse(self.db.row_exists)

    async def test_connection_outage_fails_closed(self):
        await self.store.open()
        self.db.error = OSError('offline')
        with self.assertRaises(OSError):
            await self.store.save({'schema': 1, 'machines': {MAC: machine()}, 'invites': {}})
        self.assertTrue(self.store.failed)
        self.assertFalse(self.db.payload['machines'])

    async def test_startup_diagnostics_include_phase_without_driver_secrets(self):
        self.db.error = OSError('postgresql://user:secret@private-host/db')
        with self.assertRaises(OSError) as caught:
            await self.store.open()
        self.assertIn('phase=connect', str(caught.exception))
        self.assertIn('category=connection', str(caught.exception))
        self.assertNotIn('secret', str(caught.exception))
        self.assertNotIn('private-host', str(caught.exception))

    async def test_invalid_state_diagnostic_identifies_validation_phase(self):
        self.db.payload = {'machines': []}
        with self.assertRaises(OSError) as caught:
            await self.store.open()
        self.assertIn('phase=validate-state', str(caught.exception))
        self.assertIn('category=validation', str(caught.exception))

    async def test_timeout_is_bounded_and_late_result_cannot_reenable_store(self):
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            release.wait(2)
        try:
            with patch.object(relay, 'DB_TIMEOUT', .05):
                with self.assertRaises(TimeoutError):
                    await self.store.run(slow)
            self.assertTrue(entered.is_set())
            self.assertTrue(self.store.failed)
        finally:
            release.set()

    async def test_insecure_database_url_rejected_before_connection(self):
        store = relay.PostgresState('postgresql://test@localhost/test?sslmode=disable')
        with self.assertRaises(OSError):
            await store.open()
        self.assertFalse(self.db.calls)

    async def test_encryption_only_url_is_upgraded_to_verified_tls(self):
        store = relay.PostgresState('postgresql://test@localhost/test?sslmode=require')
        self.addAsyncCleanup(store.close)
        await store.open()
        self.assertEqual(self.db.calls[-1][1]['sslmode'], 'verify-full')
        self.assertEqual(self.db.calls[-1][1]['sslrootcert'], certifi.where())


class StateValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.json'
        p = patch.object(relay, 'STATE_FILE', str(self.path))
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(relay, 'machines', {})
        p.start()
        self.addCleanup(p.stop)

    def test_legacy_state_remains_readable(self):
        self.path.write_text(json.dumps({'machines': {MAC: machine()}}))
        relay.load_state()
        self.assertEqual(relay.machines[MAC]['desired_code'], '654321')

    def test_invalid_envelopes_fail_without_resetting_memory(self):
        invalid = [
            {'schema': 99, 'machines': {}, 'invites': {}},
            {'schema': 1, 'machines': {}, 'invites': []},
            {'schema': 1, 'machines': {}, 'invites': {'a' * 64: {'expires_at': 'tomorrow'}}},
            {'machines': {MAC: machine(), OTHER_MAC: machine()}},
            {'machines': {MAC: {'name': 'x' * 81}}},
            {'schema': 1, 'machines': {}},
            {'machines': {}, 'invites': {}},
            {'schema': 1, 'machines': {}, 'invites': {'a' * 64: {'expires_at': float('nan')}}},
            {'schema': 1, 'machines': {}, 'invites': {'a' * 64: {
                'expires_at': 123, 'mac': MAC, 'code': '123456', 'host_token_hash': 'ab' * 32}}},
        ]
        for data in invalid:
            with self.subTest(data=data):
                relay.machines[MAC] = machine()
                self.path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    relay.load_state()
                self.assertIn(MAC, relay.machines)

    def test_unreadable_state_never_resets(self):
        relay.machines[MAC] = machine()
        with patch('builtins.open', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                relay.load_state()
        self.assertIn(MAC, relay.machines)


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'),
                     'PostgreSQL integration not run: TEST_DATABASE_URL is unset (use a disposable database)')
class PostgresIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_revision_conflict_and_durable_roundtrip(self):
        self.assertTrue(hasattr(relay, 'PostgresState'))
        store = relay.PostgresState(os.environ['TEST_DATABASE_URL'])
        original = await store.open(initialize=True)
        try:
            other = relay.PostgresState(os.environ['TEST_DATABASE_URL'])
            await other.open()
            snapshot = {'schema': 1, 'machines': {MAC: machine()}, 'invites': {}}
            await store.save(snapshot)
            try:
                with self.assertRaises(Exception):
                    await other.save({'schema': 1, 'machines': {}, 'invites': {'ab' * 32: {'expires_at': 9999999999}}})
            finally:
                await other.close()
            await store.close()
            store = relay.PostgresState(os.environ['TEST_DATABASE_URL'])
            self.assertEqual(await store.open(), snapshot)
        finally:
            await store.save(original)
            await store.close()


if __name__ == '__main__':
    unittest.main()
