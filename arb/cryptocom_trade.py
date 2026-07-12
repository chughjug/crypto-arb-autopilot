"""Crypto.com Exchange API client — HMAC-signed private requests."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from typing import Any

BASE_URL = "https://api.crypto.com/exchange/v1"


def _params_to_str(obj, level: int = 0) -> str:
    if level >= 4:
        return str(obj)
    return_str = ""
    for key in sorted(obj):
        return_str += key
        val = obj[key]
        if isinstance(val, list):
            for sub in val:
                return_str += _params_to_str(sub, level + 1)
        elif isinstance(val, dict):
            return_str += _params_to_str(val, level + 1)
        else:
            return_str += str(val)
    return return_str


def _sign(secret: str, method: str, req_id: int, api_key: str, params: dict, nonce: int) -> str:
    param_str = _params_to_str(params) if params else ""
    payload = f"{method}{req_id}{api_key}{param_str}{nonce}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def private_request(
    creds: dict,
    method: str,
    params: dict | None = None,
    *,
    timeout: int = 20,
) -> dict[str, Any]:
    api_key = (creds.get("api_key") or "").strip()
    secret = (creds.get("api_secret") or "").strip()
    if not api_key or not secret:
        return {"error": "Crypto.com API key and secret required"}
    params = params or {}
    req_id = int(time.time() * 1000)
    nonce = req_id
    body = {
        "id": req_id,
        "method": method,
        "api_key": api_key,
        "params": params,
        "nonce": nonce,
    }
    body["sig"] = _sign(secret, method, req_id, api_key, params, nonce)
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/{method}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode(errors='replace')[:400]}"}
    except Exception as e:
        return {"error": str(e)}
    if out.get("code") != 0:
        return {"error": out.get("message") or out.get("code"), "raw": out}
    return out.get("result") or out


def balance(creds: dict) -> dict[str, Any]:
    """USDT available balance (best-effort)."""
    res = private_request(creds, "private/user-balance", {"currency": "USDT"})
    if res.get("error"):
        return {"venue": "cryptocom", "error": res["error"]}
    data = res.get("data") if isinstance(res, dict) else res
    avail = 0.0
    if isinstance(data, list):
        for row in data:
            if (row.get("currency") or "").upper() == "USDT":
                avail = float(row.get("available") or row.get("balance") or 0)
                break
    elif isinstance(data, dict):
        avail = float(data.get("available") or data.get("balance") or 0)
    return {"venue": "cryptocom", "cash_usd": avail, "equity_usd": avail}


def place_predict_order(
    creds: dict,
    *,
    contract_id: str,
    side: str,
    price: float,
    quantity: float,
) -> dict[str, Any]:
    """Place a limit order on a Predict contract via Exchange API.

    Crypto.com maps prediction contracts to exchange instruments; instrument_name
    may be the contract id or symbol depending on product. Callers pass contract_id
    from the Predict API quote."""
    instrument = str(contract_id)
    side_u = side.upper()
    if side_u not in ("BUY", "SELL"):
        side_u = "BUY"
    params = {
        "instrument_name": instrument,
        "side": side_u,
        "type": "LIMIT",
        "price": f"{price:.4f}",
        "quantity": f"{quantity:.4f}",
        "time_in_force": "GOOD_TILL_CANCEL",
    }
    res = private_request(creds, "private/create-order", params)
    if isinstance(res, dict) and res.get("error"):
        return res
    return {"ok": True, "result": res}
