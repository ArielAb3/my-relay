#!/usr/bin/env python3
"""
Relay Server + Management Dashboard  (aiohttp)

One port serves everything (Render provides $PORT):
  GET  /ws            -> WebSocket relay (Host <-> Client) and Host control channel
  GET  /config.json   -> public config hosts poll (relay url, version, per-MAC codes)
  GET  /              -> password-protected dashboard (HTML)
  GET  /api/machines  -> JSON list of machines (auth)
  GET  /api/set       -> set a machine's desired code / name (auth)

Dashboard password: env DASHBOARD_PASSWORD (default 'admin1234').
"""

import asyncio
import json
import os
import time
import random
import string
import base64

from aiohttp import web, WSMsgType

DASHBOARD_PASSWORD = os.environ.get('DASHBOARD_PASSWORD', 'admin1234')
STATE_FILE = os.environ.get('STATE_FILE', '/tmp/rdp_state.json')
LATEST_VERSION = os.environ.get('LATEST_VERSION', '')
HOST_URL = os.environ.get('HOST_URL', '')
PUBLIC_RELAY = os.environ.get('PUBLIC_RELAY', '')

# ---- state ----
sessions = {}    # code -> {'host': ws, 'client': ws}
machines = {}    # mac  -> {name, code, desired_code, online, version, last_seen, ip}
host_ctrl = {}   # mac  -> host control ws (to push set_code)


def load_state():
    global machines
    try:
        with open(STATE_FILE) as f:
            machines = json.load(f).get('machines', {})
    except Exception:
        machines = {}

def save_state():
    try:
        clean = {mac: {k: m.get(k, '') for k in ('name', 'desired_code')}
                 for mac, m in machines.items()}
        with open(STATE_FILE, 'w') as f:
            json.dump({'machines': clean}, f)
    except Exception:
        pass

def gen_code(preferred=None):
    if preferred and len(preferred) == 6 and preferred.isdigit():
        if preferred in sessions and sessions[preferred].get('host') is None:
            del sessions[preferred]
        if preferred not in sessions:
            return preferred
    while True:
        c = ''.join(random.choices(string.digits, k=6))
        if c not in sessions:
            return c

def mark_stale():
    now = time.time()
    for m in machines.values():
        if m.get('online') and now - m.get('last_seen', 0) > 25:
            m['online'] = False


