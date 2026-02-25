# Emerald Password Share

Secure, self-destructing encrypted note and file sharing for The Emerald Group.
Inspired by 

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

## Build & run

```bash
# Build
docker build -t emerald-cryptgeon:latest .

# Run (uses docker-compose.yaml)
docker compose up -d

# Rebuild and restart
docker compose up --build -d
```

App runs at **http://localhost:8001**

Needs an SSL to generate note

## Environment variables

| Variable           | Default                  | Description                     |
|--------------------|--------------------------|----------------------------------|
| `REDIS`            | `redis://redis/`         | Redis connection URL              |
| `SIZE_LIMIT_BYTES` | `83886080` (80 MiB)      | Max note payload size             |
| `MAX_VIEWS`        | `100`                    | Max allowed views per note        |
| `MAX_EXPIRATION`   | `360`                    | Max expiration in minutes         |
| `ALLOW_ADVANCED`   | `true`                   | Allow advanced options            |
| `ALLOW_FILES`      | `true`                   | Allow file uploads                |
| `VERBOSITY`        | `INFO`                   | Log level                         |
| `THEME_IMAGE`      | Emerald logo URL         | Logo image URL                    |
| `THEME_PAGE_TITLE` | `Emerald Password Share` | Browser tab title                 |
| `THEME_FAVICON`    | Emerald favicon URL      | Favicon URL                       |
| `THEME_TEXT`       | *(empty)*                | Custom intro text (HTML)          |
