# Keys registry — ClipCascade

No static API keys exist in this project. All credentials are server env vars, set at deploy time.

| Name | Env var | Location | How to load | How to use | Status |
|---|---|---|---|---|---|
| Admin account | `CC_ADMIN_USERNAME` / `CC_ADMIN_PASSWORD` | Server env / docker-compose `.env` | Set before server start | Initial admin login (created only when user DB is empty) | required, no default |
| Database password | `CC_SERVER_DB_PASSWORD` | Server env / docker-compose `.env` | Set before server start | Encrypts user DB (H2: `<file pw> <user pw>`) | required, no default |
| CORS origins | `CC_ALLOWED_ORIGINS` | Server env / docker-compose `.env` | Set before server start | Allowed dashboard/WebSocket origins | required, no default |
| Broker creds (optional) | `CC_BROKER_USERNAME` / `CC_BROKER_PASSWORD` | Server env | Set if external STOMP broker enabled | External ActiveMQ login | conditional |

## Rules

- Secret VALUES live only in `ClipCascade_Server/docker-compose/.env` (gitignored at root) or the runtime environment — never in code, never committed.
- E2E clipboard keys are derived on-device (PBKDF2 from username+password) and never leave clients — nothing to register here.
- `ProductionConfigValidator` fails startup when required vars are missing; do not add defaults.
