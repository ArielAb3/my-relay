#!/usr/bin/env python3
"""
Relay Server + Management Dashboard  (aiohttp)
One port serves everything (Render provides $PORT):
  GET  /ws            -> WebSocket relay (Host <-> Client) and Host control channel
  GET  /config.json   -> public release metadata (no connection codes)
  GET  /healthz       -> local readiness only (no database polling)
  GET  /              -> password-protected dashboard (HTML)
  GET  /api/machines  -> JSON list of machines (auth)
  POST /api/set       -> set a machine's desired code / name (auth)
  POST /api/enroll    -> provision/rotate a per-MAC host token (returned once)
  POST /api/invite    -> admin issues a 15-minute single-use pairing code
  POST /api/pair      -> redeem invitation with a Host-generated token
  POST /api/wake      -> request Wake-on-LAN

Dashboard password: DASHBOARD_PASSWORD, at least 16 characters. Mutations require
JSON and X-Requested-With: RemoteDesktop. TLS is mandatory. TRUST_PROXY=1 permits
X-Forwarded-Proto: https ONLY behind a trusted TLS proxy which overwrites that
header and prevents direct public access to this port. Forwarded IPs are ignored;
limits use the transport peer (so proxy deployments share its per-IP quota).
ALLOW_INSECURE_LOCALHOST=1 permits cleartext only from a literal loopback peer.
STATE_FILE must be durable storage; invalid/unreadable state aborts startup.
DATABASE_URL selects PostgreSQL instead, with verify-full TLS and certifi roots.
Run --init-db explicitly once before first startup; an existing missing/corrupt
state row is never reset. Connections close after each actual durable change;
polling, health checks and unchanged reconnects do not access the database.
Revision conflicts poison this process and /healthz returns 503. Deploy one relay:
CAS detects competing writes, not changes to another process's idle sessions.
/healthz follows the same TLS/TRUST_PROXY rules as every other route.
WebSocket authentication is base64 UTF-8 JSON in X-RemoteDesktop-Auth (decoded
limit 4096 bytes), before upgrade; there is no initial WebSocket hello frame.
HTTP 401 means authentication failed, 429 attempt limit, 503 capacity/state.
"""

import asyncio
import json
import os
import time
import secrets
import hashlib
import ipaddress
import re
import tempfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import base64
import hmac
import math
from functools import wraps

from aiohttp import web, WSMsgType

DASHBOARD_PASSWORD = os.environ.get('DASHBOARD_PASSWORD', '')
STATE_FILE = os.environ.get('STATE_FILE', '/tmp/rdp_state.json')
LATEST_VERSION = os.environ.get('LATEST_VERSION', '')
HOST_URL = os.environ.get('HOST_URL', '')
PUBLIC_RELAY = os.environ.get('PUBLIC_RELAY', '')

# ---- state ----
sessions = {}    # code -> {'host': ws, 'client': ws}
machines = {}    # mac  -> {name, code, desired_code, online, version, last_seen, ip}
invites = {}     # SHA256(pairing code) -> expiry and optional redemption identity
host_ctrl = {}   # mac  -> host control ws (to push set_code)
SECURITY = web.AppKey('security', dict)
MAX_WS = 128
MAX_WS_PER_IP = 16
MAX_HASHES = 4
AUTH_WINDOW = 60
IP_ATTEMPTS = 60
CODE_ATTEMPTS = 10
GLOBAL_ATTEMPTS = 240
MAX_BUCKETS = 2048
MAX_INVITES = 256
MAX_MACHINES = 10000
STATE_BYTES = 8 * 1024 * 1024
DB_TIMEOUT = 10
MAX_PAIR_READS = 16
MAX_PAIR_READS_PER_IP = 4


def valid_hex(value, length):
    return isinstance(value, str) and re.fullmatch('[0-9a-fA-F]{%d}' % length, value) is not None


def valid_mac(value):
    return isinstance(value, str) and re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', value) is not None


def valid_code(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9]{6}', value) is not None


def attempt(app, peer, code=None, *, dashboard=False):
    state = app[SECURITY]
    now = time.monotonic()
    buckets = state['dashboard_attempts' if dashboard else 'attempts']
    for key in list(buckets):
        q = buckets[key]
        while q and q[0] <= now - AUTH_WINDOW:
            q.popleft()
        if not q:
            del buckets[key]
    keys = [('global', GLOBAL_ATTEMPTS), ('ip:' + peer, IP_ATTEMPTS)] if peer is not None else []
    if code is not None:
        keys.append(('code:' + code, CODE_ATTEMPTS))
    if len(buckets) + sum(key not in buckets for key, _ in keys) > MAX_BUCKETS:
        return False
    allowed = all(len(buckets.get(key, ())) < limit for key, limit in keys)
    for key, limit in keys:
        q = buckets.setdefault(key, deque())
        if len(q) < limit:
            q.append(now)
    return allowed


def transport_peer(request):
    peer = request.transport.get_extra_info('peername') if request.transport else None
    return str(peer[0]) if peer else '?'


@web.middleware
async def security_middleware(request, handler):
    peer = transport_peer(request)
    try:
        local = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        local = False
    secure = request.secure or (os.environ.get('TRUST_PROXY') == '1'
                                and request.headers.get('X-Forwarded-Proto') == 'https')
    if not secure and not (os.environ.get('ALLOW_INSECURE_LOCALHOST') == '1' and local):
        response = web.Response(status=403, text='TLS required')
    else:
        try:
            if request.app[SECURITY]['state_failed']:
                raise web.HTTPServiceUnavailable(text='State unavailable; restart required')
            response = await handler(request)
        except web.HTTPException as exc:
            response = web.Response(status=exc.status, text=exc.text, headers=exc.headers)
    if not response.prepared:
        set_security_headers(response)
    return response


