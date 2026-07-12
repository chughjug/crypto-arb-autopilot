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
