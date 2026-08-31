# ClipCascade — Setup Guide

ClipCascade has three independently deployable components. You can run all three, or just the ones you need:

| Component | Location | What it is |
|---|---|---|
| Desktop client | `ClipCascade_Desktop/` | Python app (Windows/macOS/Linux), GUI (Tkinter) and CLI variants |
| Server (relay) | `ClipCascade_Server/ClipCascade_Backend/` | Spring Boot (Java 21) relay + web dashboard |
| Mobile client | `ClipCascade_Mobile/src/` | React Native app (Android only) |

Minimal setup: **one server** + **two or more clients** (desktop and/or mobile) signed in as the same user.

---

## 1. Server setup

### Option A — Docker (recommended)

```bash
cd ClipCascade_Server/docker-compose
cp .env.example .env
# edit .env — the server refuses to start with empty values
docker compose up -d
```

Required in `.env`:

| Variable | Notes |
|---|---|
| `CC_ADMIN_USERNAME` | Initial admin account, created only when the user database is empty |
| `CC_ADMIN_PASSWORD` | No default — never use `admin123` |
| `CC_SERVER_DB_PASSWORD` | H2 (default): `<file password> <user password>` (two space-separated secrets). PostgreSQL: the database password |
| `CC_ALLOWED_ORIGINS` | Comma-separated origins for the dashboard/WebSocket, e.g. `https://clipcascade.example.com`. Wildcards refused in production |

The compose file documents ~30 optional variables inline (message size limits, P2P toggle, STUN URL, signup, brute-force protection, external STOMP broker, logging, database backend). Health check: `http://localhost:8080/health`.

Compose variants:
- `docker-compose.yml` — default, H2 file database
- `docker-compose-multi-users.yml` — PostgreSQL backend
- `docker-compose-stomp-external-broker.yml` — external ActiveMQ STOMP broker (requires `CC_BROKER_USERNAME`/`CC_BROKER_PASSWORD`)
- `docker-compose-limitless-data-transfer.yml` — large-transfer configuration

### Option B — Run from source

```bash
cd ClipCascade_Server/ClipCascade_Backend
# PowerShell
$env:CC_ADMIN_USERNAME="admin"; $env:CC_ADMIN_PASSWORD="..."; $env:CC_SERVER_DB_PASSWORD="..."; .\mvnw.cmd spring-boot:run
```

Build/test: `.\mvnw.cmd test` (Windows) or `./mvnw test`.

There are no working defaults for the three required env vars by design — `ProductionConfigValidator` fails startup if any is missing, if the external broker is enabled without distinct credentials, or if `CC_ALLOWED_ORIGINS` is a wildcard.

---

## 2. Desktop client setup

```bash
cd ClipCascade_Desktop
pip install -r requirements_win.txt   # or requirements_mac.txt,
                                      # requirements_linux_gui.txt / requirements_linux_cli.txt
python src/main.py
```

Pick only the requirements file for your platform. The Linux CLI variant is selected with the `LINUX_USE_CLI_UI` env var.

Development:

```bash
python -m pytest          # tests (pytest.ini sets pythonpath=src, testpaths=tests)
ruff check src tests      # lint
black / isort             # format (line length 100, configured in src/pyproject.toml)
```

Packaging (Windows):

```bash
cd src
set CLIPCASCADE_WITH_HISTORY_UI=1   # optional: bundle the PySide6 history UI
pyinstaller ClipCascade_win.spec
```

### First run

1. Point the client at your server URL (or the public instance).
2. Register/sign in. Set `CC_SIGNUP_ENABLED=true` on the server if you want open registration; otherwise the admin creates accounts from the dashboard.
3. Optional "Extra Config" on the login page sets per-client limits (max clipboard size, etc.).

## 3. Mobile client setup (Android)

```bash
cd ClipCascade_Mobile/src
npm install
npm run android
```

Tests: `npm test` · Lint: `npm run lint`. Requires Node ≥ 18 and an Android device/emulator. There is no maintained iOS target.

---

## What to read next

- [USAGE.md](USAGE.md) — operating ClipCascade day to day
- [ARCHITECTURE.md](ARCHITECTURE.md) — how the three components fit together
