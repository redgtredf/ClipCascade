# DB access — ClipCascade

## Data architecture

| Store | Where | Client/library | Env keys | Role |
|---|---|---|---|---|
| User DB (H2 encrypted file, default) | `./database/clipcascade` in server container (bind mount `./cc_users`) | Spring Data JPA / Hibernate | `CC_SERVER_DB_URL`, `CC_SERVER_DB_DRIVER`, `CC_SERVER_DB_USERNAME`, `CC_SERVER_DB_PASSWORD` | Source of truth: accounts, sessions |
| User DB (PostgreSQL option) | external Postgres (`docker-compose-multi-users.yml`) | same JPA stack | same vars, PostgreSQL values | Same, for multi-user deployments |
| Desktop clipboard history (in progress) | local SQLite per Windows machine | `ClipCascade_Desktop/src/history/store.py` | none | Local only, DPAPI-wrapped AES-256 key, per-record AES-GCM. Not wired into ClipboardManager yet. |

## Placement rules

- **All server-side DB code lives under `ClipCascade_Server/ClipCascade_Backend/src/main/java/com/acme/clipcascade/`** (entities/repositories). No DB driver or connection logic anywhere in `ClipCascade_Desktop/` or `ClipCascade_Mobile/` — those are clients of the server over STOMP/WebSocket only.
- No secret values in client code: clients never hold DB credentials; the only client-side secret is the on-device derived E2E key (in memory).
- Schema changes: Hibernate runs `ddl-auto=validate` — migrations must go through explicit SQL init (`spring.sql.init.mode=always`), not auto-DDL. H2 and PostgreSQL dialects are both supported; keep SQL portable or dialect-guarded.
- `CC_SERVER_DB_PASSWORD` format differs by backend: H2 = two space-separated secrets (`<file password> <user password>`); PostgreSQL = the database password. The same value is required for any future migration.
- Never enable `spring.h2.console` or weaken `ddl-auto` outside a commented local-debug change.

## Boundary summary (client/server)

Desktop and mobile apps may never import a DB driver, read `CC_SERVER_DB_*` vars, or talk to the database directly. The server is the only component with data access; clients sync via the clipboard STOMP contract (see `.claude/architecture.md`).
