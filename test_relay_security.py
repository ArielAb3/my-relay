import unittest
import base64
import os
import struct
import tempfile
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

import relay_server as relay
from security_test_support import MAC, machine, host_hello, client_hello, auth_headers


class PublicConfigTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {'ALLOW_INSECURE_LOCALHOST': '1'})
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_unconfigured_dashboard_rejects_empty_password(self):
        with patch.object(relay, 'DASHBOARD_PASSWORD', ''):
            async with TestClient(TestServer(relay.make_app())) as client:
                response = await client.get('/api/machines', headers={
                    'Authorization': 'Basic ' + base64.b64encode(b'admin:').decode()})
                self.assertEqual(response.status, 401)

    async def test_fixed_code_and_binary_relay(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(relay, 'STATE_FILE', str(Path(directory) / 'state.json')), \
                 patch.object(relay, 'sessions', {}), \
                 patch.object(relay, 'host_ctrl', {}), \
                 patch.object(relay, 'machines', {MAC: machine()}):
                async with TestClient(TestServer(relay.make_app())) as client:
                    for _ in range(3):
                        host = await client.ws_connect('/ws', headers=auth_headers(host_hello()))
                        self.assertEqual((await host.receive_json(timeout=3))['code'], '654321')
                        operator = await client.ws_connect('/ws', headers=auth_headers(client_hello()))
                        self.assertEqual((await operator.receive_json(timeout=3))['type'], 'connected')
                        self.assertEqual((await host.receive_json(timeout=3))['type'], 'connected')
                        keys = struct.pack('>BI', 3, 2) + b'\x02x'
                        screen = struct.pack('>BI', 1, 6) + b'screen'
                        await operator.send_bytes(keys)
                        self.assertEqual((await host.receive(timeout=3)).data, keys)
                        await host.send_bytes(screen)
                        self.assertEqual((await operator.receive(timeout=3)).data, screen)
                        await operator.close()
                        await host.close()

    async def test_public_config_does_not_expose_connection_codes(self):
        original = relay.machines
        relay.machines = {'AA:BB:CC:DD:EE:FF': {'desired_code': '123456'}}
        try:
            async with TestClient(TestServer(relay.make_app())) as client:
                response = await client.get('/config.json')
                self.assertEqual(response.status, 200)
                config = await response.json()
                self.assertEqual(set(config), {'relay', 'version', 'host_url'})
                self.assertNotIn('123456', await response.text())
                protected = await client.get('/api/machines')
                self.assertEqual(protected.status, 401)
        finally:
            relay.machines = original


if __name__ == '__main__':
    unittest.main()
