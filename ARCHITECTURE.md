# Architecture and Security Boundaries

## Mobile access

The Safari client is served from the relay's own origin in `mobile/`. All assets are local. The home-screen manifest opens the same HTTPS page. The client clears the passphrase after login and does not cache desktop frames or retain login state between visits. Desktop and phone share the bounded password verifier. A browser ticket pins a particular Host socket and remains in volatile relay memory. Each mobile connection takes the normal single-client slot. A visible Host dialog gates screen capture and input, stays open with a Stop control, and is dismissed on disconnect. Old Hosts cannot receive phone sessions.

Three scripts: relay_server.py (aiohttp), host.py (Windows tkinter/capture/input),
client.py (Windows tkinter/viewer/input). Protocol 2 authenticates before WebSocket
upgrade. See PROTOCOL.md for fields and INSTALL_GUIDE.md for migration.

## Trust Model

Administrator issues a 128-bit one-time pairing code valid for 15 minutes.
Host generates a random per-machine token and saves it with DPAPI before
redeeming the code over verified HTTPS. Pairing never replaces an existing MAC.
Repeating the same invite/MAC/token is idempotent until expiry; other reuse fails.
Manual administrator MAC enrollment/token rotation remains available.
MAC is only a lookup key. Relay stores the token hash in its durable registry and checks
the token over TLS. Host protects it with Windows current-user DPAPI. A separate
access passphrase is stored as a salted PBKDF2-SHA256 verifier (600000 iterations).
Authenticated Host provides that verifier to relay for its live session.
Passwords are checked off-loop with four bounded hash workers. Old versions and
unconfigured Hosts fail closed. Unattended access needs a logged-in Windows user;
this is not a system service and cannot promise secure-desktop access.

Relay and OS account are trusted. Compromised relay can view data/issue controls;
same-user processes can decrypt DPAPI credentials. This is not end-to-end encryption
or protection from local administrators. Python cannot reliably zeroize strings.

## Concurrency and Persistence

Host has one network thread and an iterative reconnect loop. tkinter changes use
root.after. Settings use atomic replacement; a lock/revision prevents old handshakes
restoring previous credentials. Saving closes the current connection on its loop.
Screen capture/encoding stays in an executor. File chunks run off-loop with cleanup.
Held keys/buttons release on disconnect.

Client prevents parallel workers and ignores completion from old sockets. A bounded
sender coalesces motion and orders keys/buttons. File disk work is off-loop and
sends are sequential. Screen decoding validates allocations and bounds callback
backlog. Automatic operator reconnection is not implemented: re-enter the password.

DATABASE_URL selects a single-row PostgreSQL JSON registry with revision CAS.
Database calls run in a bounded single-worker executor with verified TLS and
short-lived connections, avoiding idle leases and polling. Only changed durable
snapshots write. File mode remains supported. Invites store hashes, not raw codes.
The explicit --init-db command creates an empty registry, not a STATE_FILE migration.
Missing or invalid existing state is never overwritten by initialization.
An ambiguous commit/outage poisons the writer and revokes sessions; healthz then
fails, allowing the service supervisor to restart and reload durable state.
CAS prevents stale writes, not distributed live-session consistency. Use one relay
service/process per database; never point the old second service at this registry.

Relay membership uses exact socket/session objects. Password checks revalidate
the session after async work; superseded sockets cannot disconnect replacements.
Token rotation revokes pairs. Codes stay reserved while offline. State uses atomic
replacement; corrupt/unreadable state fails startup. Use one relay process only;
live sessions and disk state are not multi-worker storage.

## Abuse Boundaries

TLS required. TRUST_PROXY=1 only behind a controlled HTTPS proxy that overwrites
X-Forwarded-Proto and prevents direct bypass. Forwarded IP is not trusted: users
behind one proxy share its transport-peer quota. ALLOW_INSECURE_LOCALHOST=1 permits
literal loopback for tests only. No certificate-validation bypasses.

Limits: 128 connection/auth slots, 16 per transport peer, four hashes, 60 attempts per transport peer,
10 per code, 240 global per 60 seconds, including successes. Dashboard requests
use independently bounded global and peer quotas. Polling pauses when the dashboard
is hidden and backs off for a minute on HTTP 429. This bounds work, not all
denial of service. Wider deployments need appropriate edge/network protection.

If directory durability fails after a state replacement, relay reconciles memory
to the written snapshot and revokes all pairs before yielding. The admin request
fails; repair storage and reissue any token not returned to its owner. The relay
does not continue trusting credentials that no longer match disk.

Dashboard mutations use POST JSON/custom header. Metadata uses DOM textContent,
value and events, not executable HTML interpolation. Responses use no-store,
frame denial, nosniff and script-hash CSP. No public code list.

Files are opt-in, bounded, path/device-name validated, temporary then exclusively
committed without overwrite. No automatic execution or executable self-update.
An authenticated operator has full desktop control: grant access only to trusted people.

## Verification and Release

Tests cover authentication, impersonation, rotation, persistence errors, races,
backpressure, DPAPI, file limits and malformed images. They do not certify malware
absence or replace real two-machine display/input tests. EXEs are unsigned unless
separately signed. Builds do not deploy a live server, upload files or enroll users.

References: https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
and https://learn.microsoft.com/en-us/windows/win32/seccrypto/example-c-program-using-cryptprotectdata
