# Remote Desktop 2.1.0

This branch is the relay-only deployment subset. Desktop source and installer
builds remain in the local remote_desktop_FULL workspace. Run relay tests here
with `python -m unittest discover`; desktop build commands below refer to that
full workspace. Deploy this branch only to my-relay-1, not the older second service.

Self-hosted Windows remote desktop through a trusted aiohttp relay. Unattended
access after one-time pairing: an expiring administrator-issued pairing code
provisions a per-machine Host key, plus an operator-selected password.
Six-digit codes are identifiers, not passwords. Update all three components together.

Read INSTALL_GUIDE.md for setup, ARCHITECTURE.md for trust boundaries and
PROTOCOL.md for the wire contract.

- relay_server.py: HTTPS/WebSocket backend and administration dashboard.
- host.py: Windows screen/input receiver, optional startup and file reception.
- client.py: operator viewer, password prompt and bounded input/file sender.
- build.bat: local build into dist-security; no automatic Python installer.
- build-installer.ps1: per-user Windows setup into dist-setup (Inno Setup).

The Host first-run screen is in Hebrew. Normal use shows the computer code and
connection status; technical settings are collapsed. Startup is explicit opt-in.
The installer supplies both programs, shortcuts and uninstall without admin.
Uninstall preserves stored settings and received files.

Set DATABASE_URL for a verified-TLS PostgreSQL registry on Render Free (for
example Neon Free), and initialize once with `python relay_server.py --init-db`.
Normal startup never initializes missing database state. File mode still needs
durable storage. No paid resources are required; free-provider quotas and cold
starts still apply. The database stores machine metadata and hashed credentials,
not screen data or the operator's plaintext password.

Build with Python 3.11+ and requirements-desktop.txt; relay uses requirements.txt.
Test: `.venv-build/Scripts/python.exe -m unittest discover -v`.
Additional test-only dependencies are in requirements-test.txt. Real PostgreSQL
coverage uses TEST_DATABASE_URL pointed at a separate disposable test database.

Default URL: wss://my-relay-1.onrender.com. Live deployment status is not verified
by this tree. New applications cannot use an old relay. No downloads are published
automatically, and local builds are not antivirus clearance.
