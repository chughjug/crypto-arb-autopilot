"""Layered authenticated encryption for user secrets (venue keys, TOTP seeds, challenges)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

# Distinct HKDF contexts so keys are never reused across data types.
PURPOSE_VENUE_CREDS = b"crypto-arb/venue-creds/v1"
PURPOSE_TOTP = b"crypto-arb/totp-secret/v1"
PURPOSE_CHALLENGE = b"crypto-arb/login-challenge/v1"
PURPOSE_ENVELOPE = b"crypto-arb/master-envelope/v1"

_SENSITIVE_KEYS = frozenset({
    "api_key", "api_secret", "private_key", "funder", "password", "secret", "totp_secret",
})


def _require_master() -> bytes:
    raw = os.environ.get("AUTOPILOT_SECRET_KEY", "").strip()
    if os.environ.get("DYNO") and not raw:
        raise RuntimeError("AUTOPILOT_SECRET_KEY must be set in production")
    if not raw:
        raw = "dev-only-insecure-key-change-me"
    # Accept hex or arbitrary string; always derive a 32-byte key.
    if len(raw) == 64 and all(c in "0123456789abcdefABCDEF" for c in raw):
        return bytes.fromhex(raw)
    return hashlib.sha256(raw.encode()).digest()


def _derive(user_id: str, purpose: bytes) -> bytes:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=user_id.encode("utf-8"),
        info=purpose,
    )
    return hkdf.derive(_require_master())


def _aes_encrypt(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad)
    return nonce + ct


def _aes_decrypt(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if len(blob) < 13:
        raise ValueError("ciphertext too short")
    nonce, ct = blob[:12], blob[12:]
    return AESGCM(key).decrypt(nonce, ct, aad)


def _envelope_encrypt(user_id: str, inner_b64: str) -> str:
    """Second layer: master-derived envelope binds ciphertext to user_id."""
    key = _derive(user_id, PURPOSE_ENVELOPE)
    wrapped = _aes_encrypt(key, inner_b64.encode("utf-8"), user_id.encode("utf-8"))
    return base64.urlsafe_b64encode(wrapped).decode("ascii")


def _envelope_decrypt(user_id: str, blob: str) -> str:
    key = _derive(user_id, PURPOSE_ENVELOPE)
    raw = base64.urlsafe_b64decode(blob.encode("ascii"))
    return _aes_decrypt(key, raw, user_id.encode("utf-8")).decode("utf-8")


def _seal_field(value: str, field_key: bytes, user_id: str, field: str) -> str:
    aad = f"{user_id}:{field}".encode("utf-8")
    sealed = _aes_encrypt(field_key, value.encode("utf-8"), aad)
    return base64.urlsafe_b64encode(sealed).decode("ascii")


def _open_field(blob: str, field_key: bytes, user_id: str, field: str) -> str:
    aad = f"{user_id}:{field}".encode("utf-8")
    raw = base64.urlsafe_b64decode(blob.encode("ascii"))
    return _aes_decrypt(field_key, raw, aad).decode("utf-8")


def seal_sensitive_payload(user_id: str, data: dict[str, Any], *, purpose: bytes = PURPOSE_VENUE_CREDS) -> str:
    """Encrypt a dict: sensitive string fields sealed individually, then whole blob encrypted."""
    user_key = _derive(user_id, purpose)
    field_key = _derive(user_id, purpose + b"/fields")
    packed: dict[str, Any] = {}
    for k, v in data.items():
        if k in _SENSITIVE_KEYS and isinstance(v, str) and v:
            packed[k] = {"_enc": _seal_field(v, field_key, user_id, k)}
        else:
            packed[k] = v
    inner = base64.urlsafe_b64encode(
        _aes_encrypt(user_key, json.dumps(packed).encode("utf-8"), user_id.encode("utf-8"))
    ).decode("ascii")
    return _envelope_encrypt(user_id, inner)


def open_sensitive_payload(user_id: str, blob: str, *, purpose: bytes = PURPOSE_VENUE_CREDS) -> dict[str, Any]:
    user_key = _derive(user_id, purpose)
    field_key = _derive(user_id, purpose + b"/fields")
    inner_b64 = _envelope_decrypt(user_id, blob)
    raw = base64.urlsafe_b64decode(inner_b64.encode("ascii"))
    packed = json.loads(_aes_decrypt(user_key, raw, user_id.encode("utf-8")).decode("utf-8"))
    out: dict[str, Any] = {}
    for k, v in packed.items():
        if isinstance(v, dict) and "_enc" in v:
            out[k] = _open_field(v["_enc"], field_key, user_id, k)
        else:
            out[k] = v
    return out


def seal_string(user_id: str, value: str, *, purpose: bytes = PURPOSE_TOTP) -> str:
    key = _derive(user_id, purpose)
    inner = base64.urlsafe_b64encode(
        _aes_encrypt(key, value.encode("utf-8"), user_id.encode("utf-8"))
    ).decode("ascii")
    return _envelope_encrypt(user_id, inner)


def open_string(user_id: str, blob: str, *, purpose: bytes = PURPOSE_TOTP) -> str:
    key = _derive(user_id, purpose)
    inner_b64 = _envelope_decrypt(user_id, blob)
    raw = base64.urlsafe_b64decode(inner_b64.encode("ascii"))
    return _aes_decrypt(key, raw, user_id.encode("utf-8")).decode("utf-8")


def hash_token(token: str) -> str:
    pepper = _require_master()
    return hashlib.sha256(pepper + token.encode("utf-8")).hexdigest()
