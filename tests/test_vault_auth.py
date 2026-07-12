"""Tests for vault encryption and 2FA auth."""

from __future__ import annotations

import os

import pyotp

os.environ["AUTOPILOT_SECRET_KEY"] = "test-master-key-for-unit-tests-only-32b"

import auth
import vault


def test_vault_roundtrip_sensitive_fields():
    uid = "user-abc"
    payload = {
        "api_key": "key-12345",
        "api_secret": "secret-67890",
        "private_key": "-----BEGIN KEY-----",
        "demo": True,
    }
    blob = vault.seal_sensitive_payload(uid, payload)
    out = vault.open_sensitive_payload(uid, blob)
    assert out["api_key"] == payload["api_key"]
    assert out["api_secret"] == payload["api_secret"]
    assert out["private_key"] == payload["private_key"]
    assert out["demo"] is True


def test_vault_user_isolation():
    a = vault.seal_sensitive_payload("user-a", {"api_key": "aaa"})
    try:
        vault.open_sensitive_payload("user-b", a)
        raised = False
    except Exception:
        raised = True
    assert raised


def test_totp_register_and_login_flow(tmp_path, monkeypatch):
    db = tmp_path / "accounts.db"
    monkeypatch.setenv("ACCOUNTS_DB_PATH", str(db))
    auth._conn = None

    reg = auth.register("trader1")
    assert reg["requires_2fa_setup"]
    secret = reg["totp_secret"]
    code = pyotp.TOTP(secret).now()

    token, user = auth.confirm_2fa_setup(reg["setup_token"], code)
    assert user["username"] == "trader1"
    assert user["totp_enabled"]
    assert token

    step = auth.login("trader1")
    assert step["requires_2fa"]
    code2 = pyotp.TOTP(secret).now()
    token2, user2 = auth.verify_2fa(step["challenge_token"], code2)
    assert user2["username"] == "trader1"
    assert auth.user_from_token(token2)
