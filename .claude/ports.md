# Ports — ClipCascade (assigned 2026-08-31)

| Service | Port | Purpose | Start command | Notes |
|---|---|---|---|---|
| ClipCascade server (dev) | 8731 | Spring Boot relay + dashboard | `CC_PORT=8731 mvnw.cmd spring-boot:run` (in `ClipCascade_Server/ClipCascade_Backend`) | Default prod port is 8080 via Docker; 8731 reserved for dev to avoid clashing with a running container |
| Metro bundler | 8081 | React Native dev server | `npm start` (in `ClipCascade_Mobile/src`) | RN default, verified free |

Verified free at assignment: 8731, 8732 (spare), 8081.
Recovery: `/dev-restart` reads this file. Never start the dev server on 8080 if the Docker container may be running.
