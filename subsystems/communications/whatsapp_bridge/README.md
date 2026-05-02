# Pepper WhatsApp Send Bridge

Send-only HTTP service backed by [`whatsapp-web.js`](https://github.com/pedroslopez/whatsapp-web.js).

## Why a bridge?

Pepper reads WhatsApp directly from the local `ChatStorage.sqlite`, which is fast
and lock-free. WhatsApp Desktop offers no API for *sending*, so when Pepper
needs to deliver an approved reply we route it through this small bridge. The
bridge:

- runs as a separate process, started only when you want send capability
- binds to `127.0.0.1` only — never expose it off-host
- persists its session under `./.wa_session` so QR pairing is one-time
- never touches Pepper's read path

## Setup

### Docker (recommended — runs alongside the rest of Pepper)

The bridge is wired into `docker-compose.yml` as the `whatsapp-bridge` service.
Make sure `.env` contains:

```
PEPPER_WA_BRIDGE_TOKEN=<run: openssl rand -hex 24>
```

Then bring everything up:

```bash
docker compose up -d
docker compose logs -f whatsapp-bridge   # watch for the QR on first run
```

Scan the QR with `WhatsApp → Settings → Linked Devices → Link a Device`.

The session is persisted in the named docker volume `pepper_whatsapp_session`,
so you only scan once. WhatsApp may force a re-link if you don't open
WhatsApp on your phone for ~14 days, but that's WhatsApp's policy.

To wipe the session and re-pair:

```bash
docker compose stop whatsapp-bridge
docker volume rm pepper_whatsapp_session
docker compose up -d whatsapp-bridge
```

### Local (no Docker)

```bash
cd subsystems/communications/whatsapp_bridge
npm install
PEPPER_WA_TOKEN="$(openssl rand -hex 24)" npm start
```

Set the same token in Pepper's `.env` as `PEPPER_WA_BRIDGE_TOKEN` and point
`PEPPER_WA_BRIDGE_URL` at `http://127.0.0.1:3025`.

## Endpoints

- `GET /health` → `{ ready, state }`
- `POST /send` `{ chatId, message, replyTo? }` → `{ ok, id }`

`chatId` accepts a bare phone number (`447700900123`), a user JID
(`447700900123@c.us`), or a group JID (`123-456@g.us`).
