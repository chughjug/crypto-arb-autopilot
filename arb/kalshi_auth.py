"""Kalshi API request signing (REST + WebSocket handshake)."""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def kalshi_credentials_configured() -> bool:
    if not os.environ.get("KALSHI_API_KEY", "").strip():
        return False
    if os.environ.get("KALSHI_PRIVATE_KEY", "").strip():
        return True
    path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
    return bool(path and Path(path).expanduser().is_file())


def _pem_bytes() -> bytes:
    inline = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
    if inline:
        return inline.replace("\\n", "\n").encode()
    path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
    if not path:
        raise ValueError("Kalshi private key not configured")
    return Path(path).expanduser().read_bytes()


def load_private_key(path: Path | None = None):
    if path is not None:
        pem = path.read_bytes()
    else:
        pem = _pem_bytes()
    return serialization.load_pem_private_key(pem, password=None)


def sign_message(private_key, message: str) -> str:
    sig = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def ws_auth_headers(api_key: str, private_key_path: Path | None = None) -> dict[str, str]:
    """Headers for the Kalshi WebSocket handshake."""
    ts = str(int(time.time() * 1000))
    path = "/trade-api/ws/v2"
    key = load_private_key(private_key_path)
    sig = sign_message(key, ts + "GET" + path)
    return {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
    }


def rest_auth_headers(
    api_key: str,
    method: str,
    path: str,
    private_key_path: Path | None = None,
) -> dict[str, str]:
    ts = str(int(time.time() * 1000))
    key = load_private_key(private_key_path)
    sig = sign_message(key, ts + method.upper() + path)
    return {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