def set_security_headers(response):
    script = DASHBOARD_HTML.split('<script>', 1)[1].split('</script>', 1)[0]
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    response.headers.update({'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY',
                             'X-Content-Type-Options': 'nosniff',
                             'Referrer-Policy': 'no-referrer',
                             'Content-Security-Policy': "default-src 'none'; script-src 'sha256-" + digest +
                             "'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})


def load_state():
    global machines, invites
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            raw = f.read(STATE_BYTES + 1)
            if len(raw) > STATE_BYTES:
                raise ValueError('State too large')
            data = json.loads(raw)
    except FileNotFoundError:
        machines = {}
        invites = {}
        return
    data = validate_state(data)
    machines, invites = data['machines'], data['invites']


def validate_state(data):
    if not isinstance(data, dict) or not isinstance(data.get('machines'), dict):
        raise ValueError('Invalid machine registry')
    if (set(data) not in ({'machines'}, {'schema', 'machines', 'invites'})
            or ('schema' in data and (type(data['schema']) is not int or data['schema'] != 1))):
        raise ValueError('Invalid state envelope')
    registry = data['machines']
    if len(registry) > MAX_MACHINES:
        raise ValueError('Registry full')
    normalized, codes = {}, {}
    for mac, m in registry.items():
        if not valid_mac(mac) or not isinstance(m, dict):
            raise ValueError('Invalid machine registry')
        mac = mac.upper()
        if mac in normalized or set(m) - {'name', 'desired_code', 'code', 'host_token_hash'}:
            raise ValueError('Invalid machine metadata')
        if any(not isinstance(m.get(k, ''), str) for k in ('name', 'desired_code', 'code')):
            raise ValueError('Invalid machine metadata')
        if len(m.get('name', '')) > 80:
            raise ValueError('Invalid machine name')
        if any(m.get(k) and not valid_code(m[k]) for k in ('desired_code', 'code')):
            raise ValueError('Invalid stored code')
        if 'host_token_hash' in m and not valid_hex(m['host_token_hash'], 64):
            raise ValueError('Invalid stored credential')
        for code in (m.get('code'), m.get('desired_code')):
            if code and codes.setdefault(code, mac) != mac:
                raise ValueError('Duplicate code reservation')
        normalized[mac] = m.copy()
    pending = data.get('invites', {})
    if not isinstance(pending, dict) or len(pending) > MAX_INVITES:
        raise ValueError('Invalid invites')
    for digest, item in pending.items():
        if not valid_hex(digest, 64) or digest != digest.lower() or not isinstance(item, dict):
            raise ValueError('Invalid invite')
        expiry = item.get('expires_at')
        if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0:
            raise ValueError('Invalid invite expiry')
        if set(item) not in ({'expires_at'}, {'expires_at', 'mac', 'host_token_hash', 'code'}):
            raise ValueError('Invalid invite redemption')
        if 'mac' in item and (not valid_mac(item['mac']) or item['mac'] != item['mac'].upper()
                or item['mac'] not in normalized or not valid_hex(item['host_token_hash'], 64)
                or not valid_code(item['code'])):
            raise ValueError('Invalid invite identity')
    return {'schema': 1, 'machines': normalized, 'invites': {k: v.copy() for k, v in pending.items()}}


def state_snapshot(registry, pending):
    clean = {mac: {k: m[k] for k in ('name', 'desired_code', 'code', 'host_token_hash') if k in m}
             for mac, m in registry.items()}
    return validate_state({'schema': 1, 'machines': clean, 'invites': pending})


class DatabaseStartupError(OSError):
    """Safe startup diagnostics containing only application-defined labels."""


class PostgresState:
    """Short-lived TLS connections with revision CAS; no polling or idle leases.

    Stale writers fail closed. Live WebSocket membership remains process-local:
    deploy one relay, not a multi-worker service. All libpq calls run off-loop.
    Any connection/commit error requires restart and reload, never a blind retry.
    """
    def __init__(self, url):
        self.url, self.connection, self.revision = url, None, None
        self.failed = False
        self.snapshot = None
        self.closed = False
        self.phase = 'startup'
        self.worker = ThreadPoolExecutor(max_workers=1)

    async def run(self, operation):
        if self.failed:
            raise OSError('Database unavailable; restart required')
        future = asyncio.get_running_loop().run_in_executor(self.worker, operation)
        try:
            return await asyncio.wait_for(asyncio.shield(future), DB_TIMEOUT)
        except BaseException:
            self.failed = True
            # Retrieve late errors after a timeout without accepting a late commit.
            future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
            raise

    async def open(self, initialize=False):
        try:
            return await self.run(lambda: self._open(initialize))
        except Exception as exc:
            phase = self.phase
            category = ('timeout' if isinstance(exc, TimeoutError) else
                        'dependency' if isinstance(exc, ImportError) else
                        'validation' if phase == 'validate-state' else
                        'configuration' if isinstance(exc, ValueError) else
                        'connection' if phase == 'connect' else 'database')
            try:
                await self.close()
            except Exception:
                pass
            raise DatabaseStartupError(f'Database startup failed (phase={phase}, category={category})') from None

    def _open(self, initialize):
        from psycopg.types.json import Jsonb
        with self._connect() as connection:
            self.phase = 'schema-check'
            exists = connection.execute("SELECT to_regclass('public.remote_desktop_state')").fetchone()[0]
            if exists is None:
                if not initialize:
                    raise OSError('Database not initialized; run --init-db')
                self.phase = 'initialize-schema'
                connection.execute('CREATE TABLE public.remote_desktop_state ('
                    'id integer PRIMARY KEY CHECK (id = 1), revision bigint NOT NULL, payload jsonb NOT NULL)')
                empty = {'schema': 1, 'machines': {}, 'invites': {}}
                connection.execute('INSERT INTO public.remote_desktop_state VALUES (1, 0, %s)', (Jsonb(empty),))
            self.phase = 'load-state'
            row = connection.execute('SELECT revision, payload FROM public.remote_desktop_state WHERE id = 1').fetchone()
            self.phase = 'validate-state'
            if row is None or type(row[0]) is not int or row[0] < 0:
                raise ValueError('Invalid database revision')
            snapshot = validate_state(row[1])
            self.phase = 'commit-startup'
        self.revision, self.snapshot = row[0], validate_state(snapshot)
        return snapshot

    def _connect(self):
        self.phase = 'configuration'
        import psycopg
        import certifi
        from psycopg.conninfo import conninfo_to_dict
        options = conninfo_to_dict(self.url)
        if options.get('sslmode', 'verify-full') not in ('require', 'verify-ca', 'verify-full'):
            raise ValueError('Database TLS required')
        options['sslmode'] = 'verify-full'
        if not options.get('sslrootcert') or options['sslrootcert'] == 'system':
            options['sslrootcert'] = certifi.where()
        options['channel_binding'] = 'require'
        options.pop('options', None)
        options.update(connect_timeout=5,
                       gssencmode='disable', tcp_user_timeout=5000,
                       keepalives=1, keepalives_idle=5, keepalives_interval=2, keepalives_count=2)
        self.phase = 'connect'
        self.connection = psycopg.connect(**options)
        try:
            self.phase = 'verify-tls'
            if not self.connection.pgconn.ssl_in_use:
                raise OSError('Database TLS required')
            # Transaction-local settings work through transaction-pooling proxies.
            self.phase = 'transaction-setup'
            self.connection.execute("SET LOCAL statement_timeout = '5s'")
            self.connection.execute("SET LOCAL lock_timeout = '2s'")
        except BaseException:
            self.connection.close()
            raise
        return self.connection

    async def save(self, snapshot):
        snapshot = validate_state(snapshot)
        if self.failed:
            raise OSError('Database unavailable; restart required')
        if snapshot == self.snapshot:
            return
        def write():
            from psycopg.types.json import Jsonb
            with self._connect() as connection:
                row = connection.execute('UPDATE public.remote_desktop_state SET payload = %s, revision = revision + 1 '
                    'WHERE id = 1 AND revision = %s RETURNING revision', (Jsonb(snapshot), self.revision)).fetchone()
                if row is None:
                    raise OSError('Database revision conflict')
            self.revision, self.snapshot = row[0], snapshot
        await self.run(write)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        # Queue close behind an in-flight call; its connection/statement/TCP timeouts
        # remain bounded even if the awaiting HTTP request has been cancelled.
        def close_connection():
            if self.connection is not None:
                self.connection.close()
        try:
            await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(self.worker, close_connection),
                                   DB_TIMEOUT + 2)
        finally:
            self.worker.shutdown(wait=False, cancel_futures=True)

class StateCommitError(OSError):
    """The replacement succeeded, but its directory durability is uncertain."""
    def __init__(self, registry, pending=None):
        super().__init__('State replacement could not be durably confirmed')
        self.registry = registry
        self.invites = invites.copy() if pending is None else pending


async def reconcile_state(error):
    # Revoke memberships before yielding: pending authentication must also fail.
    old_pairs = list(sessions.values())
    sessions.clear()
    host_ctrl.clear()
    machines.clear()
    machines.update({mac: {**m, 'online': False} for mac, m in error.registry.items()})
    invites.clear()
    invites.update(error.invites)
    for session in old_pairs:
        for ws in (session.get('client'), session.get('host')):
            if ws is not None and not ws.closed:
                await ws.close(code=1011, message=b'State durability failure')


def save_state(registry=None, pending=None):
    registry = machines if registry is None else registry
    snapshot = state_snapshot(registry, invites if pending is None else pending)
    directory = os.path.dirname(os.path.abspath(STATE_FILE))
    fd, temporary = tempfile.mkstemp(prefix='.rdp-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, STATE_FILE)
        try:
            if os.name != 'nt':
                directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except OSError as exc:
            raise StateCommitError(snapshot['machines'], snapshot['invites']) from exc
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def gen_code(preferred=None):
    if valid_code(preferred):
        return preferred
    while True:
        c = f'{secrets.randbelow(1000000):06d}'
        if c not in sessions and not any(c in (m.get('desired_code'), m.get('code')) for m in machines.values()):
            return c

def mark_stale():
    for mac, m in machines.items():
        ws = host_ctrl.get(mac)
        m['online'] = ws is not None and not ws.closed


def reserved_for_other(code, mac):
    session = sessions.get(code)
    return (session is not None and session['mac'] != mac) or any(
        key != mac and code in (m.get('desired_code'), m.get('code')) for key, m in machines.items())


def serialized(handler):
    @wraps(handler)
    async def wrapper(request, *args):
        state = request.app[SECURITY]
        if len(state['mutations']) >= 32:
            raise web.HTTPServiceUnavailable(text='State busy')
        async def execute():
            async with state['state_lock']:
                if state['state_failed']:
                    raise web.HTTPServiceUnavailable(text='State unavailable')
                return await handler(request, *args)
        task = asyncio.create_task(execute())
        state['mutations'].add(task)
        def finished(done):
            state['mutations'].discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        # The task owns the lock through persistence AND publication, even if the
        # HTTP caller disappears after the database has committed.
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Let a host registration publish before its socket cleanup runs.
            try:
                await asyncio.shield(task)
            finally:
                raise
    return wrapper


async def fail_database(app):
    app[SECURITY]['state_failed'] = True
    await reconcile_state(StateCommitError(dict(machines), dict(invites)))


async def persist(app, registry, pending=None):
    pending = invites if pending is None else pending
    store = app[SECURITY]['store'] if app is not None else None
    try:
        snapshot = state_snapshot(registry, pending)
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text='Invalid candidate state') from None
    try:
        if store is None:
            save_state(registry, pending)
        else:
            await store.save(snapshot)
    except StateCommitError as exc:
        await reconcile_state(exc)
        raise web.HTTPServiceUnavailable(text='State unavailable; enroll again') from None
    except Exception:
        if store is not None:
            await fail_database(app)
        raise web.HTTPServiceUnavailable(text='State unavailable') from None


@serialized
async def register_host(request, ws, mac, code, original_machine, original_control, previous, msg, name, version):
    # Revalidate after both WebSocket upgrade and waiting for another mutation.
    if (machines.get(mac) is not original_machine or host_ctrl.get(mac) is not original_control
            or sessions.get(code) is not previous
            or any(key != mac and code in (item.get('desired_code'), item.get('code'))
                   for key, item in machines.items())):
        raise ValueError('Authentication failed')
    m = original_machine.copy()
    session = {'host': ws, 'client': None, 'mac': mac,
               'password_salt': msg['password_salt'], 'password_hash': msg['password_hash'],
               'allow_files': msg['allow_files'], 'host_token_hash': m['host_token_hash']}
    m.update({'name': m.get('name') or name, 'code': code,
              'desired_code': m.get('desired_code', ''), 'online': True, 'version': version,
              'last_seen': time.time(), 'ip': transport_peer(request)})
    await persist(request.app, {**machines, mac: m})
    machines[mac] = m
    old_sessions = [s for s in sessions.values() if s.get('mac') == mac]
    for old_code in list(sessions):
        if sessions[old_code].get('mac') == mac:
            del sessions[old_code]
    sessions[code] = session
    host_ctrl[mac] = ws
    return session, old_sessions


# ============================================================
#  WebSocket endpoint
# ============================================================
async def ws_endpoint(request):
    state = request.app[SECURITY]
    peer_ip = transport_peer(request)
    if state['ws_count'] >= MAX_WS or state['ws_peers'].get(peer_ip, 0) >= MAX_WS_PER_IP:
        return web.Response(status=503, text='Authentication failed')
    state['ws_count'] += 1
    state['ws_peers'][peer_ip] = state['ws_peers'].get(peer_ip, 0) + 1
    ws = web.WebSocketResponse(max_msg_size=12 * 1024 * 1024, heartbeat=30, timeout=2)
    set_security_headers(ws)

    role = code = mac = None
    failure_status = 401

    try:
        if not attempt(request.app, peer_ip):
            return web.Response(status=429, text='Authentication failed')
        headers = request.headers.getall('X-RemoteDesktop-Auth', [])
        if len(headers) != 1 or len(headers[0]) > 5464:
            raise ValueError('Authentication failed')
        decoded = base64.b64decode(headers[0], validate=True)
        if len(decoded) > 4096:
            raise ValueError('Authentication failed')
        msg = json.loads(decoded.decode('utf-8'))
        if not isinstance(msg, dict):
            raise ValueError('Authentication failed')
        candidate = msg.get('code')
        if valid_code(candidate) and not attempt(request.app, None, candidate):
            return web.Response(status=429, text='Authentication failed')
        if type(msg.get('protocol')) is not int or msg['protocol'] != 2:
            raise ValueError('Authentication failed')
        role = msg.get('role')
        code = msg.get('code')

        # ---------------- HOST ----------------
        if role == 'host':
            if not valid_mac(msg.get('mac')):
                raise ValueError('Authentication failed')
            mac = msg['mac'].upper()
            m = machines.get(mac)
            token = msg.get('host_token')
            if (not m or not valid_hex(token, 64)
                    or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), m.get('host_token_hash', ''))
                    or not valid_hex(msg.get('password_salt'), 32)
                    or not valid_hex(msg.get('password_hash'), 64)
                    or type(msg.get('allow_files')) is not bool):
                raise ValueError('Authentication failed')
            name = msg.get('name') or mac
            version = msg.get('version') or '?'
            if not isinstance(name, str) or not isinstance(version, str):
                raise ValueError('Authentication failed')
            name, version = name[:80], version[:40]

            desired = machines.get(mac, {}).get('desired_code', '')
            supplied = msg.get('preferred_code', '')
            if not isinstance(supplied, str) or (supplied and not valid_code(supplied)):
                raise ValueError('Authentication failed')
            if supplied and not desired and supplied not in sessions and reserved_for_other(supplied, mac):
                supplied = ''
            preferred = desired or supplied or m.get('code')
            code = gen_code(preferred)
            previous = sessions.get(code)
            if reserved_for_other(code, mac):
                raise ValueError('Authentication failed')
            original_machine = m
            original_control = host_ctrl.get(mac)
            await ws.prepare(request)
            failure_status = 503
            session, old_sessions = await register_host(request, ws, mac, code, original_machine,
                                                       original_control, previous, msg, name, version)
            failure_status = 401

            for previous in old_sessions:
                for old in (previous.get('client'), previous.get('host')):
                    if old is not None and not old.closed:
                        await old.close(code=1000, message=b'Host reconnected')

            if sessions.get(code) is not session:
                return ws
            await ws.send_str(json.dumps({'type': 'code', 'code': code, 'protocol': 2}))
            print(f"[relay] HOST {name} ({mac}) code={code} v{version}")

            # Relay loop: forward host->client; also accept control messages
            async for raw in ws:
                if sessions.get(code) is not session:
                    break
                if mac in machines:
                    machines[mac]['last_seen'] = time.time()
                    machines[mac]['online'] = True
                if raw.type == WSMsgType.BINARY:
                    cl = session.get('client')
                    if cl is not None and not cl.closed:
                        try: await cl.send_bytes(raw.data)
                        except Exception: pass
                elif raw.type == WSMsgType.TEXT:
                    # host may send control/status JSON; forward to client if present
                    cl = session.get('client')
                    if cl is not None and not cl.closed:
                        try: await cl.send_str(raw.data)
                        except Exception: pass
                elif raw.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break

        # ---------------- CLIENT ----------------
        elif role == 'client':
            password = msg.get('password')
            if (not valid_code(code) or not isinstance(password, str) or not 16 <= len(password) <= 128
                    or len(password.encode('utf-8')) > 512):
                raise ValueError('Authentication failed')
            session = sessions.get(code)
            if not session or session['host'].closed or session.get('client') is not None:
                raise ValueError('Authentication failed')
            if state['hash_count'] >= MAX_HASHES:
                return web.Response(status=503, text='Authentication failed')
            state['hash_count'] += 1
            future = asyncio.get_running_loop().run_in_executor(
                state['executor'], hashlib.pbkdf2_hmac, 'sha256', password.encode('utf-8'),
                bytes.fromhex(session['password_salt']), 600000, 32)
            # A cancelled request must not release capacity while its thread still runs.
            def release_hash(_):
                state['hash_count'] -= 1
            future.add_done_callback(release_hash)
            digest = await asyncio.shield(future)
            if (sessions.get(code) is not session or session['host'].closed
                    or session.get('client') is not None
                    or machines.get(session['mac'], {}).get('host_token_hash') != session['host_token_hash']
                    or not hmac.compare_digest(digest.hex(), session['password_hash'].lower())):
                raise ValueError('Authentication failed')
            await ws.prepare(request)
            if (sessions.get(code) is not session or session['host'].closed
                    or session.get('client') is not None
                    or machines.get(session['mac'], {}).get('host_token_hash') != session['host_token_hash']):
                raise ValueError('Authentication failed')
            session['client'] = ws
            host_ws = session['host']
            connected = {'type': 'connected', 'protocol': 2, 'allow_files': session['allow_files']}
            await ws.send_json(connected)
            try:
                if sessions.get(code) is session and session.get('client') is ws:
                    await host_ws.send_json(connected)
            except Exception:
                pass

            async for raw in ws:
                if sessions.get(code) is not session or session.get('client') is not ws:
                    break
                if raw.type == WSMsgType.BINARY:
                    data = raw.data
                    if (len(data) < 5 or int.from_bytes(data[1:5], 'big') != len(data) - 5
                            or data[0] not in (2, 3, 5, 6, 7)
                            or (data[0] in (5, 6, 7) and not session['allow_files'])):
                        continue
                    if not host_ws.closed:
                        try: await host_ws.send_bytes(raw.data)
                        except Exception: break
                elif raw.type == WSMsgType.TEXT:
                    # Desktop clients send binary input/files, never relay control commands.
                    continue
                elif raw.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break

        else:
            raise ValueError('Authentication failed')
    except Exception as exc:
        if isinstance(exc, StateCommitError):
            await reconcile_state(exc)
        if not ws.prepared:
            return web.Response(status=failure_status, text='Authentication failed')
        if ws.prepared and not ws.closed:
            try:
                await ws.send_json({'type': 'error', 'msg': 'Authentication failed'})
            except Exception:
                pass
    finally:
        if isinstance(code, str) and code in sessions:
            is_host = sessions[code].get('host') is ws
            is_client = sessions[code].get('client') is ws
            other = sessions[code].get('client' if is_host else 'host') if is_host or is_client else None
            # Remove only this socket's membership before yielding to other connections.
            if is_host:
                sessions.pop(code, None)
            elif is_client:
                sessions[code]['client'] = None
            if other is not None and not other.closed:
                try: await other.send_str(json.dumps({'type': 'disconnected'}))
                except Exception: pass
        if mac and mac in machines and host_ctrl.get(mac) is ws:
            machines[mac]['online'] = False
            machines[mac]['last_seen'] = time.time()
            if host_ctrl.get(mac) is ws:
                host_ctrl.pop(mac, None)
        try:
            if ws.prepared:
                await ws.close()
        finally:
            state['ws_count'] -= 1
            state['ws_peers'][peer_ip] -= 1
            if not state['ws_peers'][peer_ip]:
                del state['ws_peers'][peer_ip]
    return ws


