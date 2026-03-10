#!/usr/bin/env python3
"""
Relay Server - runs on free cloud hosting (Render / Railway / fly.io)
Pairs a HOST (the controlled machine) with a CLIENT (the controller)
via a 6-digit session code.
"""

import asyncio
import websockets
import json
import random
import string
import time
import os

# session_code -> {'host': ws, 'client': ws, 'created': timestamp}
sessions = {}

def generate_code():
    while True:
        code = ''.join(random.choices(string.digits, k=6))
        if code not in sessions:
            return code

async def cleanup_old_sessions():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        dead = [c for c, s in sessions.items() if now - s['created'] > 3600]
        for c in dead:
            del sessions[c]
        if dead:
            print(f"[relay] Cleaned {len(dead)} expired sessions")

async def handler(websocket):
    remote = websocket.remote_address
    print(f"[+] Connection from {remote}")
    role = None
    code = None

    try:
        # First message must be a JSON handshake
        raw = await asyncio.wait_for(websocket.recv(), timeout=15)
        msg = json.loads(raw)
        role = msg.get('role')   # 'host' or 'client'
        code = msg.get('code')   # client provides code; host gets one assigned

        if role == 'host':
            code = generate_code()
            sessions[code] = {
                'host': websocket,
                'client': None,
                'created': time.time()
            }
            await websocket.send(json.dumps({'type': 'code', 'code': code}))
            print(f"[relay] Host registered with code {code}")

            # Wait for a client to join
            for _ in range(600):   # wait up to 10 minutes
                await asyncio.sleep(1)
                if sessions.get(code, {}).get('client'):
                    break
            else:
                await websocket.send(json.dumps({'type': 'error', 'msg': 'Timeout waiting for client'}))
                return

            await websocket.send(json.dumps({'type': 'connected'}))
            print(f"[relay] Client joined session {code}")

            # Relay loop: host -> client
            client_ws = sessions[code]['client']
            async for data in websocket:
                try:
                    await client_ws.send(data)
                except Exception:
                    break

        elif role == 'client':
            if not code or code not in sessions:
                await websocket.send(json.dumps({'type': 'error', 'msg': 'Invalid code'}))
                return
            if sessions[code]['client'] is not None:
                await websocket.send(json.dumps({'type': 'error', 'msg': 'Session already taken'}))
                return

            sessions[code]['client'] = websocket
            host_ws = sessions[code]['host']
            await websocket.send(json.dumps({'type': 'connected'}))

            # Relay loop: client -> host
            async for data in websocket:
                try:
                    await host_ws.send(data)
                except Exception:
                    break

        else:
            await websocket.send(json.dumps({'type': 'error', 'msg': 'Unknown role'}))

    except asyncio.TimeoutError:
        pass
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        print(f"[!] Error: {e}")
    finally:
        print(f"[-] Disconnected {remote}")
        if code and code in sessions:
            # Notify the other side
            other = None
            if sessions[code].get('host') == websocket:
                other = sessions[code].get('client')
            elif sessions[code].get('client') == websocket:
                other = sessions[code].get('host')
            if other:
                try:
                    await other.send(json.dumps({'type': 'disconnected'}))
                except Exception:
                    pass
            if code in sessions:
                del sessions[code]


async def main():
    port = int(os.environ.get('PORT', 8765))
    print(f"[relay] Starting on port {port}")
    asyncio.create_task(cleanup_old_sessions())
    async with websockets.serve(handler, '0.0.0.0', port, max_size=10 * 1024 * 1024):
        print(f"[relay] Ready!")
        await asyncio.Future()  # run forever


if __name__ == '__main__':
    asyncio.run(main())
