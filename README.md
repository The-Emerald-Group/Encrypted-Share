# Emerald Password Share

Secure, self-destructing encrypted note and file sharing for The Emerald Group.
Inspired by [cryptgeon](https://github.com/cupcakearmy/cryptgeon) by [@cupcakearmy](https://github.com/cupcakearmy).

## Stack

| Layer    | Tech                              |
|----------|-----------------------------------|
| Backend  | Python 3.13 + FastAPI + Uvicorn   |
| Storage  | Redis 7 (in-memory, no disk)      |
| Frontend | Single static `index.html`        |
| Crypto   | AES-GCM-256 via browser WebCrypto |

## How it works

1. A note is encrypted **in the browser** with AES-GCM-256 before being sent anywhere.
2. The server receives only ciphertext — it is cryptographically incapable of reading note contents.
3. The encryption key lives only in the URL fragment (`#key`), which is never sent to the server.
4. Notes self-destruct after N views or a time limit. Redis TTL enforces time expiry automatically.
5. View-based expiry uses an atomic Lua script in Redis (read + decrement/delete in one step).

## Docker Image

The application is published as a Docker image on Docker Hub:

```
samuelstreets/encrypted-share:latest
```

### Quick start with Docker Compose

Copy the `docker-compose.yaml` below and run:

```bash
docker compose up -d
```

App runs at **http://localhost:8001**

> **Note:** An SSL/TLS certificate is required to generate notes (WebCrypto requires a secure context).

### docker-compose.yaml

```yaml
services:
  redis:
    image: redis:7-alpine
    command: redis-server --save "" --appendonly no
    tmpfs:
      - /data
    restart: unless-stopped

  app:
    image: samuelstreets/encrypted-share:latest
    depends_on:
      - redis
    environment:
      REDIS: redis://redis/
      SIZE_LIMIT_BYTES: "83886080"   # 80 MiB
      MAX_VIEWS: "100"
      MAX_EXPIRATION: "360"          # minutes (6 hours)
      ALLOW_ADVANCED: "true"
      ALLOW_FILES: "true"
      VERBOSITY: "INFO"
      RATE_LIMIT_CREATE: "20"
      RATE_LIMIT_READ: "60"
    ports:
      - "8001:8000"
    restart: unless-stopped
```

### Building the image yourself

```bash
# Build locally
docker build -t encrypted-share:latest .

# Rebuild and restart via Compose
docker compose up --build -d
```

The GitHub Actions workflow in `.github/workflows/docker-image.yml` automatically builds and pushes multi-arch images (`linux/amd64`, `linux/arm64`) to Docker Hub on every push to `main`.

## Environment variables

| Variable            | Default                  | Description                        |
|---------------------|--------------------------|------------------------------------|
| `REDIS`             | `redis://redis/`         | Redis connection URL                |
| `SIZE_LIMIT_BYTES`  | `83886080` (80 MiB)      | Max note payload size               |
| `MAX_VIEWS`         | `100`                    | Max allowed views per note          |
| `MAX_EXPIRATION`    | `360`                    | Max expiration in minutes           |
| `ALLOW_ADVANCED`    | `true`                   | Allow advanced options              |
| `ALLOW_FILES`       | `true`                   | Allow file uploads                  |
| `VERBOSITY`         | `INFO`                   | Log level                           |
| `RATE_LIMIT_CREATE` | `20`                     | Max note creations per minute/IP    |
| `RATE_LIMIT_READ`   | `60`                     | Max note reads per minute/IP        |
| `THEME_IMAGE`       | Emerald logo URL         | Logo image URL                      |
| `THEME_PAGE_TITLE`  | `Emerald Password Share` | Browser tab title                   |
| `THEME_FAVICON`     | Emerald favicon URL      | Favicon URL                         |
| `THEME_TEXT`        | *(empty)*                | Custom intro text (HTML)            |
| `IMPRINT_URL`       | *(empty)*                | Imprint/legal page URL              |
| `IMPRINT_HTML`      | *(empty)*                | Imprint/legal HTML content          |
