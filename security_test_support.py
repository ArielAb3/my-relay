import hashlib
import base64
import json

MAC = 'AA:BB:CC:DD:EE:01'
OTHER_MAC = 'AA:BB:CC:DD:EE:02'
TOKEN = 'ab' * 32
PASSWORD = 'local integration test password'
SALT = '12' * 16
DIGEST = hashlib.pbkdf2_hmac('sha256', PASSWORD.encode(), bytes.fromhex(SALT), 600000).hex()


def machine():
    return {'host_token_hash': hashlib.sha256(TOKEN.encode()).hexdigest(), 'desired_code': '654321'}


def host_hello(mac=MAC, code='654321'):
    return {'role': 'host', 'protocol': 2, 'mac': mac, 'host_token': TOKEN,
            'password_salt': SALT, 'password_hash': DIGEST, 'allow_files': False,
            'preferred_code': code}


def client_hello(code='654321'):
    return {'role': 'client', 'protocol': 2, 'code': code, 'password': PASSWORD}


def auth_headers(hello):
    return {'X-RemoteDesktop-Auth': base64.b64encode(json.dumps(hello).encode()).decode()}