# ============================================================
#  HTTP endpoints
# ============================================================
def check_auth(request):
    if not attempt(request.app, transport_peer(request), dashboard=True):
        raise web.HTTPTooManyRequests(text='Try again later', headers={'Retry-After': str(AUTH_WINDOW)})
    if len(DASHBOARD_PASSWORD) < 16:
        return False
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Basic '):
        return False
    try:
        _, pw = base64.b64decode(auth[6:]).decode().split(':', 1)
        return hmac.compare_digest(pw.encode('utf-8'), DASHBOARD_PASSWORD.encode('utf-8'))
    except Exception:
        return False

def need_auth():
    return web.Response(status=401, text='Auth required',
                        headers={'WWW-Authenticate': 'Basic realm="Dashboard"'})

async def config_json(request):
    cfg = {
        'relay': PUBLIC_RELAY,
        'version': LATEST_VERSION,
        'host_url': HOST_URL,
    }
    return web.json_response(cfg)


async def healthz(request):
    # security_middleware enforces TLS and checks state_failed; no storage I/O.
    return web.Response(text='ok')

async def api_machines(request):
    if not check_auth(request):
        return need_auth()
    mark_stale()
    now = int(time.time())
    in_use = {s['mac'] for s in sessions.values()
              if s.get('client') is not None and not s['client'].closed}
    out = [{
        'mac': mac, 'name': m.get('name', ''), 'code': m.get('code', ''),
        'desired_code': m.get('desired_code', ''), 'online': bool(m.get('online')), 'in_use': mac in in_use,
        'version': m.get('version', '?'), 'ip': m.get('ip', '?'),
        'last_seen': int(m.get('last_seen', 0)),
    } for mac, m in machines.items()]
    out.sort(key=lambda x: (not x['online'], x['name'] or x['mac']))
    return web.json_response({'machines': out, 'now': now})

