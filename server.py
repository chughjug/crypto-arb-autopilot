#!/usr/bin/env python3
"""Crypto Arb Autopilot — standalone Heroku app for 3-venue crypto prediction markets."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import auth
import bots_hub
import catalog
import catalog_db
import crypto_arb
import crypto_arb_bot
import crypto_hub
from ws_util import ws_guid, ws_recv_frame, ws_send_frame

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"

PAGES = {
    "/": WEB / "crypto.html",
    "/crypto": WEB / "crypto.html",
    "/crypto-market": WEB / "crypto-market.html",
    "/cryptoarbitrage": WEB / "cryptoarbitrage.html",
    "/bots": WEB / "bots.html",
    "/autopilot": WEB / "autopilot.html",
    "/account": WEB / "account.html",
    "/market": WEB / "market.html",
}

_AUTH_PAGES = {"/autopilot"}

STATIC_JS = {
    "/header.js", "/account.js", "/auth-ui.js", "/live.js", "/viewmode.js", "/market-detail.js",
    "/bot-ledger.js", "/ap-ledger.js", "/theme.css", "/bot-panel.css", "/manifest.json",
}
STATIC_IMG = {
    "/icon-192.png", "/icon-512.png", "/apple-touch-icon.png",
    "/favicon-32.png", "/logo.png",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "CryptoArbApp/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("QUIET_LOGS") != "1":
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str = "text/plain", extra: list | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra or []:
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, code: int, data, extra: list | None = None):
        self._send(code, json.dumps(data).encode(), "application/json", extra)

    def _session_token(self) -> str | None:
        return auth.parse_cookie(self.headers.get("Cookie"))

    def _current_user(self):
        token = self._session_token()
        user = auth.user_from_token(token)
        return user, token, []

    def _require_user(self):
        user, token, extra = self._current_user()
        if not user:
            return None, token, extra
        return user, token, extra

    def _redirect(self, location: str, code: int = 302) -> None:
        self.send_response(code)
        self.send_header("Location", location)
        self.end_headers()

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw) if raw else {}

    def _serve_file(self, path: Path, ctype: str, max_age: int = 0):
        if not path.is_file():
            self._send(404, b"not found")
            return
        body = path.read_bytes()
        extra = [("Cache-Control", f"max-age={max_age}" if max_age else "no-store")]
        self._send(200, body, ctype, extra)

    def _serve_ws(self, build, interval: float, *, snapshot=None):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or "websocket" not in (self.headers.get("Upgrade") or "").lower():
            self._send_json(400, {"error": "expected websocket upgrade"})
            return
        accept = base64.b64encode(hashlib.sha1((key + ws_guid()).encode()).digest()).decode()
        try:
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return
        self.close_connection = True
        sock = self.connection
        last_ver = None
        try:
            while True:
                if snapshot:
                    try:
                        body, ver = snapshot()
                    except Exception as e:
                        body, ver = json.dumps({"error": str(e)}).encode(), None
                else:
                    try:
                        body = json.dumps(build()).encode()
                    except Exception as e:
                        body = json.dumps({"error": str(e)}).encode()
                    ver = hashlib.md5(body).digest()
                if ver is None or ver != last_ver:
                    try:
                        ws_send_frame(self.wfile, 0x1, body)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
                    last_ver = ver
                time.sleep(interval)
                try:
                    sock.settimeout(0.0)
                    frame = ws_recv_frame(self.rfile)
                    if frame is None:
                        return
                    op, data = frame
                    if op == 0x8:
                        return
                    if op == 0x9:
                        ws_send_frame(self.wfile, 0xA, data)
                except (socket.timeout, TimeoutError, BlockingIOError):
                    pass
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
        finally:
            try:
                sock.settimeout(None)
            except OSError:
                pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        if path.startswith("/ws/"):
            chan = path[len("/ws/"):]
            if chan == "crypto":
                self._serve_ws(crypto_hub.crypto_overview, 2, snapshot=crypto_hub.crypto_overview_bytes)
                return
            if chan == "crypto/series":
                coin = (qs.get("coin") or [""])[0]
                freq = (qs.get("freq") or [""])[0]
                src = (qs.get("src") or qs.get("source") or [""])[0]
                self._serve_ws(lambda: crypto_hub.crypto_series(coin, freq, src), 2)
                return
            self._send_json(404, {"error": "unknown channel"})
            return

        if path in ("/api/health",):
            self._send_json(200, {
                "ok": True,
                "catalog": catalog.cache_stats(),
                "crypto_arb": crypto_arb.snapshot().get("stats", {}),
            })
            return

        if path == "/api/crypto/overview":
            try:
                body, _ = crypto_hub.crypto_overview_bytes()
                self._send(200, body, "application/json")
            except Exception as e:
                self._send_json(502, {"error": str(e)})
            return

        if path == "/api/crypto/series":
            coin = (qs.get("coin") or [""])[0]
            freq = (qs.get("freq") or [""])[0]
            src = (qs.get("src") or qs.get("source") or [""])[0]
            try:
                self._send_json(200, crypto_hub.crypto_series(coin, freq, src))
            except Exception as e:
                self._send_json(502, {"error": str(e)})
            return

        if path == "/api/cryptoarbitrage":
            try:
                self._send_json(200, crypto_arb.snapshot())
            except Exception as e:
                self._send_json(502, {"error": str(e)})
            return

        if path == "/api/cryptoarbitrage/bot":
            try:
                self._send_json(200, crypto_arb_bot.snapshot())
            except Exception as e:
                self._send_json(502, {"error": str(e)})
            return

        if path.startswith("/api/cryptoarbitrage/bot/"):
            sid = path.rsplit("/", 1)[-1]
            snap = crypto_arb_bot.strategy_snapshot(sid)
            if snap is None:
                self._send_json(404, {"error": f"strategy {sid} not found"})
            else:
                self._send_json(200, snap)
            return

        if path == "/api/bots/comparison":
            try:
                self._send_json(200, bots_hub.comparison())
            except Exception as e:
                self._send_json(502, {"error": str(e)})
            return

        if path == "/api/explore":
            q = (qs.get("q") or [""])[0].strip()
            venue = (qs.get("venue") or ["all"])[0]
            try:
                limit = max(1, min(96, int((qs.get("limit") or ["48"])[0])))
                offset = max(0, int((qs.get("offset") or ["0"])[0]))
            except ValueError:
                limit, offset = 48, 0
            try:
                self._send_json(200, crypto_hub.explore_crypto(q=q, limit=limit, offset=offset, venue=venue))
            except Exception as e:
                self._send_json(502, {"error": str(e)})
            return

        if path == "/api/market":
            venue = (qs.get("venue") or [""])[0]
            key = (qs.get("id") or qs.get("slug") or qs.get("ticker") or [""])[0]
            if venue not in ("kalshi", "polymarket", "cryptocom") or not key:
                self._send_json(400, {"error": "need venue and id"})
                return
            data = crypto_hub.market_payload(venue, key)
            if not data:
                self._send_json(404, {"error": "market not found"})
            else:
                self._send_json(200, data)
            return

        if path == "/api/market/explain":
            venue = (qs.get("venue") or [""])[0]
            key = (qs.get("id") or qs.get("slug") or qs.get("ticker") or [""])[0]
            if not venue or not key:
                self._send_json(400, {"error": "need venue and id"})
                return
            self._send_json(200, crypto_hub.market_explain(venue, key))
            return

        if path == "/api/market/activity":
            venue = (qs.get("venue") or [""])[0]
            key = (qs.get("id") or qs.get("slug") or qs.get("ticker") or [""])[0]
            outcome = (qs.get("outcome") or [""])[0]
            try:
                hours = max(1, min(720, int((qs.get("hours") or ["24"])[0])))
                min_usd = max(50, float((qs.get("min_usd") or ["500"])[0]))
            except ValueError:
                hours, min_usd = 24, 500
            rollup = (qs.get("rollup") or ["1"])[0] not in ("0", "false", "no")
            data = crypto_hub.market_activity_payload(venue, key, outcome=outcome, hours=hours, min_usd=min_usd, rollup=rollup)
            if not data:
                self._send_json(404, {"error": "market not found"})
            else:
                self._send_json(200, data)
            return

        if path == "/api/auth/me":
            user, _, extra = self._current_user()
            self._send_json(200, {"user": user}, extra)
            return

        if path == "/api/auth/2fa/qr":
            qs = parse_qs(parsed.query)
            uri = (qs.get("data") or [""])[0]
            if not uri.startswith("otpauth://"):
                self._send(400, b"invalid otpauth uri")
                return
            try:
                import io

                import segno

                buf = io.BytesIO()
                segno.make(uri).save(buf, kind="png", scale=6, border=2)
                self._send(200, buf.getvalue(), "image/png", [("Cache-Control", "no-store")])
            except Exception:
                self._send(500, b"qr generation failed")
            return

        if path == "/api/autopilot/strategies":
            user, _, extra = self._current_user()
            if not user:
                self._send_json(401, {"error": "sign in required"}, extra)
                return
            import autopilot_engine
            self._send_json(200, {"strategies": autopilot_engine.strategy_catalog()}, extra)
            return

        if path == "/api/autopilot/status":
            user, _, extra = self._current_user()
            if not user:
                self._send_json(401, {"error": "sign in required"}, extra)
                return
            import autopilot_engine
            qs = parse_qs(parsed.query)
            include_balances = (qs.get("balances") or ["0"])[0].lower() in ("1", "true", "yes")
            try:
                payload = autopilot_engine.get_runner(user["id"]).status(
                    include_balances=include_balances,
                )
            except Exception as e:
                self._send_json(500, {"error": str(e)}, extra)
                return
            self._send_json(200, payload, extra)
            return

        if path == "/api/autopilot/balances":
            user, _, extra = self._current_user()
            if not user:
                self._send_json(401, {"error": "sign in required"}, extra)
                return
            import autopilot_executor
            try:
                payload = autopilot_executor.venue_balances(user["id"])
            except Exception as e:
                self._send_json(500, {"error": str(e)}, extra)
                return
            self._send_json(200, payload, extra)
            return

        if path == "/api/autopilot/logs":
            user, _, extra = self._current_user()
            if not user:
                self._send_json(401, {"error": "sign in required"}, extra)
                return
            import autopilot_store
            self._send_json(200, {"logs": autopilot_store.recent_logs(user["id"])}, extra)
            return

        if path == "/api/autopilot/bankroll":
            user, _, extra = self._current_user()
            if not user:
                self._send_json(401, {"error": "sign in required"}, extra)
                return
            import autopilot_bankroll
            self._send_json(200, autopilot_bankroll.bankroll_payload(user["id"]), extra)
            return

        if path == "/api/autopilot/trades":
            user, _, extra = self._current_user()
            if not user:
                self._send_json(401, {"error": "sign in required"}, extra)
                return
            import autopilot_store
            qs = parse_qs(parsed.query)
            try:
                limit = max(1, min(500, int((qs.get("limit") or ["100"])[0])))
            except ValueError:
                limit = 100
            self._send_json(200, {
                "trades": autopilot_store.recent_trades(user["id"], limit=limit),
                "stats": autopilot_store.trade_stats(user["id"]),
                "db": __import__("db").backend_label(),
            }, extra)
            return

        if path == "/bankroll":
            self._redirect("/autopilot?tab=bankroll")
            return

        # HTML pages
        page_key = path
        if path.endswith(".html"):
            page_key = "/" + path.split("/")[-1].replace(".html", "")
        html = PAGES.get(path) or PAGES.get(page_key)
        if html and html.is_file():
            if page_key in _AUTH_PAGES:
                user, _, _ = self._current_user()
                if not user:
                    next_url = page_key + (("?" + parsed.query) if parsed.query else "")
                    self._redirect("/account?signin=1&next=" + quote(next_url, safe="/?=&"))
                    return
            self._send(200, html.read_bytes(), "text/html; charset=utf-8")
            return

        if path.startswith("/cryptocom/") or path.startswith("/kalshi/") or path.startswith("/polymarket/"):
            p = WEB / "market.html"
            if p.is_file():
                self._send(200, p.read_bytes(), "text/html; charset=utf-8")
                return

        if path in STATIC_JS or path.split("?")[0] in STATIC_JS:
            rel = path.lstrip("/").split("?")[0]
            ctype = "text/css; charset=utf-8" if rel.endswith(".css") else "application/javascript; charset=utf-8"
            if rel == "manifest.json":
                ctype = "application/manifest+json; charset=utf-8"
            cache = 0 if rel in ("header.js", "account.js", "auth-ui.js") else 3600
            self._serve_file(WEB / rel, ctype, max_age=cache)
            return

        if path in STATIC_IMG:
            self._serve_file(WEB / path.lstrip("/"), "image/png", max_age=604800)
            return

        self._send(404, b"not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/auth/register":
            _, _, extra = self._current_user()
            body = self._read_json()
            try:
                result = auth.register(body.get("username") or "")
                self._send_json(200, result, extra)
            except ValueError as e:
                self._send_json(400, {"error": str(e)}, extra)
            return

        if path == "/api/auth/login":
            _, _, extra = self._current_user()
            body = self._read_json()
            try:
                result = auth.login(body.get("username") or "")
                self._send_json(200, result, extra)
            except ValueError as e:
                self._send_json(400, {"error": str(e)}, extra)
            return

        if path == "/api/auth/2fa/verify":
            body = self._read_json()
            try:
                token, user = auth.verify_2fa(
                    body.get("challenge_token") or "",
                    body.get("code") or "",
                )
                self._send_json(200, {"user": user}, [("Set-Cookie", auth.cookie_header(token))])
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
            return

        if path == "/api/auth/2fa/confirm":
            body = self._read_json()
            try:
                token, user = auth.confirm_2fa_setup(
                    body.get("setup_token") or "",
                    body.get("code") or "",
                )
                self._send_json(200, {"user": user}, [("Set-Cookie", auth.cookie_header(token))])
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
            return

        if path == "/api/auth/logout":
            token = self._session_token()
            if token:
                auth.logout(token)
            self._send_json(200, {"ok": True}, [("Set-Cookie", auth.clear_cookie_header())])
            return

        if path == "/api/autopilot/config":
            user, _, extra = self._current_user()
            if not user:
                self._send_json(401, {"error": "sign in required"}, extra)
                return
            import autopilot_store
            cfg = autopilot_store.save_config(user["id"], self._read_json())
            self._send_json(200, {"config": cfg}, extra)
            return

        if path == "/api/autopilot/connect/kalshi":
            user, _, extra = self._current_user()
            if not user:
                self._send_json(401, {"error": "sign in required"}, extra)
                return
            import autopilot_store
            from arb import user_venue

            body = self._read_json()
            if not body.get("api_key") or not body.get("private_key"):
                self._send_json(400, {"error": "api_key and private_key required"}, extra)
                return
            payload = {
                "api_key": body["api_key"].strip(),
                "private_key": body["private_key"].strip(),
                "demo": bool(body.get("demo", True)),
            }
            check = user_venue.verify_kalshi_credentials(payload)
            if check.get("error"):
                self._send_json(400, {"error": check["error"]}, extra)
                return
            status = autopilot_store.save_venue_credentials(user["id"], "kalshi", payload)
            self._send_json(200, {"venue": status, "balance": check}, extra)
            return

        if path == "/api/autopilot/connect/polymarket":
            user, _, extra = self._current_user()
            if not user:
                self._send_json(401, {"error": "sign in required"}, extra)
                return
            import autopilot_store
            from arb import user_venue

            body = self._read_json()
            if not body.get("private_key"):
                self._send_json(400, {"error": "private_key required"}, extra)
                return
            payload: dict[str, Any] = {
                "private_key": body["private_key"].strip(),
                "funder": (body.get("funder") or "").strip(),
            }
            if body.get("signature_type") is not None and str(body.get("signature_type")).strip() != "":
                payload["signature_type"] = int(body["signature_type"])
            check = user_venue.verify_polymarket_credentials(payload)
            if check.get("error"):
                self._send_json(400, {"error": check["error"]}, extra)
                return
            if check.get("signature_type") is not None:
                payload["signature_type"] = int(check["signature_type"])
            status = autopilot_store.save_venue_credentials(user["id"], "polymarket", payload)
            self._send_json(200, {"venue": status, "balance": check}, extra)
            return

        if path == "/api/autopilot/connect/cryptocom":
            user, _, extra = self._current_user()
            if not user:
                self._send_json(401, {"error": "sign in required"}, extra)
                return
            import autopilot_store
            body = self._read_json()
            if not body.get("api_key") or not body.get("api_secret"):
                self._send_json(400, {"error": "api_key and api_secret required"}, extra)
                return
            status = autopilot_store.save_venue_credentials(user["id"], "cryptocom", {
                "api_key": body["api_key"].strip(),
                "api_secret": body["api_secret"].strip(),
            })
            self._send_json(200, {"venue": status}, extra)
            return

        if path == "/api/autopilot/disconnect":
            user, _, extra = self._current_user()
            if not user:
                self._send_json(401, {"error": "sign in required"}, extra)
                return
            import autopilot_store
            body = self._read_json()
            venue = (body.get("venue") or "").strip().lower()
            if venue not in autopilot_store.VENUES:
                self._send_json(400, {"error": "invalid venue"}, extra)
                return
            autopilot_store.delete_venue_credentials(user["id"], venue)
            self._send_json(200, {"ok": True}, extra)
            return

        if path == "/api/autopilot/start":
            user, _, extra = self._current_user()
            if not user:
                self._send_json(401, {"error": "sign in required"}, extra)
                return
            import autopilot_engine
            self._send_json(200, autopilot_engine.get_runner(user["id"]).start(), extra)
            return

        if path == "/api/autopilot/stop":
            user, _, extra = self._current_user()
            if not user:
                self._send_json(401, {"error": "sign in required"}, extra)
                return
            import autopilot_engine
            self._send_json(200, autopilot_engine.get_runner(user["id"]).stop(), extra)
            return

        if path == "/api/cryptoarbitrage/bot/config":
            try:
                self._send_json(200, crypto_arb_bot.set_config(self._read_json() or {}))
            except Exception as e:
                self._send_json(502, {"error": str(e)})
            return

        self._send_json(404, {"error": "not found"})


def main():
    port = int(os.environ.get("PORT", sys.argv[1] if len(sys.argv) > 1 else 8000))
    host = "0.0.0.0"
    catalog_db.init_db()
    catalog.start_background_refresh()
    crypto_hub.start_crypto_live_refresh()
    crypto_arb.start_poller()
    crypto_arb_bot.snapshot()  # warm paper bots
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Crypto Arb Autopilot at http://{host}:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
