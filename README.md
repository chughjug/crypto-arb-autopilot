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

## User flow

1. Register at `/account` (username + password)
2. Connect Kalshi (demo recommended), Polymarket, and/or Crypto.com API keys at `/autopilot`
3. Pick a sizing strategy and bankroll
4. Start autopilot — scans `crypto_arb` opportunities and places legs on connected venues

**Start in paper/demo mode.** Live mode places real orders.

## API

- `GET /api/crypto/overview` — crypto markets dashboard
- `GET /api/cryptoarbitrage` — arb scanner snapshot
- `GET /api/bots/comparison` — all strategy comparison data
- `POST /api/autopilot/connect/{kalshi|polymarket|cryptocom}` — save encrypted creds
- `POST /api/autopilot/start` / `stop`

WebSocket: `ws://host/ws/crypto` for live overview pushes.