async def mutation_data(request, require_mac=True):
    if request.headers.get('X-Requested-With') != 'RemoteDesktop':
        raise web.HTTPForbidden(text='Request rejected')
    # No CORS: cross-origin custom headers require a preflight we never allow.
    origin = request.headers.get('Origin')
    scheme = 'https' if request.secure or (os.environ.get('TRUST_PROXY') == '1'
                 and request.headers.get('X-Forwarded-Proto') == 'https') else 'http'
    if origin and origin != scheme + '://' + request.host:
        raise web.HTTPForbidden(text='Request rejected')
    try:
        async with asyncio.timeout(5):
            data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='Invalid JSON')
    if not isinstance(data, dict) or (require_mac and not valid_mac(data.get('mac'))):
        raise web.HTTPBadRequest(text='Invalid MAC')
    return data


async def commit_machine(mac, machine, app=None):
    await persist(app, {**machines, mac: machine})
    machines[mac] = machine


@serialized
async def api_enroll(request):
    if not check_auth(request):
        return need_auth()
    data = await mutation_data(request)
    mac = data['mac'].upper()
    if mac not in machines and len(machines) >= MAX_MACHINES:
        raise web.HTTPServiceUnavailable(text='Registry capacity reached')
    token = secrets.token_hex(32)
    m = machines.get(mac, {'name': mac, 'code': '', 'desired_code': ''}).copy()
    m['host_token_hash'] = hashlib.sha256(token.encode()).hexdigest()
    m['online'] = False
    await commit_machine(mac, m, request.app)
    old_pairs = []
    for code, session in list(sessions.items()):
        if session.get('mac') == mac:
            old_pairs.append(session)
            del sessions[code]
    host_ctrl.pop(mac, None)
    for session in old_pairs:
        for ws in (session.get('client'), session.get('host')):
            if ws is not None and not ws.closed:
                await ws.close(code=1000, message=b'Credential rotated')
    return web.json_response({'ok': True, 'mac': mac, 'host_token': token})


