# Architecture plan (modularise) — ClipCascade

Three independent components, no shared code. The integration surfaces below are where drift causes cross-component bugs; guard them first.

## Where new code goes

| Need | Put it in | Do NOT |
|---|---|---|
| Clipboard read/write behavior | `ClipCascade_Desktop/src/clipboard/clipboard_manager.py` (single hub) | duplicate per transport |
| New transport detail (P2S or P2P) | `src/stomp_ws/` or `src/p2p/` behind ClipboardManager | leak transport into GUI/CLI code |
| New wire-protocol field | ALL of: `stomp_manager.py` (desktop) + `StartForegroundService.js` (mobile) + `static/assets/js/main.js` (server dashboard) — in the same change | change one side only |
| New P2P fragmentation behavior | `src/core/fragment_utils.py` AND `ClipCascade_Mobile/src/fragmentUtils.js` — kept byte-identical | let them diverge |
| E2E crypto changes | `utils/cipher_manager.py` (desktop) + `App.js` key derivation (mobile) — must stay algorithm-identical | change rounds/salt on one side |
| Server config | `ClipCascadeProperties.java` via `CC_*` env vars | new `application.properties` defaults bypassing the validator |
| Dashboard UI | Thymeleaf templates + `static/assets/js/main.js` (no npm build step) | introduce a bundler without a decision |
| Server DB access | under `com/acme/clipcascade/` per `.claude/db-access.md` | anything client-side |

## Anticipated split points (future refactors, advisory)

1. **`interfaces/ws_interface.py` GUI/CLI seam** — currently imports `cli.tray` unconditionally regardless of active UI. When GUI/CLI selection is touched, split the interface selection from the cli import; this is the highest-drift seam in the desktop app.
2. **`gui/` vs `cli/` duplication** — parallel implementations of `tray.py`, `login.py`, `info.py`, `message_box.py`. If a third UI ever appears, extract shared logic instead of a third copy.
3. **Server dashboard `main.js`** — single flat JS file carrying both admin and user logic; will grow. Split admin/user modules before it exceeds ~1500 lines.
4. **`history/` integration** — when the encrypted local history store is wired up, its service should hang off ClipboardManager as a listener, not be called from transports.

## Hard constraints (from CLAUDE.md)

- `StompDestinationAuthorizationInterceptor` allow-list is the multi-user isolation boundary — new destinations need explicit server + all-client changes.
- `ProductionConfigValidator` fail-fast rules must not be relaxed with defaults.
- P2P reassembly must decode on UTF-8 codepoint boundaries and validate `totalFragments`/`index` before allocation.