# ============================================================
#  WebSocket endpoint
# ============================================================
async def ws_endpoint(request):
    ws = web.WebSocketResponse(max_msg_size=12 * 1024 * 1024, heartbeat=30)
    await ws.prepare(request)

    role = code = mac = None
    peer_ip = request.headers.get('X-Forwarded-For', request.remote or '?').split(',')[0].strip()

    try:
        # First message = handshake (JSON text)
        first = await ws.receive(timeout=20)
        if first.type != WSMsgType.TEXT:
            await ws.close()
            return ws
        msg = json.loads(first.data)
        role = msg.get('role')
        code = msg.get('code')

        # ---------------- HOST ----------------
        if role == 'host':
            mac = (msg.get('mac') or '').upper()
            name = msg.get('name') or mac or 'Unknown'
            version = msg.get('version') or '?'

            desired = machines.get(mac, {}).get('desired_code', '')
            preferred = desired or (msg.get('preferred_code') or '').strip()
            code = gen_code(preferred)
            sessions[code] = {'host': ws, 'client': None}

            m = machines.get(mac, {})
            m.update({'name': m.get('name') or name, 'code': code,
                      'desired_code': m.get('desired_code', ''),
                      'online': True, 'version': version,
                      'last_seen': time.time(), 'ip': peer_ip})
            machines[mac] = m
            host_ctrl[mac] = ws
            save_state()

            await ws.send_str(json.dumps({'type': 'code', 'code': code}))
            print(f"[relay] HOST {name} ({mac}) code={code} v{version}")

            # Relay loop: forward host->client; also accept control messages
            async for raw in ws:
                if mac in machines:
                    machines[mac]['last_seen'] = time.time()
                    machines[mac]['online'] = True
                if raw.type == WSMsgType.BINARY:
                    cl = sessions.get(code, {}).get('client')
                    if cl is not None and not cl.closed:
                        try: await cl.send_bytes(raw.data)
                        except Exception: pass
                elif raw.type == WSMsgType.TEXT:
                    # host may send control/status JSON; forward to client if present
                    cl = sessions.get(code, {}).get('client')
                    if cl is not None and not cl.closed:
                        try: await cl.send_str(raw.data)
                        except Exception: pass
                elif raw.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break

        # ---------------- CLIENT ----------------
        elif role == 'client':
            if not code or code not in sessions or sessions[code].get('host') is None:
                await ws.send_str(json.dumps({'type': 'error', 'msg': 'Invalid code'}))
                await ws.close(); return ws
            if sessions[code].get('client') is not None:
                await ws.send_str(json.dumps({'type': 'error', 'msg': 'Session busy'}))
                await ws.close(); return ws

            sessions[code]['client'] = ws
            host_ws = sessions[code]['host']
            await ws.send_str(json.dumps({'type': 'connected'}))
            try:
                await host_ws.send_str(json.dumps({'type': 'connected'}))
            except Exception:
                pass

            async for raw in ws:
                if raw.type == WSMsgType.BINARY:
                    if not host_ws.closed:
                        try: await host_ws.send_bytes(raw.data)
                        except Exception: break
                elif raw.type == WSMsgType.TEXT:
                    if not host_ws.closed:
                        try: await host_ws.send_str(raw.data)
                        except Exception: break
                elif raw.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break

    except Exception as e:
        print(f"[!] ws error: {e}")
    finally:
        if code and code in sessions:
            is_host = sessions[code].get('host') is ws
            other = sessions[code].get('client' if is_host else 'host')
            if other is not None and not other.closed:
                try: await other.send_str(json.dumps({'type': 'disconnected'}))
                except Exception: pass
            if is_host:
                sessions.pop(code, None)
            else:
                if code in sessions:
                    sessions[code]['client'] = None
        if mac and mac in machines:
            machines[mac]['online'] = False
            machines[mac]['last_seen'] = time.time()
            if host_ctrl.get(mac) is ws:
                host_ctrl.pop(mac, None)
    return ws


# ============================================================
#  HTTP endpoints
# ============================================================
def check_auth(request):
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Basic '):
        return False
    try:
        _, pw = base64.b64decode(auth[6:]).decode().split(':', 1)
        return pw == DASHBOARD_PASSWORD
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
        'codes': {mac: m.get('desired_code', '')
                  for mac, m in machines.items() if m.get('desired_code')},
    }
    return web.json_response(cfg)

async def api_machines(request):
    if not check_auth(request):
        return need_auth()
    mark_stale()
    now = int(time.time())
    out = [{
        'mac': mac, 'name': m.get('name', ''), 'code': m.get('code', ''),
        'desired_code': m.get('desired_code', ''), 'online': bool(m.get('online')),
        'version': m.get('version', '?'), 'ip': m.get('ip', '?'),
        'last_seen': int(m.get('last_seen', 0)),
    } for mac, m in machines.items()]
    out.sort(key=lambda x: (not x['online'], x['name'] or x['mac']))
    return web.json_response({'machines': out, 'now': now})

