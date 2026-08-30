# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository structure

ClipCascade is three independently-deployable components in one repo, joined only by a shared wire protocol and E2E crypto scheme — there is no shared build tooling or shared code between them:

- `ClipCascade_Desktop/` — Python client (Windows/macOS/Linux), with parallel Tkinter-GUI and terminal-CLI variants
- `ClipCascade_Server/ClipCascade_Backend/` — Spring Boot (Java 21) relay server + static/Thymeleaf dashboard
- `ClipCascade_Mobile/` — React Native client (Android only — no maintained iOS target)

## Commands

### Desktop (`ClipCascade_Desktop/`)
- Run: `python src/main.py` (from `ClipCascade_Desktop/`)
- Tests: `python -m pytest` (from `ClipCascade_Desktop/` — `pytest.ini` sets `pythonpath = src`, `testpaths = tests`)
- Single test: `python -m pytest tests/test_history_store.py::test_name`
- Lint: `ruff check src tests`
- Format: `black` / `isort` (configured in `src/pyproject.toml`, line-length 100)
- Install deps: pick the platform-specific file — `requirements_win.txt` / `requirements_mac.txt` / `requirements_linux_gui.txt` / `requirements_linux_cli.txt` — not all of them
- Package (Windows): `pyinstaller ClipCascade_win.spec` from `src/`; set `CLIPCASCADE_WITH_HISTORY_UI=1` first to bundle the on-demand PySide6 history UI (omit it and the build stays at the smaller baseline size)

### Server (`ClipCascade_Server/ClipCascade_Backend/`)
- Build/test: `./mvnw test` (`mvnw.cmd test` on Windows)
- Run: `./mvnw spring-boot:run`
- Requires `CC_ADMIN_USERNAME`, `CC_ADMIN_PASSWORD`, `CC_SERVER_DB_PASSWORD` env vars — startup fails without them (see Architecture)
- Docker: compose files live in `ClipCascade_Server/docker-compose/` (`docker-compose.yml` plus variants for an external STOMP broker, multi-user, and large-transfer configs) — copy `docker-compose/.env.example` first

### Mobile (`ClipCascade_Mobile/src/`)
- Install: `npm install`
- Run: `npm run android`
- Tests: `npm test` (Jest)
- Lint: `npm run lint` (ESLint via `@react-native/eslint-config`)

## Architecture

### Shared sync contract (spans all three components)
Every client — desktop, mobile, and the server's own dashboard JS — talks to the relay over STOMP with the same fixed destinations: clients `SUBSCRIBE /user/queue/cliptext` and `SEND /app/cliptext`. `StompDestinationAuthorizationInterceptor` (server) rejects any SUBSCRIBE/SEND to anything else, which is what keeps one user's clipboard isolated from another's. If you change the message shape, all three clients move together: `ClipCascade_Desktop/src/stomp_ws/stomp_manager.py`, `ClipCascade_Mobile/src/StartForegroundService.js`, `ClipCascade_Server/.../resources/static/assets/js/main.js`.

Two sync transports exist per client and are largely independent code paths funneling into the same clipboard read/write surface:
- **P2S** (server relay) — STOMP over WebSocket through the Spring server.
- **P2P** (WebRTC) — direct device-to-device via `aiortc` (desktop) / `react-native-webrtc` (mobile); the server is only used for signaling (SDP offer/answer, ICE) through `P2PWebSocketHandler`.

Payloads are base64-encoded and optionally AES-GCM-encrypted before either transport sends them. In P2P mode, large payloads are split into 15 KiB fragments; reassembly must decode on UTF-8 codepoint boundaries and validate `totalFragments`/`index` before allocating — the logic is deliberately kept identical in `ClipCascade_Desktop/src/core/fragment_utils.py` and `ClipCascade_Mobile/src/fragmentUtils.js`; change both together.

### E2E encryption
Desktop (`utils/cipher_manager.py`) and mobile (`App.js`) each independently derive the same AES-256 key — PBKDF2-HMAC-SHA256, 664,937 rounds, salt = `username+password+salt` — and encrypt with AES-GCM using a fresh random nonce per message. The derived key never leaves the device; the server only ever relays ciphertext (or plaintext, if a user opts out of encryption).

### Desktop: GUI vs CLI duplication
`gui/` (Tkinter — Windows/macOS/Linux-GUI) and `cli/` (terminal prompts — Linux-CLI, selected via `LINUX_USE_CLI_UI`) implement parallel versions of `tray.py`/`login.py`/`info.py`/`message_box.py`. `interfaces/ws_interface.py` is the seam meant to pick between them but imports `cli.tray` unconditionally regardless of which UI is active — the two trees are not cleanly separable yet. `clipboard/clipboard_manager.py` is the single hub both UIs and both transports call through for outbound (`clipboard_to_base64`) and inbound (`base64_to_clipboard`) clipboard events — it's the one place to hook cross-cutting behavior without duplicating it per transport.

### Desktop: encrypted local history (in progress, not yet wired up)
`history/` (`crypto.py`, `store.py`, `service.py`, `retention.py`, `models.py`) is a self-contained, Windows-only encrypted-at-rest clipboard-history store — a DPAPI-wrapped AES-256 master key, per-record AES-256-GCM with AAD binding, and a versioned SQLite schema. It has no dependency on Qt/PySide6 and is not yet called from `ClipboardManager`. `history_ui/` is a separate on-demand PySide6 child process (only packaged when `CLIPCASCADE_WITH_HISTORY_UI=1`) that will eventually read history over IPC — it never gets direct SQLite or key access. Both are being built incrementally; check their module docstrings for current scope before assuming a capability exists.

### Server: fail-fast configuration
`ProductionConfigValidator` (an `EnvironmentPostProcessor`) refuses to start the Spring context if `CC_ADMIN_USERNAME`/`CC_ADMIN_PASSWORD`/`CC_SERVER_DB_PASSWORD` are unset, if the external STOMP broker is enabled without distinct credentials, or if `CC_ALLOWED_ORIGINS` is a wildcard. None of these have working defaults on purpose — don't add one "for local dev convenience."

### Server: dashboard has no build step
The web dashboard (`src/main/resources/templates/*.html` + `static/assets/js/main.js`) is server-rendered Thymeleaf plus plain jQuery — there's no npm/webpack pipeline to run; edits to `main.js`/the templates take effect on the next Spring restart.
