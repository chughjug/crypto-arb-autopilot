"""Per-user Kalshi and Polymarket API calls (credentials passed in, not env)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from arb.kalshi_auth import sign_message
from cryptography.hazmat.primitives import serialization


def _kalshi_rest_base(demo: bool) -> str:
    return "https://demo-api.kalshi.co" if demo else "https://api.elections.kalshi.com"


def _kalshi_headers(api_key: str, private_key_pem: str, method: str, path: str) -> dict[str, str]:
    import time

    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    ts = str(int(time.time() * 1000))
    sig = sign_message(key, ts + method.upper() + path)
    return {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def kalshi_balance(creds: dict) -> dict[str, Any]:
    api_key = creds.get("api_key", "").strip()
    pem = creds.get("private_key", "").strip().replace("\\n", "\n")
    demo = bool(creds.get("demo", True))
    if not api_key or not pem:
        return {"error": "Kalshi credentials incomplete"}
    path = "/trade-api/v2/portfolio/balance"
    url = f"{_kalshi_rest_base(demo)}{path}"
    req = urllib.request.Request(url, headers=_kalshi_headers(api_key, pem, "GET", path), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"}
    except Exception as e:
        return {"error": str(e)}
    b = data.get("balance")
    if isinstance(b, dict):
        cash = int(b.get("balance") or 0)
        pv = int(b.get("portfolio_value") or cash)
    else:
        cash = int(data.get("balance") or 0)
        pv = int(data.get("portfolio_value") or cash)
    return {"venue": "kalshi", "cash_cents": cash, "equity_cents": pv, "demo": demo}


def kalshi_place_order(creds: dict, ticker: str, outcome: str, count: int, price_cents: int) -> dict[str, Any]:
    import uuid

    api_key = creds.get("api_key", "").strip()
    pem = creds.get("private_key", "").strip().replace("\\n", "\n")
    demo = bool(creds.get("demo", True))
    if not api_key or not pem:
        return {"error": "Kalshi credentials incomplete"}
    path = "/trade-api/v2/portfolio/events/orders"
    if outcome == "yes":
        book_side = "bid"
        price_dollars = max(0.01, min(0.99, price_cents / 100.0))
    else:
        book_side = "ask"
        price_dollars = max(0.01, min(0.99, (100 - price_cents) / 100.0))
    payload = {
        "ticker": ticker,
        "client_order_id": str(uuid.uuid4()),
        "side": book_side,
        "count": f"{count:.2f}",
        "price": f"{price_dollars:.4f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
    }
    body = json.dumps(payload).encode()
    url = f"{_kalshi_rest_base(demo)}{path}"
    headers = _kalshi_headers(api_key, pem, "POST", path)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"}
    except Exception as e:
        return {"error": str(e)}


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
