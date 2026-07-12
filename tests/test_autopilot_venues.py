"""Autopilot venue gating — only trade arbs on linked platforms."""

from __future__ import annotations

import os

os.environ.setdefault("AUTOPILOT_SECRET_KEY", "test-master-key-for-unit-tests-only-32b")

import autopilot_executor
import autopilot_store


def test_filter_opportunities_skips_unlinked_venues(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_DB_PATH", str(tmp_path / "autopilot.db"))
    autopilot_store._conn = None

    user_id = "user-kalshi-only"
    autopilot_store.save_venue_credentials(user_id, "kalshi", {
        "api_key": "k",
        "private_key": "-----BEGIN KEY-----",
        "demo": True,
    })

    opps = [
        {"yes_venue": "kalshi", "no_venue": "polymarket", "coin": "BTC"},
        {"yes_venue": "kalshi", "no_venue": "kalshi", "coin": "ETH"},
    ]
    filtered = autopilot_executor.filter_opportunities(user_id, opps)
    assert len(filtered) == 1
    assert filtered[0]["coin"] == "ETH"

    ok, reason = autopilot_executor.can_execute(user_id, opps[0])
    assert not ok
    assert reason == "polymarket_not_connected"
