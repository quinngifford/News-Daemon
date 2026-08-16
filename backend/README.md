# Ticker backend

The public half of the system. The VPS detector stays private and POSTs
detected events here; this service owns accounts, payment, the web app, and
push to real users.

```
VPS detector (private)                    THIS SERVICE (public)
ingest → screen → confirm ──signed POST──▶ /api/ingest/events
                                                  │
                                                  ├─▶ SSE to open browsers
                                                  ├─▶ Web Push to devices
                                                  └─▶ stored for the feed
```

## Run it

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # then edit
.venv/bin/uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>. Without Stripe keys the paywall shows a
**"Dev: grant access without paying"** button so you can use the app immediately.

```bash
.venv/bin/python -m tests.test_flow      # end-to-end: signup → pay → ingest → SSE
```

## How it scales

- **Stateless app servers.** Auth is a JWT; there are no server-side sessions,
  so you scale by running more copies behind a load balancer. No sticky sessions.
- **Postgres in production.** Set `DATABASE_URL=postgresql+psycopg://…`. Same
  code, no changes; the only dialect-specific piece is JSON → JSONB.
- **Redis for fanout.** With one replica the in-process bus is fine. Set
  `REDIS_URL` before running a second, or a browser connected to replica A will
  miss an event ingested by replica B.
- **Keyset pagination** on the event feed, so it does not degrade as the table
  grows.

```bash
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

## How to add new fields

This is designed so the detector can evolve without the backend blocking it.

1. **Nothing required.** Every field the detector sends is stored verbatim in
   `events.payload` and served to clients untouched — even fields this backend
   has never heard of. `tests/test_flow.py` asserts exactly this with a
   `future_field` the code does not know about.
2. **To query or index it**, promote it to a column:

```bash
# edit app/models.py, then:
.venv/bin/alembic revision --autogenerate -m "add my_field"
.venv/bin/alembic upgrade head
```

Every table also has a free-form `data` JSON column for attributes that need
storing but not indexing — user preferences, purchase flags, future auto-invest
settings.

> Note: `alembic revision --autogenerate` diffs against your **current**
> database. If dev already has the tables (created automatically on first boot),
> autogenerate produces an empty migration. Generate against a fresh database:
> `DATABASE_URL="sqlite:///$(mktemp -d)/fresh.db" .venv/bin/alembic revision --autogenerate -m "…"`

## Deploying to Render

`render.yaml` is a blueprint: Render reads it and creates the web service **and**
the Postgres database.

**1.** Push this repo to GitHub.

**2.** Render dashboard → **New → Blueprint** → pick the repo. It finds
`backend/render.yaml` automatically.

> If the blueprint errors on a plan name, open the service or database in the
> dashboard and pick any available plan. Render renames these periodically and
> nothing else in the file depends on it.

**3.** Set the secrets Render deliberately does not generate.
Service → **Environment**:

| Key | Value |
|---|---|
| `BASE_URL` | your Render URL, e.g. `https://ticker-api.onrender.com` — **no trailing slash** |
| `INGEST_SECRET` | `openssl rand -hex 32` — must match the detector exactly |
| `STRIPE_SECRET_KEY` | Stripe → Developers → API keys |
| `STRIPE_PUBLISHABLE_KEY` | same page |
| `STRIPE_WEBHOOK_SECRET` | from step 4 |
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` | detector's `deploy/ticker.env` |
| `VAPID_SUBJECT` | `mailto:you@example.com` |

**4.** Stripe → **Developers → Webhooks → Add endpoint**

- URL: `https://YOUR-RENDER-URL/api/billing/webhook`
- Events: `checkout.session.completed`, `charge.refunded`
- Copy the **Signing secret** (`whsec_…`) into `STRIPE_WEBHOOK_SECRET`, redeploy.

**5.** Point the detector at it — on the VPS, in `deploy/ticker.env`:

```
TICKER_WEBHOOK_URL=https://YOUR-RENDER-URL/api/ingest/events
TICKER_WEBHOOK_SECRET=<the same value as INGEST_SECRET>
```

then `sudo systemctl restart ticker.service`.

Migrations run on every deploy (`alembic upgrade head` is in the start command),
so schema changes ship with the code.

> **Free/starter instances sleep when idle**, and a sleeping instance can miss
> the POST that matters. The detector's outbox retries so nothing is lost, but
> the alert is delayed by the cold start. Move to a paid instance before this is
> real money.

## API

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /api/auth/signup` `/login` | — | Returns a bearer token |
| `GET /api/auth/me` | bearer | Account + entitlement |
| `GET /api/billing/config` | — | Price and Stripe publishable key |
| `POST /api/billing/checkout` | bearer | Starts Stripe Checkout |
| `POST /api/billing/webhook` | Stripe sig | **Only** place entitlement is granted |
| `POST /api/ingest/events` | HMAC | Detector → here |
| `GET /api/events` | **paid** | Feed, keyset-paginated |
| `GET /api/events/stream` | **paid** | SSE live stream |
| `POST /api/push/register` | bearer | Register webpush/APNs/FCM device |
| `POST /api/push/test` | **paid** | Verify notifications before the real event |
| `GET /api/news` `/api/market/{sym}` | — | Ambient content |

Interactive docs at `/api/docs`.

## Security decisions worth knowing

- **Ingest is HMAC-signed, not bearer-token'd.** A leaked read token cannot be
  used to inject a fake death alert — the most damaging thing an attacker could
  do here, given what users act on.
- **Entitlement is granted by the Stripe webhook only**, never by the browser
  returning to the success URL. A success redirect is trivially forged.
- **The app refuses to boot in prod** with a default JWT secret, an empty ingest
  secret, or `ALLOW_DEV_GRANT` still on.
- Passwords are Argon2id. Login hashes even for unknown emails so response time
  does not reveal which accounts exist.

## Native mobile app

The API is bearer-token based specifically so a native app can use it unchanged.

**The iPhone app is built** — see [../ios/README.md](../ios/README.md). It is a
SwiftUI port of `web/`, talks to these same endpoints, and registers devices as
`kind: "apns"`. `_send_apns` in `app/push.py` delivers to it; set `APNS_KEY_ID`,
`APNS_TEAM_ID`, `APNS_PRIVATE_KEY`, `APNS_TOPIC` and `APNS_USE_SANDBOX` (see
`.env.example`) or registration succeeds and nothing is ever delivered.

Android would follow the same path: register with `kind: "fcm"`, implement
`_send_fcm`, add it to `SENDERS`. Nothing else changes.

## Art placeholders

Two dashed blocks in `web/index.html`, marked `data-art="hero"` (1200×480) and
`data-art="sidebar"` (600×600). Replace each `<div class="panel art-placeholder">`
with your `<img>`.
