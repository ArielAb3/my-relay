import asyncio
import base64
import hashlib
import json
import os
import struct
import threading
import tempfile
import unittest
from unittest.mock import patch, Mock

from aiohttp import WSMsgType, WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer
import relay_server as relay

MAC = 'AA:BB:CC:DD:EE:FF'
PASSWORD = 'operator-password-123'
SALT = '12' * 16
HASH = hashlib.pbkdf2_hmac('sha256', PASSWORD.encode(), bytes.fromhex(SALT), 600000, 32).hex()


class RelayAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(relay, 'STATE_FILE', os.path.join(self.tmp.name, 'state.json')),
                        patch.object(relay, 'DASHBOARD_PASSWORD', 'dashboard-password-123'),
                        patch.dict(os.environ, {'ALLOW_INSECURE_LOCALHOST': '1', 'TRUST_PROXY': '0'})]
        for p in self.patches:
            p.start()
        relay.machines.clear()
        relay.sessions.clear()
        relay.host_ctrl.clear()
        self.client = TestClient(TestServer(relay.make_app()))
        await self.client.start_server()
        self.sockets = []
        self.auth = {'Authorization': 'Basic ' + base64.b64encode(
            ('admin:' + relay.DASHBOARD_PASSWORD).encode()).decode()}

    async def asyncTearDown(self):
        for ws in self.sockets:
            await ws.close()
        await self.client.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    async def post(self, path, data, header=True):
        return await self.client.post(path, json=data,
            headers={**self.auth, **({'X-Requested-With': 'RemoteDesktop'} if header else {})})

    async def enroll(self):
        r = await self.post('/api/enroll', {'mac': MAC})
        self.assertEqual(r.status, 200)
        return (await r.json())['host_token']

    async def ws(self, msg):
        header = base64.b64encode(json.dumps(msg).encode()).decode()
        try:
            ws = await self.client.ws_connect('/ws', headers={'X-RemoteDesktop-Auth': header})
        except WSServerHandshakeError as exc:
            return None, {'type': 'error', 'status': exc.status}
        self.sockets.append(ws)
        return ws, await ws.receive_json(timeout=3)

    def host_msg(self, token, **changes):
        return dict(role='host', protocol=2, mac=MAC, host_token=token,
                    password_salt=SALT, password_hash=HASH, allow_files=False,
                    preferred_code='123456', **changes)

    async def test_old_host_rejected(self):
        _, reply = await self.ws({'role': 'host', 'mac': MAC})
        self.assertEqual(reply['type'], 'error')
        self.assertFalse(relay.sessions)

    async def test_dashboard_polling_cannot_block_desktop_login(self):
        token = await self.enroll()
        with patch.object(relay, 'IP_ATTEMPTS', 3):
            for _ in range(2):
                self.assertEqual((await self.client.get('/api/machines', headers=self.auth)).status, 200)
            _, result = await self.ws(self.host_msg(token))
            self.assertEqual(result['type'], 'code')
            _, result = await self.ws(dict(role='client', protocol=2, code=result['code'], password=PASSWORD))
            self.assertEqual(result['type'], 'connected')

    async def test_stale_local_code_uses_own_assignment_and_preserves_other_machine(self):
        token = await self.enroll()
        other_mac = 'AA:BB:CC:DD:EE:02'
        await self.post('/api/enroll', {'mac': other_mac})
        await self.post('/api/set', {'mac': other_mac, 'code': '123456'})
        _, first = await self.ws(self.host_msg(token))
        self.assertEqual(first['type'], 'code')
        self.assertNotEqual(first['code'], '123456')
        _, second = await self.ws(self.host_msg(token))
        self.assertEqual(second['code'], first['code'])
        self.assertEqual(relay.machines[other_mac]['desired_code'], '123456')

    async def test_dashboard_distinguishes_available_from_in_use(self):
        token = await self.enroll()
        await self.ws(self.host_msg(token))
        async def in_use():
            response = await self.client.get('/api/machines', headers=self.auth)
            return (await response.json())['machines'][0].get('in_use')
        self.assertIs(await in_use(), False)
        operator, _ = await self.ws(dict(role='client', protocol=2, code='123456', password=PASSWORD))
        self.assertIs(await in_use(), True)
        host_socket = self.sockets[0]
        self.assertEqual((await host_socket.receive_json(timeout=2))['type'], 'connected')
        response = await self.post('/api/set', {'mac': MAC, 'code': '123456'})
        self.assertEqual(response.status, 200)
        await operator.close()
        self.assertEqual((await host_socket.receive_json(timeout=2))['type'], 'disconnected')
        self.assertIs(await in_use(), False)

    async def test_header_auth_rejects_before_upgrade(self):
        for header in [None, '!!!', base64.b64encode(b'x'*4097).decode(),
                       base64.b64encode(b'[]').decode()]:
            r = await self.client.get('/ws', headers={} if header is None else {'X-RemoteDesktop-Auth': header})
            self.assertEqual(r.status, 401)
            self.assertEqual(await r.text(), 'Authentication failed')
        self.assertFalse(relay.sessions)

    async def test_invalid_credentials_never_prepare_websocket(self):
        token = await self.enroll()
        msg = self.host_msg(token)
        msg['host_token'] = '00' * 32
        with patch.object(relay.web.WebSocketResponse, 'prepare') as prepare:
            _, reply = await self.ws(msg)
            self.assertEqual(reply['status'], 401)
            prepare.assert_not_called()
        self.assertEqual(self.client.server.app[relay.SECURITY]['ws_count'], 0)
        self.assertFalse(self.client.server.app[relay.SECURITY]['ws_peers'])

    async def test_header_attempt_and_peer_capacity_status(self):
        with patch.object(relay, 'IP_ATTEMPTS', 1):
            r = await self.client.get('/ws')
            self.assertEqual(r.status, 401)
            r = await self.client.get('/ws')
            self.assertEqual(r.status, 429)
        with patch.object(relay, 'MAX_WS_PER_IP', 0):
            r = await self.client.get('/ws')
            self.assertEqual(r.status, 503)

    async def test_upgrade_race_does_not_commit_revoked_host(self):
        token = await self.enroll()
        entered, release = asyncio.Event(), asyncio.Event()
        original = relay.web.WebSocketResponse.prepare
        async def paused(ws, request):
            result = await original(ws, request)
            if not entered.is_set():
                entered.set()
                await release.wait()
            return result
        with patch.object(relay.web.WebSocketResponse, 'prepare', paused):
            task = asyncio.create_task(self.ws(self.host_msg(token)))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await self.enroll()
            finally:
                release.set()
            _, reply = await task
        self.assertEqual(reply['type'], 'error')
        self.assertFalse(relay.sessions)
        self.assertFalse(relay.host_ctrl)

    async def test_upgrade_race_respects_new_offline_machine_reservation(self):
        token = await self.enroll()
        other_mac = 'AA:BB:CC:DD:EE:02'
        r = await self.post('/api/enroll', {'mac': other_mac})
        self.assertEqual(r.status, 200)
        entered, release = asyncio.Event(), asyncio.Event()
        original = relay.web.WebSocketResponse.prepare
        async def paused(ws, request):
            result = await original(ws, request)
            if not entered.is_set():
                entered.set()
                await release.wait()
            return result
        with patch.object(relay.web.WebSocketResponse, 'prepare', paused):
            task = asyncio.create_task(self.ws(self.host_msg(token)))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                r = await self.post('/api/set', {'mac': other_mac, 'code': '123456'})
                self.assertEqual(r.status, 200)
            finally:
                release.set()
            _, reply = await task
        self.assertEqual(reply['type'], 'error')
        self.assertFalse(relay.sessions)
        self.assertFalse(relay.host_ctrl)
        self.assertEqual(relay.machines[other_mac]['desired_code'], '123456')
        self.assertEqual(relay.machines[MAC]['code'], '')
        with open(relay.STATE_FILE) as f:
            stored = json.load(f)['machines']
        self.assertEqual(stored[other_mac]['desired_code'], '123456')
        self.assertEqual(stored[MAC]['code'], '')

    async def test_upgrade_race_does_not_attach_client_to_replaced_session(self):
        token = await self.enroll()
        await self.ws(self.host_msg(token))
        entered, release = asyncio.Event(), asyncio.Event()
        original = relay.web.WebSocketResponse.prepare
        async def paused(ws, request):
            result = await original(ws, request)
            if not entered.is_set():
                entered.set()
                await release.wait()
            return result
        with patch.object(relay.web.WebSocketResponse, 'prepare', paused):
            task = asyncio.create_task(self.ws(dict(role='client', protocol=2, code='123456', password=PASSWORD)))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await self.ws(self.host_msg(token))
            finally:
                release.set()
            _, reply = await task
        self.assertEqual(reply['type'], 'error')
        self.assertIsNone(relay.sessions['123456']['client'])

    async def test_post_replace_failure_reconciles_and_disconnects(self):
        for failure in ('open', 'fsync'):
            token = await self.enroll()
            h, _ = await self.ws(self.host_msg(token))
            c, _ = await self.ws(dict(role='client', protocol=2, code='123456', password=PASSWORD))
            await h.receive_json(timeout=2)
            directory = os.path.dirname(relay.STATE_FILE)
            real_open, real_fsync = os.open, os.fsync
            directory_fds = set()
            def directory_open(path, flags, *args, **kwargs):
                if path == directory:
                    if failure == 'open':
                        raise OSError('directory open failed after replace')
                    fd = real_open(os.devnull, os.O_RDONLY)
                    directory_fds.add(fd)
                    return fd
                return real_open(path, flags, *args, **kwargs)
            def directory_sync(fd):
                if fd in directory_fds:
                    raise OSError('directory fsync failed after replace')
                return real_fsync(fd)
            fake_os = Mock(wraps=os)
            fake_os.name, fake_os.O_RDONLY, fake_os.O_DIRECTORY = 'posix', os.O_RDONLY, 0
            fake_os.open.side_effect = directory_open
            fake_os.fsync.side_effect = directory_sync
            with patch.object(relay, 'os', fake_os):
                r = await self.post('/api/enroll', {'mac': MAC})
            self.assertEqual(r.status, 503)
            self.assertNotIn('host_token', await r.text())
            with open(relay.STATE_FILE) as f:
                stored = json.load(f)['machines'][MAC]
            self.assertNotEqual(stored['host_token_hash'], hashlib.sha256(token.encode()).hexdigest())
            self.assertEqual(relay.machines[MAC]['host_token_hash'], stored['host_token_hash'])
            self.assertFalse(relay.sessions)
            self.assertFalse(relay.host_ctrl)
            self.assertEqual((await h.receive(timeout=2)).type, WSMsgType.CLOSE)
            self.assertEqual((await c.receive(timeout=2)).type, WSMsgType.CLOSE)
            _, rejected = await self.ws(self.host_msg(token))
            self.assertEqual(rejected['status'], 401)

    async def test_enroll_auth_csrf_and_token_storage(self):
        r = await self.post('/api/enroll', {'mac': MAC}, header=False)
        self.assertEqual(r.status, 403)
        token = await self.enroll()
        self.assertEqual(len(token), 64)
        with open(relay.STATE_FILE) as f:
            stored = f.read()
        self.assertNotIn(token, stored)
        self.assertIn(hashlib.sha256(token.encode()).hexdigest(), stored)
        r = await self.client.get('/api/machines', headers=self.auth)
        self.assertNotIn('token', await r.text())

    async def test_enroll_rejects_non_mac(self):
        for value in ['unknown', 'AA-BB-CC-DD-EE-FF', 'AA:BB:CC:DD:EE:GG', ['bad']]:
            r = await self.post('/api/enroll', {'mac': value})
            self.assertEqual(r.status, 400)

    async def test_protocol_password_and_control_injection(self):
        token = await self.enroll()
        h, code = await self.ws(self.host_msg(token))
        self.assertEqual(code, {'type': 'code', 'code': '123456', 'protocol': 2})
        for msg in [{'role': 'client', 'code': '123456'},
                    {'role': 'client', 'protocol': 2, 'code': '123456', 'password': 'wrong-password-123'}]:
            _, result = await self.ws(msg)
            self.assertEqual(result['type'], 'error')
        c, result = await self.ws(dict(role='client', protocol=2, code='123456', password=PASSWORD))
        self.assertEqual(result, {'type': 'connected', 'protocol': 2, 'allow_files': False})
        self.assertEqual(await h.receive_json(timeout=2), result)
        await c.send_json({'type': 'set_code', 'code': '654321'})
        frame = struct.pack('>BI', 2, 10) + bytes(10)
        await c.send_bytes(frame)
        self.assertEqual((await h.receive(timeout=2)).data, frame)

    async def test_client_frame_allowlist(self):
        token = await self.enroll()
        h, _ = await self.ws(self.host_msg(token))
        c, _ = await self.ws(dict(role='client', protocol=2, code='123456', password=PASSWORD))
        await h.receive_json(timeout=2)
        for kind in (1, 5, 6, 7, 8, 255):
            await c.send_bytes(struct.pack('>BI', kind, 0))
        await c.send_bytes(b'\x02\x00')
        good = struct.pack('>BI', 3, 2) + b'\x01a'
        await c.send_bytes(good)
        self.assertEqual((await h.receive(timeout=2)).data, good)

    async def test_password_result_cannot_attach_to_replacement(self):
        token = await self.enroll()
        h, _ = await self.ws(self.host_msg(token))
        entered, release = threading.Event(), threading.Event()
        original = hashlib.pbkdf2_hmac
        def slow(*args):
            entered.set()
            release.wait(5)
            return original(*args)
        try:
            with patch.object(relay.hashlib, 'pbkdf2_hmac', slow):
                task = asyncio.create_task(self.ws(dict(role='client', protocol=2, code='123456', password=PASSWORD)))
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set())
                replacement, _ = await self.ws(self.host_msg(token))
                release.set()
                _, reply = await task
                self.assertEqual(reply['type'], 'error')
                self.assertIsNone(relay.sessions['123456']['client'])
                self.assertFalse(replacement.closed)
        finally:
            release.set()

    async def test_auth_limits_include_success_and_reset_per_app(self):
        with patch.object(relay, 'IP_ATTEMPTS', 2):
            for _ in range(2):
                r = await self.client.get('/api/machines', headers=self.auth)
                self.assertEqual(r.status, 200)
            r = await self.client.get('/api/machines', headers=self.auth)
            self.assertEqual(r.status, 429)
            self.assertEqual(r.headers['Retry-After'], '60')
            fresh = relay.make_app()
            self.assertTrue(relay.attempt(fresh, '127.0.0.1'))
            fresh[relay.SECURITY]['executor'].shutdown()

    async def test_overload_rejects_without_hash_queue(self):
        token = await self.enroll()
        await self.ws(self.host_msg(token))
        with patch.object(relay, 'MAX_HASHES', 0):
            _, reply = await self.ws(dict(role='client', protocol=2, code='123456', password=PASSWORD))
            self.assertEqual(reply['type'], 'error')
            self.assertEqual(reply['status'], 503)
        with patch.object(relay, 'MAX_WS', 0):
            r = await self.client.get('/ws')
            self.assertEqual(r.status, 503)

    async def test_strict_host_fields(self):
        token = await self.enroll()
        for key, value in [('host_token', '00'*32), ('password_salt', 'x'*32),
                           ('password_hash', []), ('allow_files', 1), ('mac', 'bad')]:
            msg = self.host_msg(token)
            msg[key] = value
            _, reply = await self.ws(msg)
            self.assertEqual(reply['type'], 'error')
        self.assertFalse(relay.sessions)

    async def test_proxy_opt_in_and_origin_rejection(self):
        with patch.dict(os.environ, {'ALLOW_INSECURE_LOCALHOST': '0', 'TRUST_PROXY': '1'}):
            r = await self.client.get('/', headers={**self.auth, 'X-Forwarded-Proto': 'https'})
            self.assertEqual(r.status, 200)
        r = await self.client.post('/api/enroll', json={'mac': MAC},
            headers={**self.auth, 'X-Requested-With': 'RemoteDesktop', 'Origin': 'https://evil.invalid'})
        self.assertEqual(r.status, 403)
        self.assertFalse(relay.machines)

    def test_invalid_state_structure_rejected(self):
        for data in [{}, [], {'machines': {MAC: {'host_token_hash': 'bad'}}}]:
            with open(relay.STATE_FILE, 'w') as f:
                json.dump(data, f)
            with self.assertRaises(ValueError):
                relay.load_state()

    async def test_rotation_disconnects_and_preserves_code(self):
        old = await self.enroll()
        h, _ = await self.ws(self.host_msg(old))
        c, _ = await self.ws(dict(role='client', protocol=2, code='123456', password=PASSWORD))
        await h.receive_json(timeout=2)
        new = await self.enroll()
        self.assertNotEqual(old, new)
        self.assertEqual((await h.receive(timeout=2)).type, WSMsgType.CLOSE)
        self.assertEqual((await c.receive(timeout=2)).type, WSMsgType.CLOSE)
        _, denied = await self.ws(self.host_msg(old))
        self.assertEqual(denied['type'], 'error')
        _, accepted = await self.ws(self.host_msg(new))
        self.assertEqual(accepted['code'], '123456')

    async def test_save_failure_keeps_credentials_and_session(self):
        token = await self.enroll()
        h, _ = await self.ws(self.host_msg(token))
        before = relay.machines[MAC].copy()
        with patch.object(relay.os, 'replace', side_effect=OSError('disk failed')):
            r = await self.post('/api/enroll', {'mac': MAC})
        self.assertEqual(r.status, 503)
        self.assertEqual(relay.machines[MAC], before)
        self.assertIs(relay.host_ctrl[MAC], relay.sessions['123456']['host'])
        self.assertFalse(h.closed)

    async def test_tls_password_headers_and_post_only(self):
        with patch.dict(os.environ, {'ALLOW_INSECURE_LOCALHOST': '0'}):
            r = await self.client.get('/api/machines', headers={**self.auth, 'X-Forwarded-Proto': 'https'})
            self.assertEqual(r.status, 403)
        with patch.object(relay, 'DASHBOARD_PASSWORD', 'short'):
            r = await self.client.get('/', headers={'Authorization': 'Basic YWRtaW46c2hvcnQ='})
            self.assertEqual(r.status, 401)
        for path in ['/api/set', '/api/wake', '/api/enroll']:
            r = await self.client.get(path, headers=self.auth)
            self.assertEqual(r.status, 405)
        r = await self.client.get('/', headers=self.auth)
        for name in ['Content-Security-Policy', 'X-Frame-Options', 'X-Content-Type-Options', 'Cache-Control']:
            self.assertIn(name, r.headers)
        html = await r.text()
        self.assertNotIn('innerHTML', html)
        self.assertNotIn('onclick=', html)

    def test_corrupt_state_fails_closed(self):
        with open(relay.STATE_FILE, 'w') as f:
            f.write('{broken')
        with self.assertRaises((ValueError, RuntimeError)):
            relay.load_state()

    async def test_fsync_failure_does_not_issue_token(self):
        with patch.object(relay.os, 'fsync', side_effect=OSError('disk failed')):
            r = await self.post('/api/enroll', {'mac': MAC})
        self.assertEqual(r.status, 503)
        self.assertFalse(relay.machines)
        self.assertFalse(os.path.exists(relay.STATE_FILE))

    async def test_fixed_code_reconnects_and_failed_set(self):
        token = await self.enroll()
        r = await self.post('/api/set', {'mac': MAC, 'code': '456789'})
        self.assertEqual(r.status, 200)
        for _ in range(3):
            _, reply = await self.ws(self.host_msg(token))
            self.assertEqual(reply['code'], '456789')
        with patch.object(relay.os, 'replace', side_effect=OSError('disk failed')):
            r = await self.post('/api/set', {'mac': MAC, 'code': '654321'})
        self.assertEqual(r.status, 503)
        self.assertEqual(relay.machines[MAC]['desired_code'], '456789')

    async def test_file_opt_in_and_result_json(self):
        token = await self.enroll()
        msg = self.host_msg(token)
        msg['allow_files'] = True
        h, _ = await self.ws(msg)
        c, reply = await self.ws(dict(role='client', protocol=2, code='123456', password=PASSWORD))
        self.assertTrue(reply['allow_files'])
        await h.receive_json(timeout=2)
        frame = struct.pack('>BI', 5, 2) + b'{}'
        await c.send_bytes(frame)
        self.assertEqual((await h.receive(timeout=2)).data, frame)
        result = {'type': 'file_result', 'ok': True, 'name': 'example.txt'}
        await h.send_json(result)
        self.assertEqual(await c.receive_json(timeout=2), result)

    def test_code_global_and_bucket_limits(self):
        app = self.client.server.app
        with patch.object(relay, 'CODE_ATTEMPTS', 2):
            self.assertTrue(relay.attempt(app, 'a', '123456'))
            self.assertTrue(relay.attempt(app, 'b', '123456'))
            self.assertFalse(relay.attempt(app, 'c', '123456'))
        with patch.object(relay, 'GLOBAL_ATTEMPTS', 3):
            self.assertFalse(relay.attempt(app, 'd', '654321'))
        with patch.object(relay, 'MAX_BUCKETS', len(app[relay.SECURITY]['attempts'])):
            self.assertFalse(relay.attempt(app, 'new', '555555'))

    async def test_malformed_handshake_consumes_attempt(self):
        with patch.object(relay, 'IP_ATTEMPTS', 1):
            _, reply = await self.ws([])
            self.assertEqual(reply['type'], 'error')
            _, second = await self.ws([])
            self.assertEqual(second['status'], 429)
            self.assertEqual((await self.client.get('/api/machines', headers=self.auth)).status, 200)


if __name__ == '__main__':
    unittest.main()
