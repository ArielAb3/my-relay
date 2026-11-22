import os
import tempfile
import unittest
from unittest.mock import patch
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer
import relay_server as relay
from security_test_support import MAC, PASSWORD, host_hello, auth_headers, machine


class MobileAccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for change in (
                patch.dict(os.environ, {'ALLOW_INSECURE_LOCALHOST': '1', 'DATABASE_URL': ''}),
                patch.object(relay, 'STATE_FILE', os.path.join(tmp.name, 'registry.json')),
                patch.object(relay, 'machines', {MAC: machine()}),
                patch.object(relay, 'sessions', {}), patch.object(relay, 'host_ctrl', {})):
            change.start()
            self.addCleanup(change.stop)
        self.browser = TestClient(TestServer(relay.make_app()))
        await self.browser.start_server()
        self.addAsyncCleanup(self.browser.close)
        self.origin = str(self.browser.make_url('/')).rstrip('/')

    async def host(self, capable=True):
        hello = host_hello()
        if capable:
            hello['mobile_consent'] = 1
        ws = await self.browser.ws_connect('/ws', headers=auth_headers(hello))
        self.addAsyncCleanup(ws.close)
        self.assertEqual((await ws.receive_json(timeout=2))['type'], 'code')
        return ws

    async def login(self, password=PASSWORD, origin=None):
        return await self.browser.post('/mobile/login', json={'code':'654321','password':password},
            headers={'Origin':self.origin if origin is None else origin,'X-Requested-With':'RemoteDesktop'})

    async def dial(self, cookie, origin=None):
        return await self.browser.ws_connect('/mobile/ws', headers={
            'Origin':self.origin if origin is None else origin,'Cookie':'rd_mobile='+cookie})

    async def request(self, host):
        login = await self.login()
        self.assertEqual(login.status,200)
        cookie = login.cookies['rd_mobile']
        self.assertTrue(cookie['httponly'] and cookie['secure'])
        self.assertEqual(cookie['samesite'], 'Strict')
        mobile = await self.dial(cookie.value)
        self.addAsyncCleanup(mobile.close)
        self.assertEqual((await mobile.receive_json(timeout=2))['type'],'waiting')
        request = await host.receive_json(timeout=2)
        self.assertEqual(request['type'],'mobile_request')
        return mobile, request['id'], cookie.value

    async def test_home_screen_entry(self):
        landing = await self.browser.get('/mobile/')
        self.assertEqual(landing.status,200)
        self.assertIn('apple-touch-icon', await landing.text())
        manifest = await (await self.browser.get('/mobile/manifest.webmanifest')).json(content_type=None)
        self.assertEqual(manifest['start_url'],'/mobile/')
        self.assertEqual(manifest['display'],'standalone')

    async def test_origin_password_and_old_host(self):
        await self.host(capable=False)
        self.assertNotEqual((await self.login()).status,200)
        self.assertEqual((await self.login(origin='https://another.example')).status,403)
        self.assertEqual((await self.login(password='a different password')).status,401)

    async def test_approval_gates_screen_and_input(self):
        host = await self.host()
        mobile, request_id, cookie = await self.request(host)
        with self.assertRaises(WSServerHandshakeError):
            await self.dial(cookie)
        fake_frame = bytes([1,0,0,0,1,42])
        await host.send_bytes(fake_frame)
        await host.send_json({'type':'mobile_decision','id':request_id,'approved':True})
        self.assertEqual((await mobile.receive_json(timeout=2))['type'],'connected')
        self.assertEqual((await host.receive_json(timeout=2))['type'],'mobile_start')
        await host.send_bytes(fake_frame)
        self.assertEqual(await mobile.receive_bytes(timeout=2),fake_frame)
        click = bytes([2,0,0,0,10,3,0,0,0,9,0,0,0,6,0])
        await mobile.send_bytes(click)
        self.assertEqual(await host.receive_bytes(timeout=2),click)

    async def test_denial_ends_request(self):
        host = await self.host()
        mobile, request_id, _ = await self.request(host)
        await host.send_json({'type':'mobile_decision','id':request_id,'approved':False})
        self.assertEqual((await mobile.receive_json(timeout=2))['type'],'denied')
        self.assertIn((await mobile.receive(timeout=3)).type.name,('CLOSE','CLOSED'))

    async def test_client_input_and_files_never_pass_before_approval(self):
        host = await self.host()
        mobile, _, _ = await self.request(host)
        await mobile.send_bytes(bytes([2,0,0,0,10,3,0,0,0,9,0,0,0,6,0]))
        self.assertIn((await mobile.receive(timeout=3)).type.name, ('CLOSE','CLOSED'))
        terminal = await host.receive(timeout=3)
        self.assertNotEqual(terminal.type.name, 'BINARY')

    async def test_expired_ticket_and_password_not_accepted_again(self):
        await self.host()
        result = await self.login()
        self.assertEqual(result.status,200)
        token = result.cookies['rd_mobile'].value
        tickets = self.browser.app[relay.SECURITY]['mobile_tickets']
        _, code, session = tickets[token]
        tickets[token] = (0,code,session)
        with self.assertRaises(WSServerHandshakeError):
            await self.dial(token)
        self.assertNotIn(token,tickets)
        self.assertEqual((await self.login(password='bad-password-here!')).status,401)

    async def test_cross_origin_socket_and_host_replacement(self):
        await self.host()
        result = await self.login()
        self.assertEqual(result.status,200)
        cookie = result.cookies['rd_mobile'].value
        with self.assertRaises(WSServerHandshakeError) as failure:
            await self.dial(cookie,origin='https://another.example')
        self.assertEqual(failure.exception.status,403)
        await self.host()
        with self.assertRaises(WSServerHandshakeError):
            await self.dial(cookie)