@serialized
async def api_set(request):
    if not check_auth(request):
        return need_auth()
    data = await mutation_data(request)
    mac = data['mac'].upper()
    if mac not in machines:
        raise web.HTTPBadRequest(text='Unknown machine')
    m = machines[mac].copy()
    notify_host = False
    if 'code' in data:
        nc = data['code']
        if nc != '' and not valid_code(nc):
            raise web.HTTPBadRequest(text='Invalid code')
        if nc and reserved_for_other(nc, mac):
            return web.json_response({'ok': False, 'err': 'code already assigned'}, status=409)
        notify_host = not nc or nc != m.get('code') or m.get('desired_code') not in ('', None, nc)
        m['desired_code'] = nc
        if not nc:
            m['code'] = ''
    if 'name' in data:
        if not isinstance(data['name'], str):
            raise web.HTTPBadRequest(text='Invalid name')
        m['name'] = data['name'].strip()[:40]
    await commit_machine(mac, m, request.app)
    if notify_host:
        ws = host_ctrl.get(mac)
        if ws is not None and not ws.closed:
            try: await ws.send_json({'type': 'set_code', 'code': data['code']})
            except Exception: pass
    return web.json_response({'ok': True})


@serialized
async def api_invite(request):
    if not check_auth(request):
        return need_auth()
    await mutation_data(request, require_mac=False)
    now = time.time()
    pending = {k: v for k, v in invites.items() if v['expires_at'] > now}
    if len(pending) >= MAX_INVITES:
        raise web.HTTPServiceUnavailable(text='Invite capacity reached')
    code = secrets.token_hex(16)
    pending[hashlib.sha256(code.encode()).hexdigest()] = {'expires_at': now + 900}
    await persist(request.app, machines, pending)
    invites.clear()
    invites.update(pending)
    return web.json_response({'pairing_code': code, 'expires_in': 900})


