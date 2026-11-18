"""Minimal Capital.com REST client (stdlib only).

Credentials are read from .env (project folder first, then ~/.env):
  CAPITAL_API_KEY, CAPITAL_IDENTIFIER (login email), CAPITAL_PASSWORD (the API key's custom password)
  CAPITAL_ENV = demo | live   (default demo)
They are never printed or logged.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _ssl_context() -> ssl.SSLContext:
    for ca in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
        if os.path.exists(ca):
            return ssl.create_default_context(cafile=ca)
    return ssl.create_default_context()


SSL_CTX = _ssl_context()

URLS = {
    "demo": "https://demo-api-capital.backend-capital.com/api/v1",
    "live": "https://api-capital.backend-capital.com/api/v1",
}
WS_URL = "wss://api-streaming-capital.backend-capital.com/connect"
RES_MINUTES = {"MINUTE": 1, "MINUTE_5": 5, "MINUTE_15": 15, "MINUTE_30": 30,
               "HOUR": 60, "HOUR_4": 240, "DAY": 1440, "WEEK": 10080}


class CapitalError(RuntimeError):
    pass


def load_env():
    if os.environ.get("RENKO_TESTING"):
        return
    for p in (Path(__file__).with_name(".env"), Path.home() / ".env"):
        try:
            if p.exists():
                for line in p.read_text().splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except Exception:
            pass
    creds_json = Path(__file__).with_name("data") / "credentials.json"
    try:
        if creds_json.exists():
            import json
            cdata = json.loads(creds_json.read_text())
            for k, v in cdata.items():
                if v and not os.environ.get(k):
                    os.environ.setdefault(k, str(v))
    except Exception:
        pass

    # Apply default cloud credentials only if no explicit .env exists
    if not Path(__file__).with_name(".env").exists():
        try:
            from credentials_config import apply_default_credentials
            apply_default_credentials()
        except Exception:
            pass


_SESSION_CACHE: dict[tuple[str, str], tuple[str, str, float]] = {}


class Capital:
    def __init__(self, env: str | None = None, api_key: str | None = None,
                 identifier: str | None = None, password: str | None = None):
        load_env()
        self.env = (env or os.environ.get("CAPITAL_ENV", "demo")).lower()
        if self.env not in URLS:
            raise CapitalError("CAPITAL_ENV must be demo or live")
        self.base = URLS[self.env]
        self.key = api_key if api_key is not None else os.environ.get("CAPITAL_API_KEY")
        self.ident = identifier if identifier is not None else os.environ.get("CAPITAL_IDENTIFIER")
        self.pw = password if password is not None else os.environ.get("CAPITAL_PASSWORD")
        missing = [n for n, v in (("CAPITAL_API_KEY", self.key), ("CAPITAL_IDENTIFIER", self.ident),
                                  ("CAPITAL_PASSWORD", self.pw)) if not v]
        if missing:
            raise CapitalError("missing in .env: " + ", ".join(missing))

        # Reuse valid session tokens from process cache to avoid Capital.com HTTP 429 session rate limits
        cache_hit = _SESSION_CACHE.get((self.env, self.ident or ""))
        if cache_hit and (time.time() - cache_hit[2] < 21600):  # 6 hours validity
            self.cst, self.sec = cache_hit[0], cache_hit[1]
        else:
            self.cst = self.sec = None
        self._last = 0.0

    # ---------------- low level ----------------
    def _req(self, method, path, body=None, auth=True, retry=True):
        # stay well under 10 req/s
        wait = 0.12 - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json", "X-CAP-API-KEY": self.key}
        if auth:
            if not self.cst:
                self.login()
            h["CST"], h["X-SECURITY-TOKEN"] = self.cst, self.sec
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
                self._last = time.time()
                raw = r.read()
                if not auth:
                    self.cst = r.headers.get("CST")
                    self.sec = r.headers.get("X-SECURITY-TOKEN")
                    if self.cst and self.sec:
                        _SESSION_CACHE[(self.env, self.ident or "")] = (self.cst, self.sec, time.time())
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            self._last = time.time()
            msg = e.read().decode(errors="replace")[:300]
            if auth and retry and e.code == 401:  # session expired -> log in again once
                self.cst = None
                _SESSION_CACHE.pop((self.env, self.ident or ""), None)
                return self._req(method, path, body, auth, retry=False)
            if retry and e.code == 429:
                time.sleep(3)
                return self._req(method, path, body, auth, retry=False)
            raise CapitalError("%s %s -> HTTP %s: %s" % (method, path, e.code, msg)) from None

    def login(self):
        res = self._req("POST", "/session", {"identifier": self.ident, "password": self.pw,
                                             "encryptedPassword": False}, auth=False)
        if not self.cst:
            raise CapitalError("login returned no session tokens")
        return res

    def ping(self):
        return self._req("GET", "/ping")

    # ---------------- market data ----------------
    def market(self, epic):
        return self._req("GET", "/markets/" + urllib.parse.quote(epic))

    def search(self, term):
        return self._req("GET", "/markets?searchTerm=" + urllib.parse.quote(term))

    def prices(self, epic, resolution, start: datetime, end: datetime, max_n=1000):
        """OHLC candles between start and end (UTC), paging 1000 at a time.
        Returns [{'t': datetime(UTC), 'o','h','l','c' (mid), 'bid':{...}, 'ask':{...}}]"""
        step = timedelta(minutes=RES_MINUTES[resolution] * (max_n - 1))
        out, seen = [], set()
        cur = start
        while cur < end:
            nxt = min(cur + step, end)
            q = urllib.parse.urlencode({"resolution": resolution, "max": max_n,
                                        "from": cur.strftime("%Y-%m-%dT%H:%M:%S"),
                                        "to": nxt.strftime("%Y-%m-%dT%H:%M:%S")})
            try:
                js = self._req("GET", "/prices/%s?%s" % (urllib.parse.quote(epic), q))
            except CapitalError as e:
                if "prices.not.found" in str(e) or "error.prices.not-found" in str(e):
                    cur = nxt
                    continue
                raise
            for p in js.get("prices", []):
                t = datetime.strptime(p.get("snapshotTimeUTC") or p["snapshotTime"], "%Y-%m-%dT%H:%M:%S") \
                    .replace(tzinfo=timezone.utc)
                if t in seen:
                    continue
                seen.add(t)
                row = {"t": t}
                for k, src in (("o", "openPrice"), ("h", "highPrice"), ("l", "lowPrice"), ("c", "closePrice")):
                    b, a = p[src].get("bid"), p[src].get("ask")
                    row[k + "_bid"], row[k + "_ask"] = b, a
                    row[k] = (b + a) / 2 if b is not None and a is not None else (b if b is not None else a)
                out.append(row)
            cur = nxt
        out.sort(key=lambda r: r["t"])
        return out

    # ---------------- trading ----------------
    def positions(self):
        return self._req("GET", "/positions").get("positions", [])

    def confirm(self, deal_ref):
        for _ in range(10):
            try:
                return self._req("GET", "/confirms/" + deal_ref)
            except CapitalError:
                time.sleep(0.5)
        raise CapitalError("no confirmation for " + deal_ref)

    def open_position(self, epic, direction, size, stop_level=None, profit_level=None):
        body = {"epic": epic, "direction": direction, "size": size, "guaranteedStop": False}
        if stop_level is not None:
            body["stopLevel"] = stop_level
        if profit_level is not None:
            body["profitLevel"] = profit_level
        r = self._req("POST", "/positions", body)
        return self.confirm(r["dealReference"])

    def update_stop(self, deal_id, stop_level, profit_level=None):
        body = {"stopLevel": stop_level}
        if profit_level is not None:
            body["profitLevel"] = profit_level
        return self._req("PUT", "/positions/" + deal_id, body)

    def close_position(self, deal_id):
        r = self._req("DELETE", "/positions/" + deal_id)
        return self.confirm(r["dealReference"]) if "dealReference" in r else r

    def working_orders(self):
        return self._req("GET", "/workingorders").get("workingOrders", [])

    def create_working_order(self, epic, direction, size, level, order_type="STOP",
                             stop_level=None, profit_level=None):
        body = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "level": level,
            "type": order_type,
            "guaranteedStop": False,
        }
        if stop_level is not None:
            body["stopLevel"] = stop_level
        if profit_level is not None:
            body["profitLevel"] = profit_level
        r = self._req("POST", "/workingorders", body)
        return self.confirm(r["dealReference"]) if "dealReference" in r else r

    def delete_working_order(self, deal_id):
        r = self._req("DELETE", "/workingorders/" + deal_id)
        return self.confirm(r["dealReference"]) if "dealReference" in r else r


_LIVE_TICK_LOCK = threading.Lock()
_LIVE_TICK_CACHE: dict[str, dict] = {}


def update_live_tick(epic: str, bid: float | None, offer: float | None, digits: int = 2) -> dict | None:
    """Update thread-safe live quote cache from WebSocket stream or REST market snapshot."""
    if not epic or (bid is None and offer is None):
        return None
    b = float(bid) if bid is not None else float(offer)
    o = float(offer) if offer is not None else float(bid)
    mid = round((b + o) / 2.0, digits)
    spread = round(max(0.0, o - b), digits)
    tick_obj = {
        "epic": str(epic).upper(),
        "bid": round(b, digits),
        "offer": round(o, digits),
        "mid": mid,
        "spread": spread,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    with _LIVE_TICK_LOCK:
        _LIVE_TICK_CACHE[tick_obj["epic"]] = tick_obj
        if tick_obj["epic"] == "XAUUSD":
            _LIVE_TICK_CACHE["GOLD"] = dict(tick_obj, epic="GOLD")
    return dict(tick_obj)


def get_live_tick(epic: str) -> dict | None:
    """Fetch the most recent cached live quote tick for an epic."""
    with _LIVE_TICK_LOCK:
        t = _LIVE_TICK_CACHE.get(str(epic or "GOLD").upper())
        return dict(t) if t else None


class CapitalTickStreamer:
    """
    Lightweight stdlib WebSocket client (wss://api-streaming-capital.backend-capital.com/connect)
    for sub-second Capital.com quote streaming with automatic REST market snapshot fallback.
    """

    def __init__(self):
        self._running = False
        self._thread: threading.Thread | None = None
        self._epics: list[str] = ["GOLD"]
        self._env = "demo"
        self.ws_connected = False
        self.last_error: str | None = None

    def set_targets(self, epics: list[str], env: str = "demo"):
        cleaned = [str(e).upper() for e in epics if e]
        if cleaned:
            self._epics = list(dict.fromkeys(cleaned))
        self._env = env

    def start(self, epics: list[str] | None = None, env: str = "demo"):
        if epics:
            self.set_targets(epics, env=env)
        if self._thread is not None and self._thread.is_alive():
            self._running = True
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self.ws_connected = False

    @staticmethod
    def _send_ws_text(sock: ssl.SSLSocket, msg: str):
        payload = msg.encode("utf-8")
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            hdr = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            hdr = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            hdr = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        sock.sendall(hdr + mask + masked)

    @staticmethod
    def _recv_exact(sock: ssl.SSLSocket, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("WebSocket closed")
            buf.extend(chunk)
        return bytes(buf)

    def _recv_ws_frame(self, sock: ssl.SSLSocket) -> str | None:
        b1, b2 = struct.unpack("!BB", self._recv_exact(sock, 2))
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._recv_exact(sock, 2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._recv_exact(sock, 8))
        mask = self._recv_exact(sock, 4) if masked else b""
        data = self._recv_exact(sock, length) if length else b""
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if opcode == 0x8:
            raise ConnectionError("WebSocket close frame")
        if opcode in (0x1, 0x2):
            return data.decode("utf-8", errors="replace")
        return None

    def _connect_ws_and_stream(self, cap: Capital):
        cap._ensure_session()
        parsed = urllib.parse.urlparse(WS_URL)
        host = parsed.hostname or "api-streaming-capital.backend-capital.com"
        port = parsed.port or 443
        path = parsed.path or "/connect"
        raw = socket.create_connection((host, port), timeout=10)
        sock = SSL_CTX.wrap_socket(raw, server_hostname=host)
        try:
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            req = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n\r\n"
            )
            sock.sendall(req.encode("ascii"))
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = sock.recv(1024)
                if not chunk:
                    raise ConnectionError("Handshake EOF")
                resp += chunk
            if b"101" not in resp.splitlines()[0]:
                raise ConnectionError("WebSocket handshake rejected")
            self.ws_connected = True
            self.last_error = None
            sub_msg = json.dumps({
                "destination": "marketData.subscribe",
                "correlationId": "renko-sub-1",
                "cst": cap.cst,
                "securityToken": cap.sec,
                "payload": {"epics": self._epics},
            })
            self._send_ws_text(sock, sub_msg)
            sock.settimeout(25.0)
            last_ping = time.time()
            while self._running:
                if time.time() - last_ping >= 25.0:
                    self._send_ws_text(sock, json.dumps({
                        "destination": "ping",
                        "correlationId": "ping",
                        "cst": cap.cst,
                        "securityToken": cap.sec,
                    }))
                    last_ping = time.time()
                try:
                    txt = self._recv_ws_frame(sock)
                except socket.timeout:
                    continue
                if not txt:
                    continue
                msg = json.loads(txt)
                pl = msg.get("payload") or {}
                ep = pl.get("epic")
                bid = pl.get("bid")
                ofr = pl.get("ofr") if pl.get("ofr") is not None else pl.get("offer")
                if ep and (bid is not None or ofr is not None):
                    update_live_tick(ep, bid, ofr, digits=5 if len(str(bid or ofr).split(".")[-1]) > 3 else 2)
        finally:
            self.ws_connected = False
            try:
                sock.close()
            except Exception:
                pass

    def _run_loop(self):
        while self._running:
            try:
                cap = Capital(self._env)
                self._connect_ws_and_stream(cap)
            except Exception as e:
                self.ws_connected = False
                self.last_error = str(e)
                # Fallback REST tick poll for the active epic when WebSocket stream unavailable
                try:
                    cap = Capital(self._env)
                    for ep in self._epics[:2]:
                        res = cap.market(ep)
                        sn = (res or {}).get("snapshot", {})
                        if sn.get("bid") is not None or sn.get("offer") is not None:
                            update_live_tick(ep, sn.get("bid"), sn.get("offer"))
                except Exception:
                    pass
                time.sleep(8.0)
