"""Polymarket CLOB balance helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from arb.user_venue import (
    _parse_polymarket_usdc,
    _polymarket_signature_candidates,
    _polymarket_signature_type,
    normalize_polymarket_creds,
    polymarket_balance,
    verify_polymarket_credentials,
)


def test_parse_polymarket_usdc_from_wei():
    assert _parse_polymarket_usdc({"balance": "5533220"}) == 5.53
    assert _parse_polymarket_usdc({"balance": "1000000"}) == 1.0
    assert _parse_polymarket_usdc({}) is None


def test_normalize_polymarket_creds_adds_prefixes():
    out = normalize_polymarket_creds({
        "private_key": "a" * 64,
        "funder": "b" * 40,
    })
    assert out["private_key"] == "0x" + "a" * 64
    assert out["funder"] == "0x" + "b" * 40


def test_polymarket_signature_type_defaults():
    assert _polymarket_signature_type({}) is None
    assert _polymarket_signature_type({"funder": "0xabc"}) == 1
    assert _polymarket_signature_type({"signature_type": 2, "funder": "0xabc"}) == 2


def test_polymarket_signature_candidates_auto_with_funder():
    assert _polymarket_signature_candidates({"funder": "0xabc"}) == [1, 3, 2, 0]
    assert _polymarket_signature_candidates({"funder": "0xabc", "signature_type": 3}) == [3]


def test_polymarket_balance_uses_collateral_asset_type():
    creds = {"private_key": "0x" + "a" * 64, "funder": "0x" + "b" * 40, "signature_type": 1}
    mock_client = MagicMock()
    mock_client.get_balance_allowance.return_value = {"balance": "2500000", "allowance": "0"}

    with patch("arb.user_venue._polymarket_clob_client", return_value=mock_client):
        result = polymarket_balance(creds)

    assert result["venue"] == "polymarket"
    assert result["usdc"] == 2.5
    assert result["raw"]["balance"] == "2500000"
    assert mock_client.get_balance_allowance.call_count >= 1


def test_polymarket_balance_missing_key():
    assert polymarket_balance({}) == {"error": "Polymarket private key missing"}


def test_verify_polymarket_credentials_picks_working_signature_type():
    creds = {
        "private_key": "0x" + "a" * 64,
        "funder": "0x" + "b" * 40,
    }
    mock_client = MagicMock()
    mock_client.get_balance_allowance.return_value = {"balance": "1000000"}

    def fake_client(probe):
        sig = probe.get("signature_type")
        if sig == 1:
            raise RuntimeError("invalid asset type")
        if sig == 3:
            return mock_client
        raise RuntimeError("auth failed")

    with patch("arb.user_venue._polymarket_clob_client", side_effect=fake_client):
        result = verify_polymarket_credentials(creds)

    assert result["signature_type"] == 3
    assert result["usdc"] == 1.0


def test_verify_polymarket_credentials_rejects_bad_key():
    result = verify_polymarket_credentials({"private_key": "not-a-key"})
    assert "error" in result