async def api_pair(request):
    peer = transport_peer(request)
    state = request.app[SECURITY]
    if not attempt(request.app, peer):
        raise web.HTTPTooManyRequests(text='Pairing failed')
    if (state['pair_reads'] >= MAX_PAIR_READS
            or state['pair_peers'].get(peer, 0) >= MAX_PAIR_READS_PER_IP):
        raise web.HTTPTooManyRequests(text='Pairing busy')
    state['pair_reads'] += 1
    state['pair_peers'][peer] = state['pair_peers'].get(peer, 0) + 1
    try:
        async with asyncio.timeout(5):
            data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='Invalid JSON') from None
    finally:
        state['pair_reads'] -= 1
        state['pair_peers'][peer] -= 1
        if not state['pair_peers'][peer]:
            del state['pair_peers'][peer]
    if (not isinstance(data, dict) or not valid_hex(data.get('pairing_code'), 32)
            or not valid_mac(data.get('mac')) or not valid_hex(data.get('host_token'), 64)
            or not isinstance(data.get('name'), str) or len(data['name']) > 80):
        raise web.HTTPBadRequest(text='Invalid pairing request')
    return await consume_invite(request, data)


@serialized
async def consume_invite(request, data):
    key = hashlib.sha256(data['pairing_code'].lower().encode()).hexdigest()
    invite = invites.get(key)
    if invite is None or invite['expires_at'] <= time.time():
        raise web.HTTPUnauthorized(text='Pairing failed')
    mac = data['mac'].upper()
    digest = hashlib.sha256(data['host_token'].encode()).hexdigest()
    if 'mac' in invite:
        if (invite['mac'] != mac or not hmac.compare_digest(invite['host_token_hash'], digest)
                or not hmac.compare_digest(machines.get(mac, {}).get('host_token_hash', ''), digest)):
            raise web.HTTPUnauthorized(text='Pairing failed')
        return web.json_response({'ok': True, 'code': invite['code']})
    if mac in machines:
        raise web.HTTPConflict(text='Machine already enrolled')
    if len(machines) >= MAX_MACHINES:
        raise web.HTTPServiceUnavailable(text='Registry capacity reached')
    code = gen_code()
    m = {'name': data['name'].strip() or mac, 'code': code, 'desired_code': '', 'host_token_hash': digest}
    pending = {**invites, key: {**invite, 'mac': mac, 'host_token_hash': digest, 'code': code}}
    await persist(request.app, {**machines, mac: m}, pending)
    machines[mac] = m
    invites.clear()
    invites.update(pending)
    return web.json_response({'ok': True, 'code': code})


