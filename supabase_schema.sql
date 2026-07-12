-- Crypto Arb Autopilot — Supabase / PostgreSQL schema
-- Run once in Supabase SQL editor, or auto-applied on first connect via db.py

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE,
    password_hash TEXT,
    username TEXT UNIQUE,
    display_name TEXT NOT NULL,
    is_guest BOOLEAN NOT NULL DEFAULT FALSE,
    totp_secret_enc TEXT,
    totp_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    created_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS auth_challenges (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_challenges_user ON auth_challenges(user_id);

CREATE TABLE IF NOT EXISTS venue_credentials (
    user_id TEXT NOT NULL,
    venue TEXT NOT NULL,
    enc_payload TEXT NOT NULL,
    key_fingerprint TEXT,
    connected_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, venue)
);

CREATE TABLE IF NOT EXISTS autopilot_config (
    user_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL DEFAULT 'half_kelly',
    bankroll_usd DOUBLE PRECISION NOT NULL DEFAULT 300,
    live_mode BOOLEAN NOT NULL DEFAULT FALSE,
    max_exposure_pct DOUBLE PRECISION,
    reserve_pct DOUBLE PRECISION NOT NULL DEFAULT 30,
    running BOOLEAN NOT NULL DEFAULT FALSE,
    overrides TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS autopilot_log (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    ts DOUBLE PRECISION NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_autopilot_log_user ON autopilot_log(user_id, ts DESC);

CREATE TABLE IF NOT EXISTS autopilot_trades (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    arb_id TEXT NOT NULL,
    ts DOUBLE PRECISION NOT NULL,
    coin TEXT,
    expiry TEXT,
    contracts INTEGER,
    edge_cents DOUBLE PRECISION,
    locked_pnl DOUBLE PRECISION,
    cost_total DOUBLE PRECISION,
    live_mode BOOLEAN NOT NULL DEFAULT FALSE,
    ok BOOLEAN NOT NULL DEFAULT FALSE,
    errors JSONB,
    legs JSONB,
    status TEXT NOT NULL DEFAULT 'open',
    settled_at DOUBLE PRECISION,
    pnl DOUBLE PRECISION,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_autopilot_trades_user_ts ON autopilot_trades(user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_autopilot_trades_status ON autopilot_trades(user_id, status);

CREATE TABLE IF NOT EXISTS bot_state (
    strategy_id TEXT PRIMARY KEY,
    life INTEGER NOT NULL DEFAULT 1,
    cash_cents INTEGER NOT NULL DEFAULT 5000,
    realized_cents INTEGER NOT NULL DEFAULT 0,
    settled_count INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    total_injected_cents INTEGER NOT NULL DEFAULT 5000,
    lifetime_realized_cents INTEGER NOT NULL DEFAULT 0,
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_positions (
    id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES bot_state(strategy_id) ON DELETE CASCADE,
    coin TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike DOUBLE PRECISION,
    yes_venue TEXT,
    no_venue TEXT,
    yes_cost DOUBLE PRECISION,
    no_cost DOUBLE PRECISION,
    contracts INTEGER,
    cost_cents INTEGER,
    locked_cents INTEGER,
    payout_cents INTEGER,
    gap DOUBLE PRECISION,
    entry_ts DOUBLE PRECISION,
    expiry_ts DOUBLE PRECISION,
    data JSONB,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bot_positions_strategy ON bot_positions(strategy_id);

CREATE TABLE IF NOT EXISTS bot_trades (
    id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES bot_state(strategy_id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    coin TEXT,
    expiry TEXT,
    strike DOUBLE PRECISION,
    contracts INTEGER,
    cost_total DOUBLE PRECISION,
    locked_pnl DOUBLE PRECISION,
    pnl DOUBLE PRECISION,
    spread DOUBLE PRECISION,
    entry_ts DOUBLE PRECISION,
    settled_at DOUBLE PRECISION,
    data JSONB,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bot_trades_strategy ON bot_trades(strategy_id);
CREATE INDEX IF NOT EXISTS idx_bot_trades_status ON bot_trades(strategy_id, status);

CREATE TABLE IF NOT EXISTS bot_busts (
    id BIGSERIAL PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES bot_state(strategy_id) ON DELETE CASCADE,
    life INTEGER NOT NULL,
    ts DOUBLE PRECISION NOT NULL,
    final_cash DOUBLE PRECISION NOT NULL,
    realized DOUBLE PRECISION NOT NULL,
    settled INTEGER NOT NULL,
    wins INTEGER NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bot_busts_strategy ON bot_busts(strategy_id);

CREATE TABLE IF NOT EXISTS bot_equity_curve (
    id BIGSERIAL PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES bot_state(strategy_id) ON DELETE CASCADE,
    ts DOUBLE PRECISION NOT NULL,
    equity DOUBLE PRECISION NOT NULL,
    life INTEGER,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bot_equity_curve_strategy ON bot_equity_curve(strategy_id, ts DESC);
