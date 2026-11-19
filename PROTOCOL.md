# Protocol 2 (Remote Desktop 2.0.0)

Version 2.1.0 retains protocol 2 screen/input frames and authentication. It adds
one-time HTTPS pairing; existing manually enrolled protocol-2 clients still work.

## First-Run Pairing (2.1.0)

POST /api/invite: administrator Basic Auth and X-Requested-With: RemoteDesktop,
same-origin checks, empty JSON object. Returns {pairing_code, expires_in:900}.
The code is 16 random bytes encoded as 32 lowercase hex characters. Treat it as
a temporary secret; relay persists only its SHA256 hash, expiry and redemption.

POST /api/pair: verified HTTPS, JSON {pairing_code, mac, name, host_token}.
Host generates its 64-hex token locally and persists its DPAPI-protected token
and salted password verifier BEFORE this request. Neither password nor verifier
is sent in pairing. Success: {ok:true, code:"123456"}. Existing enrolled MACs
cannot be replaced. Same invite/MAC/token retries are idempotent until expiry;
another identity or expired invite fails. Attempts and pending invites are bounded.
The desktop refuses redirects and oversized or malformed responses.

The invite hash and pending encrypted token permit safe retry after response loss.
Resetting unfinished setup requires explicit confirmation: the server may have
already enrolled the MAC, requiring administrator recovery instead of a new invite.
Keep operator passwords distinct from pairing codes and six-digit computer codes.

GET /healthz is a non-secret readiness check, subject to the same TLS policy.
It does not query the database or expose registry information.

Protocol 1 is rejected. Update relay, Host and Client together. The relay is
trusted with passwords and session data. WSS protects transport, not against
the relay administrator; this is not end-to-end encryption.

## Authentication Before Upgrade

GET /ws uses X-RemoteDesktop-Auth containing base64-encoded UTF-8 JSON (decoded
limit 4096 bytes). Base64 is not encryption: TLS is required. Never log this
header at a reverse proxy. No initial WebSocket JSON handshake is sent.
Credentials are checked before upgrade, preventing unauthenticated sockets from
buffering screen-sized messages.

Host object:
```
{role:"host", protocol:2, mac, host_token, password_salt, password_hash,
 allow_files:false, name, version, preferred_code}
```
mac is a colon-separated six-byte address enrolled by an administrator.
host_token is 32 random bytes encoded as 64 hex characters; the registry stores
SHA256 of that ASCII string. password_salt is 16 bytes (32 hex); password_hash is
PBKDF2-HMAC-SHA256(password UTF-8, salt, 600000), 32 bytes (64 hex). Host stores
only the verifier and a current-user DPAPI-protected identity token.

Client object:
```
{role:"client", protocol:2, code:"654321", password:"operator passphrase"}
```
Code is an identifier, not authorization. Password: 16-128 characters, at most
512 UTF-8 bytes; Client does not persist it. Incorrect credentials, legacy
protocol, wrong Host key and busy sessions fail authentication.

Host receives `{type:"code", protocol:2, code}`. Both peers receive
`{type:"connected", protocol:2, allow_files}` after password verification.
Desktop programs reject successful replies missing protocol 2.

## Control Messages

Relay to Host: set_code with code (six ASCII digits or empty), wake with mac,
and disconnected. Host saves set_code and reconnects even during a session.
Dashboard desired_code takes priority. Client text is never forwarded to Host.
After authenticating a Host, the relay ignores a locally requested code that is
reserved to another offline machine. A claim on another live session still fails.
The relay reuses the Host's own assignment or allocates
a free code. An administrator reservation remains authoritative.
Confirming the current code on the dashboard keeps an active session connected.
Host to Client: `{type:"file_result", ok:true, name}` after committed file receipt.

## Binary Frames

All integers big-endian. Each message is exactly `[type:u8][length:u32][payload]`.
Trailing/truncated bytes are invalid. Authenticated WebSocket maximum: 12 MiB.

| Type | Payload |
|------|---------|
| 0x01 SCREEN | JPEG, <=8192 per dimension, <=16 Mi pixels |
| 0x02 MOUSE | action:u8, x:i32, y:i32, button:u8 (10 bytes) |
| 0x03 KEY | action:u8, strict UTF-8 key/text |
| 0x05 FILE_META | JSON object: name, integer size |
| 0x06 FILE_DATA | name_length:u16, UTF-8 name, chunk (1-65536 bytes) |
| 0x07 FILE_DONE | UTF-8 name |
| 0x08 TILES | width:u16,height:u16,tile_width:u16,tile_height:u16,count:u16; then count records: column:u8,row:u8,JPEG_length:u32,JPEG |

Mouse: 0 move, 1 down, 2 up, 3 click, 4 double click, 5 scroll. Buttons 0/1/2:
left/middle/right. Scroll is signed int8 in button, limited to +/-10. Coordinates
limited to +/-16384. Key actions 0/1 release/press an allowlisted key; 2 enters
printable text (<=32 characters/128 bytes). Held keys/buttons release on disconnect.
Tiles: <=1024 records; dimensions, bounds, duplicate positions, decoded pixel budget
and exact length validated before compositing. Screen callbacks are awaited.

## File Policy

Disabled by default. One pending transfer, 100 MiB cumulative declared bytes,
100 files per session. Metadata <=2048 bytes; name <=240 UTF-8 bytes. No Windows
reserved names, separators, ADS colons, control characters or trailing dot/space.
Received size must match metadata. Partial files removed on error/disconnect;
atomic exclusive linking prevents overwrites. No automatic execution. Invalid
transfers terminate the session; Client awaits each chunk send.

## Dashboard API

Private routes require Basic Auth/HTTPS, DASHBOARD_PASSWORD >=16 characters.
GET /api/machines never returns tokens or password verifiers.
Each machine has an `in_use` boolean for a connected operator, separate from
`online` for a registered Host. Dashboard quota exhaustion returns HTTP 429
and a Retry-After header. Desktop and dashboard request quotas are independent.
POST /api/enroll JSON {mac}: returns {ok:true,mac,host_token} once, revokes old pairs.
POST /api/set JSON {mac,code?,name?}; POST /api/wake JSON {mac}.
Mutations require X-Requested-With: RemoteDesktop and same-origin requests.
No CORS or GET mutations.
