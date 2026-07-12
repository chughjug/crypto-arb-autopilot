"""Per-user Kalshi and Polymarket API calls (credentials passed in, not env)."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from arb.kalshi_auth import sign_message
from cryptography.hazmat.primitives import serialization

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)


def _kalshi_rest_base(demo: bool) -> str:
    if demo:
        return "https://external-api.demo.kalshi.co"
    return "https://external-api.kalshi.com"


def normalize_kalshi_creds(creds: dict) -> dict[str, Any]:
    """Normalize API key + PEM formatting from UI paste."""
    out = dict(creds)
    out["api_key"] = str(creds.get("api_key") or "").strip()
    pem = str(creds.get("private_key") or "").strip()
    pem = pem.replace("\\n", "\n").replace("\r\n", "\n")
    if pem and "BEGIN" not in pem:
        pem = pem.replace(" ", "\n")
    out["private_key"] = pem
    out["demo"] = bool(creds.get("demo", True))
    return out


def _load_private_key(pem: str):
    try:
        return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except Exception as e:
        raise ValueError(
            "Invalid Kalshi private key PEM — paste the full key including "
            "-----BEGIN ... PRIVATE KEY----- lines"
        ) from e


def _kalshi_headers(api_key: str, private_key_pem: str, method: str, path: str) -> dict[str, str]:
    import time

    key = _load_private_key(private_key_pem)
    ts = str(int(time.time() * 1000))
    sig = sign_message(key, ts + method.upper() + path)
    return {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _friendly_kalshi_error(status: int, body: str, demo: bool) -> str:
    text = body[:400]
    if status == 401 and "NOT_FOUND" in text:
        env = "demo" if demo else "production"
        other = "production" if demo else "demo"
        return (
            f"Kalshi rejected your {env} API key (NOT_FOUND). "
            f"Keys are environment-specific — if you created this key at "
            f"{'demo.kalshi.co' if other == 'demo' else 'kalshi.com'}, "
            f"switch Kalshi mode to {other.title()} and reconnect. "
            f"Also confirm the API key is the UUID from the dashboard."
        )
    if status == 401:
        return f"Kalshi authentication failed ({'demo' if demo else 'production'}): {text}"
    return f"HTTP {status}: {text}"


def _kalshi_request(creds: dict, method: str, path: str, body: bytes | None = None) -> dict[str, Any]:
    creds = normalize_kalshi_creds(creds)
    api_key = creds["api_key"]
    pem = creds["private_key"]
    demo = creds["demo"]
    if not api_key or not pem:
        return {"error": "Kalshi credentials incomplete"}
    url = f"{_kalshi_rest_base(demo)}{path}"
    try:
        headers = _kalshi_headers(api_key, pem, method, path)
    except ValueError as e:
        return {"error": str(e)}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        return {"error": _friendly_kalshi_error(e.code, raw, demo)}
    except Exception as e:
        return {"error": str(e)}


def verify_kalshi_credentials(creds: dict) -> dict[str, Any]:
    """Check balance; if NOT_FOUND, hint if the key works on the other environment."""
    creds = normalize_kalshi_creds(creds)
    if creds["api_key"] and not _UUID_RE.match(creds["api_key"]):
        return {
            "error": (
                "Kalshi API key should be the UUID from Account → API Keys "
                "(not the key's display name)"
            ),
        }
    bal = kalshi_balance(creds)
    if not bal.get("error"):
        return bal
    err = bal["error"]
    if "NOT_FOUND" not in err:
        return bal
    flipped = {**creds, "demo": not creds["demo"]}
    probe = kalshi_balance(flipped)
    if not probe.get("error"):
        want = "Production" if flipped["demo"] is False else "Demo"
        return {
            "error": (
                f"This API key works on {want}, but you selected "
                f"{'Demo' if creds['demo'] else 'Production'}. "
                f"Change Kalshi mode to {want} and connect again."
            ),
        }
    return bal


def kalshi_balance(creds: dict) -> dict[str, Any]:
    path = "/trade-api/v2/portfolio/balance"
    data = _kalshi_request(creds, "GET", path)
    if data.get("error"):
        return data
    b = data.get("balance")
    demo = normalize_kalshi_creds(creds)["demo"]
    if isinstance(b, dict):
        cash = int(b.get("balance") or 0)
        pv = int(b.get("portfolio_value") or cash)
    else:
        cash = int(data.get("balance") or 0)
        pv = int(data.get("portfolio_value") or cash)
    return {"venue": "kalshi", "cash_cents": cash, "equity_cents": pv, "demo": demo}


def kalshi_place_order(creds: dict, ticker: str, outcome: str, count: int, price_cents: int) -> dict[str, Any]:
    import uuid

    creds = normalize_kalshi_creds(creds)
    px = int(max(1, min(99, round(price_cents))))
    payload = {
        "ticker": ticker,
        "client_order_id": str(uuid.uuid4()),
        "action": "buy",
        "side": outcome,
        "count": int(count),
        "type": "limit",
        "yes_price" if outcome == "yes" else "no_price": px,
        "time_in_force": "immediate_or_cancel",
    }
    path = "/trade-api/v2/portfolio/orders"
    data = _kalshi_request(creds, "POST", path, json.dumps(payload).encode())
    if data.get("error"):
        return data
    return data


def polymarket_balance(creds: dict) -> dict[str, Any]:
    """Best-effort USDC balance via CLOB client."""
    private_key = creds.get("private_key", "").strip()
    funder = creds.get("funder", "").strip()
    if not private_key:
        return {"error": "Polymarket private key missing"}
    try:
        from py_clob_client_v2 import ClobClient
    except ImportError:
        return {"error": "py-clob-client-v2 not installed"}
    host = "https://clob.polymarket.com"
    chain_id = 137
    try:
        client = ClobClient(host=host, chain_id=chain_id, key=private_key)
        api_creds = client.create_or_derive_api_key()
        client = ClobClient(
            host=host, chain_id=chain_id, key=private_key,
            creds=api_creds, funder=funder or None,
        )
        bal = client.get_balance_allowance()
        return {"venue": "polymarket", "raw": bal}
    except Exception as e:
        return {"error": str(e)}


def polymarket_buy(
    creds: dict,
    *,
    market_id: str,
    token_id: str,
    price: float,
    size: float,
    live: bool,
) -> dict[str, Any]:
    if not live:
        from arb import poly_paper
        return poly_paper.buy(market_id, token_id, "YES", size)
    private_key = creds.get("private_key", "").strip()
    funder = creds.get("funder", "").strip()
    if not private_key or not token_id:
        return {"error": "Polymarket live credentials or token missing"}
    try:
        from py_clob_client_v2 import ClobClient, OrderArgs, OrderType, PartialCreateOrderOptions, Side
    except ImportError:
        return {"error": "py-clob-client-v2 not installed"}
    host = "https://clob.polymarket.com"
    chain_id = 137
    client = ClobClient(host=host, chain_id=chain_id, key=private_key)
    api_creds = client.create_or_derive_api_key()
    client = ClobClient(
        host=host, chain_id=chain_id, key=private_key,
        creds=api_creds, funder=funder or None,
    )
    try:
        order = client.create_order(
            OrderArgs(token_id=token_id, price=price, size=size, side=Side.BUY),
            PartialCreateOrderOptions(order_type=OrderType.FOK),
        )
        return {"ok": True, "order": order}
    except Exception as e:
        return {"error": str(e)}