async def api_wake(request):
    if not check_auth(request):
        return need_auth()
    data = await mutation_data(request)
    mac = data['mac'].upper()
    sent = 0
    for ws in list(host_ctrl.values()):
        if ws is None or ws.closed:
            continue
        try:
            await ws.send_json({'type': 'wake', 'mac': mac})
            sent += 1
        except Exception:
            pass
    return web.json_response({'ok': True, 'asked': sent})

async def dashboard(request):
    if not check_auth(request):
        return need_auth()
    return web.Response(text=DASHBOARD_HTML, content_type='text/html', charset='utf-8')


DASHBOARD_HTML = r"""<!doctype html>
<html lang="he" dir="rtl"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ניהול חיבורים מרחוק</title>
<style>
 :root{--bg:#0f1420;--card:#1a2234;--line:#283347;--txt:#e6edf6;--mut:#8b9ab0;--grn:#2ecc71;--blue:#2d7ff9}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--txt);font-family:Segoe UI,Arial,sans-serif}
 header{background:#131a28;padding:16px 20px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between}
 h1{font-size:18px;margin:0}.sub{color:var(--mut);font-size:12px;margin-top:3px}
 .wrap{padding:16px;max-width:1000px;margin:0 auto}.grid{display:grid;gap:12px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
 .row{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
 .dot{width:10px;height:10px;border-radius:50%}
 .on{background:var(--grn);box-shadow:0 0 8px var(--grn)}.off{background:#555}
 .name{font-size:16px;font-weight:600}
 .meta{color:var(--mut);font-size:12px;font-family:Consolas,monospace}
 .code{font-family:Consolas,monospace;font-size:22px;font-weight:bold;color:#4fd1ff;letter-spacing:2px}
 input{background:#0d1320;border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:7px 10px;font-size:14px}
 input.codein{width:110px;font-family:Consolas,monospace;font-size:16px;text-align:center}
 button{background:var(--blue);color:#fff;border:0;border-radius:8px;padding:7px 14px;cursor:pointer;font-size:13px;font-weight:600}
 button.ghost{background:#26334a}button.wake{background:#8e44ad}.spacer{flex:1}
 .empty{color:var(--mut);text-align:center;padding:40px}
 summary{cursor:pointer;padding:8px 0;color:var(--mut)}.meta,.code{unicode-bidi:plaintext}
 [role=status]{font-size:14px;margin:6px 0}button:disabled{opacity:.55;cursor:default}
 .pill{font-size:11px;padding:2px 8px;border-radius:20px;background:#26334a;color:var(--mut)}
 .pill.onl{background:rgba(46,204,113,.15);color:var(--grn)}
</style></head><body>
<header><div><h1>ניהול חיבורים מרחוק</h1></div>
<div id="count" class="pill"></div></header>
<div class="wrap"><div id="loadStatus" role="status"></div><div id="list" class="grid"><div class="empty">טוען…</div></div></div>
<script>
function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=String(text);return n;}
function button(parent,label,action,cls){const b=el('button',cls,label);b.addEventListener('click',async()=>{b.disabled=true;try{await action();}catch{alert('הפעולה נכשלה');}finally{b.disabled=false;}});parent.append(b);return b;}
async function post(path,data){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'RemoteDesktop'},body:JSON.stringify(data)});if(!r.ok)throw new Error('Request failed');return r.json();}
let loading=false, retryAt=0;
async function load(){
 if(loading||document.hidden||Date.now()<retryAt)return;
 loading=true;
 const status=document.getElementById('loadStatus');
 try{
  const r=await fetch('/api/machines',{cache:'no-store'});
  if(r.status===429){retryAt=Date.now()+60000;status.textContent='הרענון הושהה לדקה. המצב המוצג עשוי להשתנות.';return;}
  if(!r.ok)throw new Error();
  const d=await r.json();status.textContent='';
  if(!document.querySelector('#list input:focus'))render(d.machines,d.now);
 }catch{status.textContent='אין עדכון מהשרת. המצב המוצג הוא מהבדיקה האחרונה.';}
 finally{loading=false;}
}
function ago(ts,now){const s=now-ts;if(s<60)return s+' שניות';if(s<3600)return Math.floor(s/60)+' דק׳';return Math.floor(s/3600)+' שע׳';}
async function enroll(mac){const d=await post('/api/enroll',{mac});prompt('מפתח Host חדש (מוצג פעם אחת):',d.host_token);load();}
const enrollBar=el('div','row');enrollBar.style.marginBottom='12px';
const macInput=el('input');macInput.placeholder='AA:BB:CC:DD:EE:FF';macInput.maxLength=17;
enrollBar.append(macInput);button(enrollBar,'רישום מחשב',()=>enroll(macInput.value.trim()));
const manual=el('details');manual.append(el('summary','','רישום ידני לפי MAC'),enrollBar);
document.querySelector('.wrap').prepend(manual);
const inviteBar=el('div','row');inviteBar.style.marginBottom='12px';
const inviteCode=el('input');inviteCode.readOnly=true;inviteCode.dir='ltr';inviteCode.setAttribute('aria-label','קוד צימוד');inviteCode.style.maxWidth='100%';inviteCode.style.width='320px';
const inviteStatus=el('span','sub');inviteStatus.setAttribute('role','status');
button(inviteBar,'הוספת מחשב',async()=>{const d=await post('/api/invite',{});inviteCode.hidden=false;copyInvite.hidden=false;inviteCode.value=d.pairing_code;inviteStatus.textContent='הדביקו את הקוד ב־Host במחשב החדש ובחרו סיסמת גישה. הקוד תקף ל־15 דקות.';});
inviteCode.hidden=true;inviteBar.append(inviteCode);
const copyInvite=button(inviteBar,'העתק קוד צימוד',async()=>{try{await navigator.clipboard.writeText(inviteCode.value);}catch{inviteCode.select();inviteStatus.textContent='הקוד מסומן. לחצו Ctrl+C להעתקה.';return;}inviteStatus.textContent='הקוד הועתק. הדביקו אותו ב־Host במחשב החדש.';},'ghost');
copyInvite.hidden=true;
inviteBar.append(inviteStatus);document.querySelector('.wrap').prepend(inviteBar);
function render(ms,now){
 const list=document.getElementById('list');
 const expanded=new Set(Array.from(list.querySelectorAll('details[open]'),item=>item.dataset.mac));
 list.replaceChildren();
 document.getElementById('count').textContent=ms.filter(m=>m.online).length+' מחוברים / '+ms.length+' סה״כ';
 if(!ms.length){list.append(el('div','empty','אין מחשבים רשומים עדיין.'));return;}
 for(const m of ms){
  const card=el('div','card'),row=el('div','row'),info=el('div');
  info.append(el('div','name',m.name||m.mac),el('div','meta',m.mac+' · v'+m.version+' · '+m.ip));
  const state=m.in_use?'בשימוש':m.online?'מוכן לחיבור':m.last_seen?'אופליין · '+ago(m.last_seen,now):'טרם התחבר';
  row.append(el('div','dot '+(m.online?'on':'off')),info,el('div','spacer'),el('span','pill '+(m.online?'onl':''),state));
  const controls=el('div','row');controls.style.marginTop='12px';
  const current=el('div');current.append(el('div','meta','קוד נוכחי'),el('div','code',m.code||'------'));
  const code=el('input','codein');code.placeholder='קוד קבוע';code.value=m.desired_code||'';code.maxLength=6;
  controls.append(code);
  button(controls,'קבע קוד',async()=>{const v=code.value.trim();if(v&&!/^[0-9]{6}$/.test(v)){alert('קוד חייב 6 ספרות');return;}await post('/api/set',{mac:m.mac,code:v});load();});
  const name=el('input');name.placeholder='שם מחשב';name.value=m.name||'';name.style.width='140px';controls.append(name);
  button(controls,'שמור שם',async()=>{await post('/api/set',{mac:m.mac,name:name.value.trim()});load();},'ghost');
  if(!m.online)button(controls,'הדלק',async()=>{const d=await post('/api/wake',{mac:m.mac});alert('נשלח דרך '+d.asked+' מחשבים');},'wake');
  button(controls,'החלף מפתח',()=>{if(confirm('להחליף מפתח ולנתק את החיבור הקיים?'))return enroll(m.mac);},'ghost');
  const options=el('details');options.dataset.mac=m.mac;options.open=expanded.has(m.mac);
  options.append(el('summary','','אפשרויות מחשב'),controls);
  card.append(row,current,options);list.append(card);
 }
}
load();setInterval(load,10000);document.addEventListener('visibilitychange',load);
</script></body></html>"""


