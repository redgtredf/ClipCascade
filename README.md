# ClipCascade

Sync your clipboard across all your devices, in real time, with end-to-end encryption. Copy on your phone, paste on your PC — the server only ever relays ciphertext it cannot read.

## How it works

Three independently deployable components:

| Component | Stack | Role |
|---|---|---|
| **Server** (`ClipCascade_Server/`) | Java 21 / Spring Boot | Relay + admin dashboard. Docker images provided. |
| **Desktop client** (`ClipCascade_Desktop/`) | Python | Windows/macOS/Linux, with GUI (tray) and CLI variants |
| **Mobile client** (`ClipCascade_Mobile/`) | React Native | Android |

Two sync transports:

- **Peer-to-Server (P2S)** — default; clipboard events relay through the server over STOMP/WebSocket.
- **Peer-to-Peer (P2P)** — optional; devices connect directly via WebRTC (server used only for signaling), removing server size limits.

Privacy: keys are derived on-device from your username+password (PBKDF2, 664,937 rounds) and each message is encrypted with AES-256-GCM. The key never leaves the device.

## Documentation

| Doc | Contents |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | Install and configure the server (Docker or source), desktop and mobile clients |
| [docs/USAGE.md](docs/USAGE.md) | Day-to-day use, dashboard admin, sizing, troubleshooting |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the components fit together: protocol, transports, crypto, internals |

## Quick start

```bash
# 1. Start the server
cd ClipCascade_Server/docker-compose
cp .env.example .env        # fill in required credentials
docker compose up -d

# 2. Install a client on each device (see docs/SETUP.md)
```

Sign in with the same account on every device you want synced.

## Repository

Upstream: https://github.com/Sathvik-Rao/ClipCascade

## License

See [LICENSE](LICENSE).
