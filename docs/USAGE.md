# ClipCascade — Usage Guide

## What ClipCascade does

ClipCascade syncs your clipboard across your devices in real time. Copy on your phone, paste on your PC — and vice versa. Everything can be end-to-end (E2E) encrypted so the server only ever relays ciphertext it cannot read.

## Core concepts

| Concept | Meaning |
|---|---|
| **P2S** (Peer-to-Server) | Default mode. Clipboard events relay through the Spring server over STOMP/WebSocket. Limited by the server's `CC_MAX_MESSAGE_SIZE_IN_MiB`. |
| **P2P** (Peer-to-Peer) | Optional. Devices connect directly via WebRTC; the server is only used for signaling. Removes server size limits and load. Enable with `CC_P2P_ENABLED=true` + a STUN URL. Some networks block direct connections. |
| **E2E encryption** | On by default. Key derived on-device from your username+password (PBKDF2, 664,937 rounds); each message encrypted with AES-256-GCM and a fresh nonce. The derived key never leaves the device. Opting out sends plaintext. |
| **Sync scope** | Only devices signed into the *same account* on the same server see each other's clipboard. |

## Day-to-day use

1. **Start the client** — desktop: system tray app (`python src/main.py`); mobile: the app runs a foreground service.
2. **Sign in** once; the session persists.
3. **Copy anything** — text, images, files. It is sent automatically to your other signed-in devices, which receive it as a notification / directly into their clipboard depending on platform behavior.
4. **Paste** on the other device as normal.

### Server dashboard

The admin dashboard is served at the server root (e.g. `http://localhost:8080`). From there the admin can:

- Create/disable user accounts (used when `CC_SIGNUP_ENABLED=false`)
- View connection and usage info
- Configure server-side options

### Sizing your server

- `CC_MAX_MESSAGE_SIZE_IN_MiB` caps clipboard payload size in P2S mode (default 1 MiB — Android text clipboards rarely exceed ~1 MiB; raise it for images/files).
- Client-side limits are set per device via "Extra Config" on the login page.
- P2P mode ignores the size caps entirely.

### Security behaviors to know

- **Brute-force protection**: accounts lock after failed attempts from multiple IPs (`CC_MAX_UNIQUE_IP_ATTEMPTS`, default 15) or per-IP attempts (default 30); lockout scales with repeats.
- **Sessions** are logged out on server restart (default timeout: 1 year).
- Behind a reverse proxy, add its address to `CC_TRUSTED_PROXIES` so brute-force protection sees real client IPs. Do not enable `forward-headers-strategy=framework` — it would let clients spoof their IP.

## Troubleshooting

| Symptom | Check |
|---|---|
| Server won't start | Required env vars missing (`CC_ADMIN_USERNAME`, `CC_ADMIN_PASSWORD`, `CC_SERVER_DB_PASSWORD`, `CC_ALLOWED_ORIGINS`). The validator's error names the missing one. |
| Client can't connect | Server URL reachable? Origin in `CC_ALLOWED_ORIGINS`? Health endpoint: `/health`. |
| Clipboard doesn't sync between two devices | Both signed into the same account on the same server? Both online? |
| Large copies fail in P2S mode | Raise `CC_MAX_MESSAGE_SIZE_IN_MiB`, or enable P2P. |
| P2P never connects | Network may block WebRTC; try a different STUN server or fall back to P2S. |
| Wrong-password lockout | Wait out the lockout timer; it scales with repeat failures. |
