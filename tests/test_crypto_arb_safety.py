"""Safety invariants for crypto-arbitrage matching and settlement."""

from unittest.mock import patch

import crypto_arb
import crypto_arb_bot
import poly_chainlink


def _quote(venue, strike, expiry="2026-07-12T12:00:00", yes=0.40, no=0.60):
    return {
        "coin": "BTC",
        "venue": venue,
        "strike": strike,
        "expiry": expiry,
        "yes": yes,
        "no": no,
        "strike_verified": True,
        "strike_source": "test source",
        "settlement_rule_verified": True,
        "yes_operator": ">=",
        "no_operator": "<",
    }


def test_expiry_is_normalized_to_exact_utc_second():
    assert crypto_arb._canonical_expiry("2026-07-12T08:00:01-04:00") == "2026-07-12T12:00:01"


def test_chainlink_t0_tick_requires_connection_before_window():
    start_ms = 1_800_000_000_000
    with patch.object(poly_chainlink, "_persist_locked"):
        poly_chainlink._REFS.clear()
        poly_chainlink._CONNECTED_SINCE_MS = start_ms - 1_000
        poly_chainlink._record_tick("btc/usd", start_ms + 5_000, 64_000)
    assert poly_chainlink._REFS["BTC|1800000000"]["verified"] is True


def test_chainlink_tick_after_late_connection_is_not_verified():
    start_ms = 1_800_000_000_000
    with patch.object(poly_chainlink, "_persist_locked"):
        poly_chainlink._REFS.clear()
        poly_chainlink._CONNECTED_SINCE_MS = start_ms + 1_000
        poly_chainlink._record_tick("btc/usd", start_ms + 5_000, 64_000)
    assert poly_chainlink._REFS["BTC|1800000000"]["verified"] is False


def test_compute_accepts_only_exact_verified_cross_venue_strikes():
    result = crypto_arb.compute([
        _quote("kalshi", 100_000, yes=0.40, no=0.60),
        _quote("polymarket", 100_000.0, yes=0.60, no=0.40),
    ])
    assert len(result["opportunities"]) == 1
    assert result["opportunities"][0]["exact_strike_match"] is True


def test_compute_accepts_near_strikes_when_payout_is_covered():
    result = crypto_arb.compute([
        _quote("kalshi", 100_000, yes=0.40, no=0.60),
        _quote("polymarket", 100_050, no=0.40),
    ])
    assert len(result["opportunities"]) == 1
    assert result["opportunities"][0]["exact_strike_match"] is False
    assert result["opportunities"][0]["strike_gap"] > 0


def test_compute_rejects_misordered_strikes():
    result = crypto_arb.compute([
        _quote("kalshi", 100_050, yes=0.40),
        _quote("polymarket", 100_000, no=0.40),
    ])
    assert result["opportunities"] == []


def test_compute_rejects_different_expiry_seconds():
    result = crypto_arb.compute([
        _quote("kalshi", 100_000, "2026-07-12T12:00:00", yes=0.40),
        _quote("polymarket", 100_000, "2026-07-12T12:00:01", no=0.40),
    ])
    assert result["opportunities"] == []


def test_compute_rejects_unverified_strike():
    quote = _quote("polymarket", 100_000, no=0.40)
    quote["strike_verified"] = False
    result = crypto_arb.compute([
        _quote("kalshi", 100_000, yes=0.40),
        quote,
    ])
    assert result["opportunities"] == []


def test_compute_rejects_pair_that_misses_equality():
    strict_yes = _quote("cryptocom", 100_000, yes=0.40)
    strict_yes["yes_operator"] = ">"
    result = crypto_arb.compute([
        strict_yes,
        _quote("kalshi", 100_000, no=0.40),
    ])
    assert result["opportunities"] == []


def test_expired_trade_stays_pending_without_price():
    with patch.object(crypto_arb_bot.ArbBot, "_ensure_loop"):
        bot = crypto_arb_bot.ArbBot("half_kelly")
    bot.positions["trade"] = {
        "coin": "BTC",
        "expiry": "2026-07-12T12:00:00",
        "expiry_ts": 1.0,
        "strike": 100_000.0,
        "yes_venue": "kalshi",
        "no_venue": "polymarket",
        "yes_cost": 0.40,
        "no_cost": 0.40,
        "contracts": 1,
        "cost_cents": 80,
        "payout_cents": 100,
        "locked_cents": 20,
        "gap": 0.0,
        "spread": 0.20,
        "strategy": "half_kelly",
        "venue_details": {
            "kalshi": {"strike": 100_000.0, "strike_verified": True, "settlement_rule_verified": True, "yes_operator": ">=", "no_operator": "<"},
            "polymarket": {"strike": 100_000.0, "strike_verified": True, "settlement_rule_verified": True, "yes_operator": ">=", "no_operator": "<"},
        },
    }
    bot.cash_cents -= 80

    with patch.object(crypto_arb, "_fetch_spot", return_value={}):
        bot._settle(2.0)

    assert "trade" in bot.positions
    assert bot.settled_count == 0
    assert bot.wins == 0


def test_normalize_strike_rounds_to_cent():
    assert crypto_arb.normalize_strike("63729.649035") == 63729.65
    assert crypto_arb.normalize_strike(63700) == 63700.0
    assert crypto_arb.format_strike(63700) == "63700.00"


def test_threshold_won_uses_unrounded_spot():
    assert crypto_arb.threshold_won("100000.004", 100_000.0, ">=") is True
    assert crypto_arb.threshold_won("99999.996", 100_000.0, ">=") is False
    assert crypto_arb.threshold_won("100000.00", 100_000.0, ">=") is True
    assert crypto_arb.threshold_won("99999.99", 100_000.0, "<") is True


def test_expired_trade_settles_only_after_price_comparison():
    with patch.object(crypto_arb_bot.ArbBot, "_ensure_loop"):
        bot = crypto_arb_bot.ArbBot("half_kelly")
    bot.positions["trade"] = {
        "coin": "BTC",
        "expiry": "2026-07-12T12:00:00",
        "expiry_ts": 1.0,
        "strike": 100_000.0,
        "yes_venue": "kalshi",
        "no_venue": "polymarket",
        "yes_cost": 0.40,
        "no_cost": 0.40,
        "contracts": 1,
        "cost_cents": 80,
        "payout_cents": 100,
        "locked_cents": 20,
        "gap": 0.0,
        "spread": 0.20,
        "strategy": "half_kelly",
        "venue_details": {
            "kalshi": {"strike": 100_000.0, "strike_verified": True, "settlement_rule_verified": True, "yes_operator": ">=", "no_operator": "<"},
            "polymarket": {"strike": 100_000.0, "strike_verified": True, "settlement_rule_verified": True, "yes_operator": ">=", "no_operator": "<"},
        },
    }
    bot.cash_cents -= 80

    with patch.object(crypto_arb, "_fetch_spot", return_value={"BTC": 100_000.004}):
        bot._settle(2.0)

    assert "trade" not in bot.positions
    assert bot.settled_count == 1
    assert bot.wins == 1
    assert bot.former_positions[0]["winning_leg"] == "YES"
    assert bot.former_positions[0]["spot_price"] == 100_000.004