async def api_set(request):
    if not check_auth(request):
        return need_auth()
    mac = (request.query.get('mac') or '').upper()
    if not mac:
        return web.json_response({'ok': False, 'err': 'no mac'}, status=400)
    m = machines.setdefault(mac, {'name': mac, 'code': '', 'desired_code': '',
                                  'online': False, 'version': '?', 'last_seen': 0, 'ip': '?'})
    if 'code' in request.query:
        nc = request.query['code'].strip()
        if nc == '' or (len(nc) == 6 and nc.isdigit()):
            m['desired_code'] = nc
            ws = host_ctrl.get(mac)
            if ws is not None and not ws.closed:
                try: await ws.send_str(json.dumps({'type': 'set_code', 'code': nc}))
                except Exception: pass
    if 'name' in request.query:
        m['name'] = request.query['name'].strip()[:40]
    save_state()
    return web.json_response({'ok': True})

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
 button.ghost{background:#26334a}.spacer{flex:1}
 .empty{color:var(--mut);text-align:center;padding:40px}
 .pill{font-size:11px;padding:2px 8px;border-radius:20px;background:#26334a;color:var(--mut)}
 .pill.onl{background:rgba(46,204,113,.15);color:var(--grn)}
</style></head><body>
<header><div><h1>🖥️ ניהול חיבורים מרחוק</h1><div class="sub">רענון אוטומטי כל 3 שניות</div></div>
<div id="count" class="pill"></div></header>
<div class="wrap"><div id="list" class="grid"><div class="empty">טוען…</div></div></div>
<script>
async function load(){try{const r=await fetch('/api/machines',{cache:'no-store'});const d=await r.json();render(d.machines,d.now);}catch(e){document.getElementById('list').innerHTML='<div class="empty">שגיאת חיבור</div>';}}
function ago(ts,now){const s=now-ts;if(s<60)return s+' שניות';if(s<3600)return Math.floor(s/60)+' דק׳';return Math.floor(s/3600)+' שע׳';}
function esc(s){return(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function render(ms,now){const on=ms.filter(m=>m.online).length;
 document.getElementById('count').textContent=on+' מחוברים / '+ms.length+' סה״כ';
 if(!ms.length){document.getElementById('list').innerHTML='<div class="empty">אין מחשבים רשומים עדיין.<br>פתח Host במחשב כלשהו והוא יופיע כאן.</div>';return;}
 document.getElementById('list').innerHTML=ms.map(m=>`<div class="card">
 <div class="row"><div class="dot ${m.online?'on':'off'}"></div>
 <div><div class="name">${esc(m.name||m.mac)}</div><div class="meta">${m.mac} · v${m.version} · ${m.ip}</div></div>
 <div class="spacer"></div><span class="pill ${m.online?'onl':''}">${m.online?'אונליין':'אופליין · '+ago(m.last_seen,now)}</span></div>
 <div class="row" style="margin-top:12px"><div><div class="meta">קוד נוכחי</div><div class="code">${m.code||'------'}</div></div>
 <div class="spacer"></div>
 <input class="codein" id="code_${m.mac}" placeholder="קוד קבוע" value="${m.desired_code||''}" maxlength="6">
 <button onclick="setCode('${m.mac}')">קבע קוד</button>
 <input id="name_${m.mac}" placeholder="שם מחשב" value="${esc(m.name||'')}" style="width:140px">
 <button class="ghost" onclick="setName('${m.mac}')">שמור שם</button></div></div>`).join('');}
async function setCode(mac){const v=document.getElementById('code_'+mac).value.trim();
 if(v&&!/^\d{6}$/.test(v)){alert('קוד חייב 6 ספרות (או ריק = רנדומי)');return;}
 await fetch('/api/set?mac='+encodeURIComponent(mac)+'&code='+encodeURIComponent(v));load();}
async function setName(mac){const v=document.getElementById('name_'+mac).value.trim();
 await fetch('/api/set?mac='+encodeURIComponent(mac)+'&name='+encodeURIComponent(v));load();}
load();setInterval(load,3000);
</script></body></html>"""


def make_app():
    app = web.Application()
    app.router.add_get('/ws', ws_endpoint)
    app.router.add_get('/config.json', config_json)
    app.router.add_get('/api/machines', api_machines)
    app.router.add_get('/api/set', api_set)
    app.router.add_get('/', dashboard)
    app.router.add_get('/dashboard', dashboard)
    return app


if __name__ == '__main__':
    load_state()
    port = int(os.environ.get('PORT', 8765))
    print(f"[relay] aiohttp on :{port}  (dashboard pw via DASHBOARD_PASSWORD)")
    web.run_app(make_app(), host='0.0.0.0', port=port, print=None)