def make_app():
    app = web.Application(middlewares=[security_middleware], client_max_size=8192)
    app[SECURITY] = {'attempts': {}, 'dashboard_attempts': {}, 'ws_count': 0, 'ws_peers': {}, 'hash_count': 0,
                     'executor': ThreadPoolExecutor(max_workers=MAX_HASHES),
                     'state_lock': asyncio.Lock(), 'mutations': set(), 'state_failed': False, 'store': None,
                     'pair_reads': 0, 'pair_peers': {}}
    async def startup(app):
        url = os.environ.get('DATABASE_URL')
        if url:
            store = PostgresState(url)
            app[SECURITY]['store'] = store
            try:
                data = await store.open()
            except BaseException:
                app[SECURITY]['executor'].shutdown(wait=False, cancel_futures=True)
                raise
            machines.clear()
            machines.update(data['machines'])
            invites.clear()
            invites.update(data['invites'])
    async def cleanup(app):
        pending = list(app[SECURITY]['mutations'])
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if app[SECURITY]['store'] is not None:
            await app[SECURITY]['store'].close()
        app[SECURITY]['executor'].shutdown(wait=True, cancel_futures=True)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    app.router.add_get('/ws', ws_endpoint)
    app.router.add_get('/config.json', config_json)
    app.router.add_get('/healthz', healthz)
    app.router.add_get('/api/machines', api_machines)
    app.router.add_post('/api/set', api_set)
    app.router.add_post('/api/wake', api_wake)
    app.router.add_post('/api/enroll', api_enroll)
    app.router.add_post('/api/invite', api_invite)
    app.router.add_post('/api/pair', api_pair)
    app.router.add_get('/', dashboard)
    app.router.add_get('/dashboard', dashboard)
    return app


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Remote Desktop relay')
    parser.add_argument('--init-db', action='store_true', help='Initialize DATABASE_URL without starting the relay')
    args = parser.parse_args()
    if args.init_db:
        if not os.environ.get('DATABASE_URL'):
            parser.error('DATABASE_URL is required for --init-db')
        async def initialize_database():
            store = PostgresState(os.environ['DATABASE_URL'])
            try:
                await store.open(initialize=True)
            finally:
                await store.close()
        try:
            asyncio.run(initialize_database())
        except DatabaseStartupError as exc:
            raise SystemExit(str(exc)) from None
        except Exception:
            raise SystemExit('Database initialization failed; existing state was not reset') from None
        print('Database initialized')
        raise SystemExit(0)
    if not os.environ.get('DATABASE_URL'):
        load_state()
    port = int(os.environ.get('PORT', 8765))
    print(f"[relay] aiohttp on :{port}  (dashboard pw via DASHBOARD_PASSWORD)")
    web.run_app(make_app(), host='0.0.0.0', port=port, print=None)
