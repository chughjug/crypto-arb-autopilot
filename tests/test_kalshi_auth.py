"""Kalshi credential normalization and error hints."""

from __future__ import annotations

from arb.user_venue import _friendly_kalshi_error, normalize_kalshi_creds


def test_normalize_pem_newlines():
    creds = normalize_kalshi_creds({
        "api_key": "  abc-def  ",
        "private_key": "-----BEGIN RSA PRIVATE KEY-----\\nline\\n-----END RSA PRIVATE KEY-----",
        "demo": False,
    })
    assert creds["api_key"] == "abc-def"
    assert "\n" in creds["private_key"]
    assert creds["demo"] is False


def test_friendly_not_found_message():
    msg = _friendly_kalshi_error(
        401,
        '{"error":{"code":"authentication_error","details":"NOT_FOUND"}}',
        demo=True,
    )
    assert "demo" in msg.lower()
    assert "production" in msg.lower()
