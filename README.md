# Crypto Arb Autopilot

Standalone app for **crypto.com Predict + Kalshi + Polymarket** short-term threshold markets.

## Features

- **/crypto** — live crypto prediction markets across all three venues
- **/cryptoarbitrage** — real-time 3-venue arb scanner + 22 paper sizing strategies
- **/bots** — strategy comparison dashboard (equity curves, heatmaps, allocator)
- **/autopilot** — user accounts connect venue API keys; bot trades on their behalf
- **Crypto.com Exchange API** — balance + order placement via HMAC-signed private API

## Local dev

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export AUTOPILOT_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
python server.py 8000
```

Open http://127.0.0.1:8000/crypto

## Heroku deploy

```bash
heroku create your-crypto-arb-app
heroku config:set AUTOPILOT_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
heroku config:set CATALOG_WARM=1 CATALOG_DB_PATH=/tmp/catalog.db
git push heroku main
```

## Env vars

| Variable | Purpose |
|----------|---------|
| `AUTOPILOT_SECRET_KEY` | Encrypts per-user Kalshi/Poly/Crypto.com credentials (required in prod) |
| `CATALOG_DB_PATH` | SQLite catalog (`/tmp/catalog.db` on Heroku) |
| `ACCOUNTS_DB_PATH` | User accounts DB |
| `AUTOPILOT_DB_PATH` | Autopilot config + encrypted creds |
| `CRYPTO_ARB_POLL_SECONDS` | Arb scanner interval (default 2s) |

## Security

- **Passwords:** scrypt (N=2^15, r=8, p=1), min 10 characters
- **2FA:** mandatory TOTP (Google Authenticator / 1Password / Authy) on every sign-in
- **Venue API keys:** per-user AES-256-GCM with HKDF-derived keys, sensitive fields sealed individually, master envelope layer
- **Sessions:** random tokens stored as HMAC-SHA256 hashes only (never plaintext in DB)
- **Cookies:** `HttpOnly`, `SameSite=Strict`, `Secure` on Heroku

Set a strong `AUTOPILOT_SECRET_KEY` (32+ byte random hex recommended):

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

## User flow

1. **Register** at `/account` → scan QR / enter TOTP secret → confirm with first code
2. **Sign in** → password → 6-digit authenticator code
3. Connect venues at `/autopilot` → start in paper/demo mode

## API

- `GET /api/crypto/overview` — crypto markets dashboard
- `GET /api/cryptoarbitrage` — arb scanner snapshot
- `GET /api/bots/comparison` — all strategy comparison data
- `POST /api/autopilot/connect/{kalshi|polymarket|cryptocom}` — save encrypted creds
- `POST /api/autopilot/start` / `stop`

WebSocket: `ws://host/ws/crypto` for live overview pushes.
