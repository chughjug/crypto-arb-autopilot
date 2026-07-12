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


def test_credential_fingerprint_is_one_way():
    uid = "user-abc"
    payload = {"api_key": "key-12345", "api_secret": "secret-67890"}
    fp1 = vault.credential_fingerprint(uid, payload)
    fp2 = vault.credential_fingerprint(uid, {"api_key": "different", "api_secret": "secret-67890"})
    assert fp1
    assert fp1 != fp2
    assert "key-12345" not in fp1
    assert "secret-67890" not in fp1


def test_venue_credentials_stored_encrypted_not_plaintext(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_DB_PATH", str(tmp_path / "autopilot.db"))
    import autopilot_store

    autopilot_store._conn = None
    secrets = {
        "api_key": "kalshi-live-key-abcdef",
        "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
        "demo": True,
    }
    autopilot_store.save_venue_credentials("user1", "kalshi", secrets)
    row = autopilot_store._connect().execute(
        "SELECT enc_payload, key_fingerprint FROM venue_credentials WHERE user_id=? AND venue=?",
        ("user1", "kalshi"),
    ).fetchone()
    blob = row["enc_payload"]
    assert secrets["api_key"] not in blob
    assert secrets["private_key"] not in blob
    assert row["key_fingerprint"]
    assert secrets["api_key"] not in row["key_fingerprint"]
    opened = autopilot_store.get_venue_credentials("user1", "kalshi")
    assert opened["api_key"] == secrets["api_key"]


def test_append_log_redacts_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_DB_PATH", str(tmp_path / "autopilot.db"))
    import autopilot_store

    autopilot_store._conn = None
    autopilot_store.append_log("user1", "info", "test", {
        "api_key": "super-secret-key",
        "legs": {"kalshi_yes": {"order_id": "123"}},
    })
    row = autopilot_store._connect().execute(
        "SELECT detail FROM autopilot_log WHERE user_id=?", ("user1",)
    ).fetchone()
    assert "super-secret-key" not in row["detail"]
    assert "[redacted]" in row["detail"]


def test_totp_register_and_login_flow(tmp_path, monkeypatch):
    db = tmp_path / "accounts.db"
    monkeypatch.setenv("ACCOUNTS_DB_PATH", str(db))
    auth._conn = None

    reg = auth.register("trader1")
    assert reg["requires_2fa_setup"]
    auth._conn = None
    conn = auth._connect()
    assert conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
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
