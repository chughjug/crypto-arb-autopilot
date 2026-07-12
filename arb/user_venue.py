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


_POLY_HOST = "https://clob.polymarket.com"
_POLY_CHAIN_ID = 137
_POLY_PK_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_POLY_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_POLY_SIG_LABELS = {
    0: "EOA (MetaMask)",
    1: "email/Magic proxy",
    2: "Gnosis Safe",
    3: "API deposit wallet",
}


def normalize_polymarket_creds(creds: dict) -> dict[str, Any]:
    out = dict(creds)
    pk = str(creds.get("private_key") or "").strip()
    if pk and not pk.startswith("0x"):
        pk = f"0x{pk}"
    out["private_key"] = pk
    funder = str(creds.get("funder") or "").strip()
    if funder and not funder.startswith("0x"):
        funder = f"0x{funder}"
    out["funder"] = funder
    raw_sig = creds.get("signature_type")
    if raw_sig is not None and str(raw_sig).strip() != "":
        out["signature_type"] = int(raw_sig)
    return out


def _polymarket_signature_type(creds: dict) -> int | None:
    """CLOB signature type: 0=EOA, 1=POLY_PROXY (email/Magic), 2=GNOSIS_SAFE, 3=POLY_1271."""
    creds = normalize_polymarket_creds(creds)
    if creds.get("signature_type") is not None:
        return int(creds["signature_type"])
    if creds.get("funder"):
        return 1
    return None


def _polymarket_signature_candidates(creds: dict) -> list[int]:
    creds = normalize_polymarket_creds(creds)
    if creds.get("signature_type") is not None:
        return [int(creds["signature_type"])]
    if creds.get("funder"):
        # Email/Magic proxy first; "For API use only" deposit wallets use type 3.
        return [1, 3, 2, 0]
    return [0]


def _parse_polymarket_usdc(raw: dict) -> float | None:
    """Convert CLOB balance-allowance response (wei strings) to USD."""
    if not isinstance(raw, dict):
        return None
    bal = raw.get("balance")
    if bal is None:
        return None
    try:
        return round(int(str(bal)) / 1e6, 2)
    except (TypeError, ValueError):
        return None


def _friendly_polymarket_error(message: str, creds: dict) -> str:
    text = message[:400]
    lower = text.lower()
    if "could not create api key" in lower or "derive" in lower and "api" in lower:
        return (
            "Polymarket rejected your wallet private key. Paste the key from "
            "reveal.magic.link for the same email you use on Polymarket."
        )
    if "invalid asset type" in lower:
        return (
            "Polymarket balance query failed (invalid asset type). "
            "Reconnect — the app will retry signature types automatically."
        )
    if creds.get("funder") and ("401" in text or "unauthorized" in lower):
        return (
            "Polymarket auth failed for this funder + signature type. "
            "Use the address from polymarket.com/settings — email users usually "
            "need type 1 (proxy); 'For API use only' addresses need type 3."
        )
    return text


def _polymarket_fetch_balance(client) -> dict[str, Any]:
    from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

    try:
        return client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
        )
    except Exception as e:
        if "asset type" not in str(e).lower():
            raise
        return client.get_balance_allowance()


def _polymarket_clob_client(creds: dict):
    from py_clob_client_v2 import ClobClient

    creds = normalize_polymarket_creds(creds)
    private_key = creds["private_key"]
    funder = creds.get("funder") or None
    sig_type = _polymarket_signature_type(creds)
    base = {"host": _POLY_HOST, "chain_id": _POLY_CHAIN_ID, "key": private_key}
    client = ClobClient(**base)
    api_creds = client.create_or_derive_api_key()
    client_kwargs = {**base, "creds": api_creds, "funder": funder}
    if sig_type is not None:
        client_kwargs["signature_type"] = sig_type
    return ClobClient(**client_kwargs)


def verify_polymarket_credentials(creds: dict) -> dict[str, Any]:
    """Probe CLOB auth + pUSD balance; auto-detect signature type when funder is set."""
    creds = normalize_polymarket_creds(creds)
    if not creds.get("private_key"):
        return {"error": "Polymarket private key required"}
    if not _POLY_PK_RE.match(creds["private_key"]):
        return {
            "error": (
                "Private key should be 64 hex characters from reveal.magic.link "
                "(with or without 0x prefix)"
            ),
        }
    if creds.get("funder") and not _POLY_ADDR_RE.match(creds["funder"]):
        return {"error": "Funder should be a 0x… address from your Polymarket profile"}
    try:
        from py_clob_client_v2 import ClobClient  # noqa: F401
    except ImportError:
        return {"error": "py-clob-client-v2 not installed"}

    attempts: list[str] = []
    for sig in _polymarket_signature_candidates(creds):
        probe = {**creds, "signature_type": sig}
        try:
            client = _polymarket_clob_client(probe)
            bal = _polymarket_fetch_balance(client)
            if isinstance(bal, dict) and bal.get("balance") is not None:
                out: dict[str, Any] = {
                    "venue": "polymarket",
                    "raw": bal,
                    "signature_type": sig,
                }
                usdc = _parse_polymarket_usdc(bal)
                if usdc is not None:
                    out["usdc"] = usdc
                return out
        except Exception as e:
            label = _POLY_SIG_LABELS.get(sig, str(sig))
            attempts.append(f"{label}: {str(e)[:120]}")

    if creds.get("funder"):
        return {
            "error": (
                "Could not authenticate with Polymarket using your key + funder. "
                "Confirm the Magic private key matches your Polymarket email login, "
                "and the funder is from polymarket.com/settings — proxy address for "
                "email users (type 1) or 'For API use only' (type 3). "
                f"Attempts: {'; '.join(attempts[:4])}"
            ),
        }
    return {
        "error": (
            "Could not authenticate with Polymarket. Email/Magic users must paste "
            "the funder/proxy address from polymarket.com/settings. "
            f"Attempts: {'; '.join(attempts[:2])}"
        ),
    }


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
    """Best-effort pUSD balance via CLOB client."""
    creds = normalize_polymarket_creds(creds)
    if not creds.get("private_key"):
        return {"error": "Polymarket private key missing"}
    try:
        from py_clob_client_v2 import ClobClient  # noqa: F401
    except ImportError:
        return {"error": "py-clob-client-v2 not installed"}
    try:
        client = _polymarket_clob_client(creds)
        bal = _polymarket_fetch_balance(client)
        usdc = _parse_polymarket_usdc(bal)
        out: dict[str, Any] = {"venue": "polymarket", "raw": bal}
        if usdc is not None:
            out["usdc"] = usdc
        if creds.get("signature_type") is not None:
            out["signature_type"] = int(creds["signature_type"])
        return out
    except Exception as e:
        err = _friendly_polymarket_error(str(e), creds)
        if creds.get("funder"):
            probe = verify_polymarket_credentials(creds)
            if not probe.get("error"):
                return probe
        return {"error": err}


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
    if not private_key or not token_id:
        return {"error": "Polymarket live credentials or token missing"}
    try:
        from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side
    except ImportError:
        return {"error": "py-clob-client-v2 not installed"}
    try:
        client = _polymarket_clob_client(creds)
    except Exception as e:
        return {"error": str(e)}
    try:
        order = client.create_order(
            OrderArgs(token_id=token_id, price=price, size=size, side=Side.BUY),
            PartialCreateOrderOptions(order_type=OrderType.FOK),
        )
        return {"ok": True, "order": order}
    except Exception as e:
        return {"error": str(e)}
