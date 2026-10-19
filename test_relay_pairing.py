import asyncio
import base64
import hashlib
import json
import os
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
import relay_server as relay
from security_test_support import MAC, OTHER_MAC, TOKEN, host_hello, client_hello, auth_headers
from test_relay_database import DatabaseFixture


class PairingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.json'
        for p in (patch.object(relay, 'STATE_FILE', str(self.path)),
                  patch.object(relay, 'DASHBOARD_PASSWORD', 'dashboard-password-123'),
                  patch.object(relay, 'machines', {}), patch.object(relay, 'sessions', {}),
                  patch.object(relay, 'invites', {}),
                  patch.object(relay, 'host_ctrl', {}),
                  patch.dict(os.environ, {'ALLOW_INSECURE_LOCALHOST': '1', 'DATABASE_URL': ''})):
            p.start()
            self.addCleanup(p.stop)
        relay.load_state()
        self.client = TestClient(TestServer(relay.make_app()))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.headers = {'Authorization': 'Basic ' + base64.b64encode(
            b'admin:dashboard-password-123').decode(), 'X-Requested-With': 'RemoteDesktop'}

    async def invite(self):
        response = await self.client.post('/api/invite', json={}, headers=self.headers)
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data['expires_in'], 900)
        self.assertRegex(data['pairing_code'], r'^[0-9a-f]{32}$')
        return data['pairing_code']

    async def pair(self, code, **changes):
        data = dict(pairing_code=code, mac=MAC, name='Office', host_token=TOKEN)
        data.update(changes)
        return await self.client.post('/api/pair', json=data)

    async def use_database(self):
        self.db = DatabaseFixture()
        self.db.payload = relay.state_snapshot(relay.machines, relay.invites)
        p = patch('psycopg.connect', side_effect=self.db.connect)
        p.start()
        self.addCleanup(p.stop)
        store = relay.PostgresState('postgresql://test@localhost/test')
        await store.open()
        self.client.app[relay.SECURITY]['store'] = store
        return store

    async def test_pair_restart_retry_and_authenticated_connection(self):
        invite = await self.invite()
        response = await self.pair(invite)
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertEqual(result['ok'], True)
        self.assertRegex(result['code'], r'^[0-9]{6}$')
        stored = self.path.read_text()
        self.assertNotIn(invite, stored)
        self.assertNotIn(TOKEN, stored)
        relay.load_state()
        self.assertEqual(await (await self.pair(invite, mac=MAC.lower())).json(), result)
        host = await self.client.ws_connect('/ws', headers=auth_headers(host_hello(code=result['code'])))
        self.addAsyncCleanup(host.close)
        self.assertEqual((await host.receive_json())['code'], result['code'])
        operator = await self.client.ws_connect('/ws', headers=auth_headers(client_hello(result['code'])))
        self.addAsyncCleanup(operator.close)
        self.assertEqual((await operator.receive_json())['type'], 'connected')

    async def test_replay_and_existing_enrollment_cannot_replace_identity(self):
        invite = await self.invite()
        self.assertEqual((await self.pair(invite)).status, 200)
        before = relay.machines[MAC].copy()
        for changes in ({'mac': OTHER_MAC}, {'host_token': 'cd' * 32}):
            self.assertEqual((await self.pair(invite, **changes)).status, 401)
        fresh = await self.invite()
        self.assertEqual((await self.pair(fresh)).status, 409)
        self.assertEqual(relay.machines[MAC], before)
        self.assertEqual((await self.pair(fresh, mac=OTHER_MAC)).status, 200)

    async def test_expiry_including_consumed_retry(self):
        now = time.time()
        with patch.object(relay.time, 'time', return_value=now):
            unused, used = await self.invite(), await self.invite()
            self.assertEqual((await self.pair(used)).status, 200)
        with patch.object(relay.time, 'time', return_value=now + 900):
            self.assertEqual((await self.pair(unused, mac=OTHER_MAC)).status, 401)
            self.assertEqual((await self.pair(used)).status, 401)

    async def test_concurrent_redeemers_only_one_wins(self):
        invite = await self.invite()
        responses = await asyncio.gather(self.pair(invite), self.pair(invite, mac=OTHER_MAC))
        self.assertEqual(sorted(r.status for r in responses), [200, 401])
        self.assertEqual(len(relay.machines), 1)

    async def test_invite_auth_csrf_tls_and_pair_validation(self):
        self.assertEqual((await self.client.post('/api/invite', json={})).status, 401)
        self.assertEqual((await self.client.post('/api/invite', json={}, headers={
            'Authorization': self.headers['Authorization']})).status, 403)
        invite = await self.invite()
        for changes in ({'pairing_code': []}, {'host_token': 'a'}, {'mac': 'invalid'},
                        {'name': []}, {'name': 'x' * 81}):
            self.assertEqual((await self.pair(invite, **changes)).status, 400)
        with patch.dict(os.environ, {'ALLOW_INSECURE_LOCALHOST': '0'}):
            self.assertEqual((await self.pair(invite)).status, 403)
        self.assertFalse(relay.machines)

    async def test_write_failure_does_not_consume_invite(self):
        invite = await self.invite()
        with patch.object(relay.os, 'replace', side_effect=OSError('disk unavailable')):
            self.assertEqual((await self.pair(invite)).status, 503)
        self.assertFalse(relay.machines)
        self.assertEqual((await self.pair(invite)).status, 200)

    async def test_invite_capacity_prunes_only_expired_entries(self):
        self.assertTrue(hasattr(relay, 'MAX_INVITES'))
        with patch.object(relay, 'MAX_INVITES', 2):
            now = time.time()
            with patch.object(relay.time, 'time', return_value=now):
                await self.invite()
                await self.invite()
                response = await self.client.post('/api/invite', json={}, headers=self.headers)
                self.assertEqual(response.status, 503)
            with patch.object(relay.time, 'time', return_value=now + 901):
                await self.invite()

    async def test_rotation_invalidates_idempotent_pair_retry(self):
        invite = await self.invite()
        self.assertEqual((await self.pair(invite)).status, 200)
        response = await self.client.post('/api/enroll', json={'mac': MAC}, headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await self.pair(invite)).status, 401)

    async def test_database_outage_revokes_sessions_and_rejects_later_requests(self):
        invite = await self.invite()
        result = await (await self.pair(invite)).json()
        host = await self.client.ws_connect('/ws', headers=auth_headers(host_hello(code=result['code'])))
        self.addAsyncCleanup(host.close)
        await host.receive_json()
        await self.use_database()
        self.db.error = OSError('database unavailable')
        response = await self.client.post('/api/enroll', json={'mac': MAC}, headers=self.headers)
        self.assertEqual(response.status, 503)
        self.assertFalse(relay.sessions)
        self.assertFalse(relay.host_ctrl)
        self.assertEqual((await self.pair(invite)).status, 503)
        self.assertEqual((await self.client.get('/api/machines', headers=self.headers)).status, 503)

    async def test_async_rotation_cannot_publish_stale_handshake(self):
        invite = await self.invite()
        result = await (await self.pair(invite)).json()
        await self.use_database()
        entered, release = threading.Event(), threading.Event()
        connect = self.db.connect
        def slow(**options):
            entered.set()
            if not release.wait(3):
                raise OSError('test release timed out')
            return connect(**options)
        with patch('psycopg.connect', side_effect=slow):
            rotation = asyncio.create_task(self.client.post('/api/enroll', json={'mac': MAC}, headers=self.headers))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                host = await self.client.ws_connect('/ws', headers=auth_headers(host_hello(code=result['code'])))
                self.addAsyncCleanup(host.close)
            finally:
                release.set()
            self.assertEqual((await rotation).status, 200)
            self.assertEqual((await host.receive_json(timeout=3))['type'], 'error')
        self.assertFalse(relay.sessions)
        self.assertFalse(relay.host_ctrl)

    async def test_unchanged_reconnect_and_dashboard_poll_do_not_query_database(self):
        invite = await self.invite()
        result = await (await self.pair(invite)).json()
        await self.use_database()
        calls = len(self.db.calls)
        for _ in range(3):
            host = await self.client.ws_connect('/ws', headers=auth_headers(host_hello(code=result['code'])))
            self.assertEqual((await host.receive_json())['code'], result['code'])
            await host.close()
            self.assertEqual((await self.client.get('/api/machines', headers=self.headers)).status, 200)
        self.assertEqual(len(self.db.calls), calls)

    async def test_health_is_private_data_free_and_does_not_poll_database(self):
        await self.use_database()
        calls = len(self.db.calls)
        response = await self.client.get('/healthz')
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.text(), 'ok')
        self.client.app[relay.SECURITY]['state_failed'] = True
        self.assertEqual((await self.client.get('/healthz')).status, 503)
        self.assertEqual(len(self.db.calls), calls)

    async def test_health_requires_tls_or_trusted_proxy(self):
        with patch.dict(os.environ, {'ALLOW_INSECURE_LOCALHOST': '0', 'TRUST_PROXY': '1'}):
            self.assertEqual((await self.client.get('/healthz')).status, 403)
            self.assertEqual((await self.client.get('/healthz', headers={'X-Forwarded-Proto': 'https'})).status, 200)

    async def test_slow_pair_body_does_not_block_host_registration(self):
        invite = await self.invite()
        code = (await (await self.pair(invite)).json())['code']
        entered, release = asyncio.Event(), asyncio.Event()
        original_json = relay.web.Request.json
        async def observed_json(request, *args, **kwargs):
            if request.path == '/api/pair':
                entered.set()
            return await original_json(request, *args, **kwargs)
        async def body():
            yield b'{'
            await release.wait()
            yield b'}'
        with patch.object(relay.web.Request, 'json', observed_json), \
                patch.object(relay, 'MAX_PAIR_READS_PER_IP', 1):
            pending = asyncio.create_task(self.client.post('/api/pair', data=body()))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertEqual((await self.pair(invite)).status, 429)
                self.assertFalse(self.client.app[relay.SECURITY]['mutations'])
                host = await self.client.ws_connect('/ws', headers=auth_headers(host_hello(code=code)))
                self.addAsyncCleanup(host.close)
                self.assertEqual((await host.receive_json(timeout=1))['code'], code)
            finally:
                release.set()
                await pending
        self.assertEqual(self.client.app[relay.SECURITY]['pair_reads'], 0)
        self.assertFalse(self.client.app[relay.SECURITY]['pair_peers'])

    async def test_registry_capacity_does_not_poison_database_or_consume_invite(self):
        invite = await self.invite()
        code = (await (await self.pair(invite)).json())['code']
        fresh = await self.invite()
        host = await self.client.ws_connect('/ws', headers=auth_headers(host_hello(code=code)))
        self.addAsyncCleanup(host.close)
        await host.receive_json()
        await self.use_database()
        calls = len(self.db.calls)
        with patch.object(relay, 'MAX_MACHINES', 1):
            for path, payload in [('/api/pair', dict(pairing_code=fresh, mac=OTHER_MAC, name='Other', host_token=TOKEN)),
                                  ('/api/enroll', {'mac': OTHER_MAC})]:
                response = await self.client.post(path, json=payload, headers=self.headers)
                self.assertEqual(response.status, 503)
                self.assertEqual((await self.client.get('/healthz')).status, 200)
                self.assertIn(code, relay.sessions)
        self.assertEqual(len(self.db.calls), calls)
        self.assertEqual((await self.pair(fresh, mac=OTHER_MAC)).status, 200)

    async def test_candidate_validation_is_not_a_database_failure(self):
        await self.use_database()
        calls = len(self.db.calls)
        with self.assertRaises(relay.web.HTTPBadRequest):
            await relay.persist(self.client.app, {MAC: {'code': 'invalid'}})
        self.assertEqual((await self.client.get('/healthz')).status, 200)
        self.assertEqual(len(self.db.calls), calls)


if __name__ == '__main__':
    unittest.main()
