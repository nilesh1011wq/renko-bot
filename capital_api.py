"""Minimal Capital.com REST client (stdlib only).

Credentials are read from .env (project folder first, then ~/.env):
  CAPITAL_API_KEY, CAPITAL_IDENTIFIER (login email), CAPITAL_PASSWORD (the API key's custom password)
  CAPITAL_ENV = demo | live   (default demo)
They are never printed or logged.
"""
from __future__ import annotations

import json
import os
import ssl
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
RES_MINUTES = {"MINUTE": 1, "MINUTE_5": 5, "MINUTE_15": 15, "MINUTE_30": 30,
               "HOUR": 60, "HOUR_4": 240, "DAY": 1440, "WEEK": 10080}


class CapitalError(RuntimeError):
    pass


def load_env():
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
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            self._last = time.time()
            msg = e.read().decode(errors="replace")[:300]
            if auth and retry and e.code == 401:  # session expired -> log in again once
                self.cst = None
                return self._req(method, path, body, auth, retry=False)
            if retry and e.code == 429:
                time.sleep(2)
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

    def open_position(self, epic, direction, size, stop_level=None):
        body = {"epic": epic, "direction": direction, "size": size, "guaranteedStop": False}
        if stop_level is not None:
            body["stopLevel"] = stop_level
        r = self._req("POST", "/positions", body)
        return self.confirm(r["dealReference"])

    def update_stop(self, deal_id, stop_level):
        return self._req("PUT", "/positions/" + deal_id, {"stopLevel": stop_level})

    def close_position(self, deal_id):
        r = self._req("DELETE", "/positions/" + deal_id)
        return self.confirm(r["dealReference"]) if "dealReference" in r else r
