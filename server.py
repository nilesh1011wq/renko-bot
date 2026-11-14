"""Cloud-Ready HTTP Server, REST API & 24/7 Background Bot Worker for Renko ATR Cockpit.

Features for Internet / Cloud Hosting (Railway, Render, Fly.io, Docker, VPS):
- Binds to 0.0.0.0:$PORT (falls back to 8765 locally).
- Multi-threaded HTTP server (ThreadingHTTPServer).
- Built-in Password Authentication Gate (DASHBOARD_PASSWORD env var):
  * If DASHBOARD_PASSWORD is set, all API routes & actions require a valid signed session
    token (HttpOnly cookie or Authorization: Bearer <token>).
  * Constant-time HMAC comparison prevents timing attacks.
- Built-in 24/7 Background Watcher Thread:
  * Start / Pause / Step the live Capital.com candle watcher directly from the web UI
    (or auto-start on boot via AUTO_START_WATCHER=true).
- Optional Telegram Webhook / Bot Notifications (TELEGRAM_BOT_TOKEN & TELEGRAM_CHAT_ID)
  for instant mobile alerts when a Semi-Auto Ticket or trade triggers.
- Health check endpoint: GET /healthz (unauthenticated, for Railway/Render/Fly probes).
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import hmac
import io
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http. cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from backtest import (DATA, JOURNAL, INSTRUMENT_SPECS, data_path, ensure_data, generate_sample_gold_data,
                      load_config, save_bars, save_config, session_breakdown,
                      simulate, stats, write_trades)
from bot import LIVE_TRADES, PENDING_FILE, resolve_epic_alias, step_once, step_multi_market
from capital_api import Capital, CapitalError, RES_MINUTES, SSL_CTX, load_env
from montecarlo import run_montecarlo
from risk import kill_switch_active, set_kill_switch, today_stats

ROOT = Path(__file__).parent
STATIC = ROOT / "static"


# ----------------------------------------------------------------------------
# Telegram Notification Helper (optional)
# ----------------------------------------------------------------------------
def send_telegram(text: str):
    load_env()
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not tok or not chat:
        return
    try:
        url = f"https://api.telegram.org/bot{tok}/sendMessage"
        payload = json.dumps({"chat_id": chat, "text": text}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8, context=SSL_CTX):
            pass
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Authentication Helper (DASHBOARD_PASSWORD)
# ----------------------------------------------------------------------------
def get_dashboard_password() -> str:
    load_env()
    return os.environ.get("DASHBOARD_PASSWORD", "").strip()


def expected_session_token(pw: str) -> str:
    return hmac.new(pw.encode("utf-8"), b"renko-atr-pro-session-v2", hashlib.sha256).hexdigest()


def is_request_authenticated(handler: BaseHTTPRequestHandler) -> bool:
    pw = get_dashboard_password()
    if not pw:
        return True  # No password configured -> open (e.g. local dev)
    exp = expected_session_token(pw)

    # 1. Check Authorization: Bearer <token> or X-Dashboard-Token header
    auth_hdr = handler.headers.get("Authorization", "")
    if auth_hdr.startswith("Bearer "):
        tok = auth_hdr[7:].strip()
        if hmac.compare_digest(tok, exp) or hmac.compare_digest(tok, pw):
            return True
    x_tok = handler.headers.get("X-Dashboard-Token", "").strip()
    if x_tok and (hmac.compare_digest(x_tok, exp) or hmac.compare_digest(x_tok, pw)):
        return True

    # 2. Check Cookie: renko_session=<token>
    raw_cookie = handler.headers.get("Cookie", "")
    if raw_cookie:
        c = SimpleCookie()
        try:
            c.load(raw_cookie)
            if "renko_session" in c and hmac.compare_digest(c["renko_session"].value, exp):
                return True
        except Exception:
            pass
    return False


# ----------------------------------------------------------------------------
# 24/7 Background Watcher Worker
# ----------------------------------------------------------------------------
class BackgroundWatcher:
    def __init__(self):
        self.running = False
        self.last_check: str | None = None
        self.last_status: str = "Idle (Paused)"
        self.last_error: str | None = None
        self.checks_count: int = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def start(self):
        with self._lock:
            self.running = True
            self.last_status = "Watcher active"

    def stop(self):
        with self._lock:
            self.running = False
            self.last_status = "Paused by user"

    def run_single_step(self) -> str:
        cfg = load_config()
        creds = check_credentials_present()
        if not (creds["api_key"] and creds["identifier"] and creds["password"]):
            self.last_check = datetime.now(timezone.utc).isoformat()
            self.last_status = "Skipped: Capital.com API keys not configured yet"
            return self.last_status
        try:
            had_pending = PENDING_FILE.exists()
            cap = Capital("live" if cfg.get("live") else "demo")
            summary = step_multi_market(cap, cfg)
            self.checks_count += 1
            self.last_check = datetime.now(timezone.utc).isoformat()
            self.last_error = None
            monitored = summary.get("monitored", 16)
            open_deals = summary.get("open_deals", 0)
            self.last_status = f"OK ({cap.env.upper()}) — monitored {monitored} mkts | {open_deals} open deals | check #{self.checks_count}"
            if not had_pending and PENDING_FILE.exists():
                t = json.loads(PENDING_FILE.read_text())
                send_telegram(
                    f"🔔 RENKO ATR TICKET: {t['action']} {t['size']} {t['epic']} @ ~{t.get('ask') or t.get('bid')} "
                    f"(Stop: {t['safety_stop']}). Open your dashboard to Approve or Reject."
                )
            return self.last_status
        except Exception as e:
            self.last_check = datetime.now(timezone.utc).isoformat()
            self.last_error = str(e)
            self.last_status = f"Error: {e}"
            return self.last_status

    def _loop(self):
        while True:
            try:
                if self.running and not kill_switch_active():
                    self.run_single_step()
                    cfg = load_config()
                    res_m = RES_MINUTES.get(cfg.get("resolution", "MINUTE"), 1)
                    now_utc = datetime.now(timezone.utc)
                    sec_in_candle = (now_utc.minute % res_m) * 60 + now_utc.second + (now_utc.microsecond / 1_000_000.0)
                    total_candle_sec = res_m * 60.0
                    half_sec = total_candle_sec / 2.0
                    # Target 1: mid-candle check; Target 2: right after candle close (1.0s past :00)
                    if sec_in_candle < 1.0:
                        wait_s = 1.0 - sec_in_candle
                    elif sec_in_candle < half_sec:
                        wait_s = half_sec - sec_in_candle
                    elif sec_in_candle < (total_candle_sec + 1.0):
                        wait_s = (total_candle_sec + 1.0) - sec_in_candle
                    else:
                        wait_s = 15.0
                    wait_s = max(2.0, min(wait_s, 60.0))
                else:
                    wait_s = 3.0
            except Exception:
                wait_s = 10.0
            end_t = time.time() + float(wait_s)
            while time.time() < end_t:
                if not self.running:
                    break
                time.sleep(min(0.5, max(0.05, end_t - time.time())))

    def state(self) -> dict:
        return {
            "running": self.running,
            "last_check": self.last_check,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "checks_count": self.checks_count,
        }


WATCHER = BackgroundWatcher()


def mask_identifier(ident: str) -> str:
    if not ident:
        return ""
    if "@" in ident:
        parts = ident.split("@", 1)
        u, domain = parts[0], parts[1]
        masked_u = (u[:2] + "***" + u[-1:]) if len(u) > 3 else (u[:1] + "***")
        return f"{masked_u}@{domain}"
    if len(ident) > 6:
        return ident[:2] + "***" + ident[-2:]
    return ident[:1] + "***"


def mask_key(k: str) -> str:
    if not k:
        return ""
    return k[:4] + "••••••••" + k[-4:] if len(k) >= 10 else "••••••••"


def update_dotenv(updates: dict[str, str | None]):
    """Update or remove variables in local .env file and in os.environ."""
    dotenv_path = ROOT / ".env"
    lines = []
    seen = set()
    if dotenv_path.exists():
        for line in dotenv_path.read_text().splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, _ = s.split("=", 1)
                k = k.strip()
                seen.add(k)
                if k in updates:
                    val = updates[k]
                    if val is not None:
                        lines.append(f'{k}="{val}"')
                else:
                    lines.append(line)
            else:
                lines.append(line)
    for k, val in updates.items():
        if k not in seen and val is not None:
            lines.append(f'{k}="{val}"')
    dotenv_path.write_text("\n".join(lines).strip() + "\n")

    for k, val in updates.items():
        if val is not None:
            os.environ[k] = str(val)
        else:
            os.environ.pop(k, None)

    creds_json = ROOT / "data" / "credentials.json"
    try:
        creds_json.parent.mkdir(parents=True, exist_ok=True)
        current_creds = {}
        if creds_json.exists():
            try:
                current_creds = json.loads(creds_json.read_text())
            except Exception:
                current_creds = {}
        for k, val in updates.items():
            if val is not None:
                current_creds[k] = str(val)
            else:
                current_creds.pop(k, None)
        if current_creds:
            creds_json.write_text(json.dumps(current_creds, indent=2) + "\n")
        elif creds_json.exists():
            creds_json.unlink()
    except Exception:
        pass


def check_credentials_present() -> dict:
    load_env()
    key = os.environ.get("CAPITAL_API_KEY", "").strip()
    ident = os.environ.get("CAPITAL_IDENTIFIER", "").strip()
    pw = os.environ.get("CAPITAL_PASSWORD", "").strip()
    env = os.environ.get("CAPITAL_ENV", "demo").lower()
    has_creds = bool(key and ident and pw)
    return {
        "connected": has_creds,
        "api_key": bool(key),
        "identifier": bool(ident),
        "password": bool(pw),
        "masked_identifier": mask_identifier(ident) if ident else None,
        "masked_key": mask_key(key) if key else None,
        "env": env,
        "telegram": bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")),
        "password_protected": bool(get_dashboard_password()),
    }


_MARKET_SNAP_CACHE: dict[str, tuple[float, dict, str]] = {}
_MARKET_SNAP_LOCK = threading.Lock()
MARKET_SNAP_TTL = 10.0


def get_live_market_snapshot(cap: Capital, epic: str) -> tuple[dict, str]:
    """Query live market snapshot from Capital.com, checking aliases with in-memory caching."""
    now = time.time()
    with _MARKET_SNAP_LOCK:
        if epic in _MARKET_SNAP_CACHE:
            ts, snap, cand = _MARKET_SNAP_CACHE[epic]
            if now - ts < MARKET_SNAP_TTL:
                return snap, cand

    candidates = resolve_epic_alias(epic)
    for cand in candidates:
        try:
            res = cap.market(cand)
            if res and res.get("snapshot"):
                snap = res.get("snapshot", {})
                with _MARKET_SNAP_LOCK:
                    _MARKET_SNAP_CACHE[epic] = (now, snap, cand)
                return snap, cand
        except Exception:
            continue
    return {}, epic


_BACKTEST_CACHE: dict[str, tuple[float, dict]] = {}
_BACKTEST_CACHE_LOCK = threading.Lock()
BACKTEST_CACHE_TTL = 60.0  # seconds


def _cfg_cache_key(cfg: dict) -> str:
    epic = str(cfg.get("epic", "GOLD")).upper()
    strat = cfg.get("strategy", {})
    risk = cfg.get("risk", {})
    strat_str = json.dumps(strat, sort_keys=True)
    risk_str = json.dumps(risk, sort_keys=True)
    return f"{epic}_{cfg.get('resolution','MINUTE')}_{strat_str}_{risk_str}"


def clear_backtest_cache():
    with _BACKTEST_CACHE_LOCK:
        _BACKTEST_CACHE.clear()


def serialize_backtest(cfg: dict, use_cache: bool = True, skip_network: bool = False) -> dict:
    cache_key = _cfg_cache_key(cfg)
    now = time.time()
    if use_cache:
        with _BACKTEST_CACHE_LOCK:
            if cache_key in _BACKTEST_CACHE:
                cached_time, cached_res = _BACKTEST_CACHE[cache_key]
                if now - cached_time < BACKTEST_CACHE_TTL:
                    return cached_res

    bars, days = ensure_data(cfg)
    use_stop = bool(cfg.get("risk", {}).get("safety_stop_in_backtest", True))

    trades, eng, signals, filtered = simulate(bars, days, cfg, use_stop=use_stop)
    write_trades(trades, JOURNAL / "backtest_trades.csv")

    raw_cfg = json.loads(json.dumps(cfg))
    raw_cfg["strategy"]["one_trade_per_run"] = False
    raw_cfg["strategy"]["max_bricks_on_entry_bar"] = 0
    raw_cfg["strategy"]["max_overshoot_bricks"] = 0.0
    raw_cfg["strategy"]["grid_anchor"] = False
    raw_cfg["strategy"]["confirm_minutes"] = 0
    raw_cfg["strategy"]["dual_order_mode"] = False
    raw_cfg["risk"]["safety_stop_in_backtest"] = False
    raw_cfg["risk"]["breakeven_after_bricks"] = 0.0
    raw_cfg["risk"]["max_spread_frac_of_brick"] = 0.0
    raw_cfg["risk"]["rollover_blackout"] = False
    raw_cfg["risk"]["sessions"] = {"asian": True, "london": True, "new_york": True}
    raw_trades, _, _, _ = simulate(bars, days, raw_cfg, use_stop=False)

    tail_bricks = eng.bricks[-250:]
    spec = INSTRUMENT_SPECS.get(cfg.get("epic", "GOLD").upper(), INSTRUMENT_SPECS.get("GOLD", {}))
    digits = spec.get("digits", 2)
    bricks_json = [{
        "index": b.index,
        "open": round(b.open, digits),
        "close": round(b.close, digits),
        "direction": b.direction,
        "time": b.time.isoformat() if hasattr(b.time, "isoformat") else str(b.time),
        "st_value": round(b.st_value, digits) if b.st_value is not None else None,
        "st_dir": b.st_dir,
        "brick_size": round(b.brick_size, max(digits, 2)),
    } for b in tail_bricks]

    trades_json = []
    eq_gross = eq_net = 0.0
    equity_curve = [{"i": 0, "t": bars[0]["t"].isoformat() if bars else "", "gross": 0.0, "net": 0.0}]
    for idx, t in enumerate(trades, 1):
        eq_gross = round(eq_gross + t["gross"], 2)
        eq_net = round(eq_net + t["net"], 2)
        et = t["entry_time"].isoformat() if hasattr(t["entry_time"], "isoformat") else str(t["entry_time"])
        xt = t["exit_time"].isoformat() if hasattr(t["exit_time"], "isoformat") else str(t["exit_time"])
        equity_curve.append({"i": idx, "t": xt, "gross": eq_gross, "net": eq_net})
        trades_json.append({
            "num": idx,
            "leg": t.get("leg", "SINGLE"),
            "why_in": t.get("why_in", ""),
            "side": t["side"],
            "size": t["size"],
            "session": t["session"],
            "entry_time": et,
            "entry_brick_idx": t.get("entry_brick_idx"),
            "trigger_px": t["trigger_px"],
            "entry": t["entry"],
            "entry_exec": t["entry_exec"],
            "overshoot": t["overshoot"],
            "brick_size": t["brick_size"],
            "exit_time": xt,
            "exit_brick_idx": t.get("exit_brick_idx"),
            "exit": t["exit"],
            "exit_exec": t["exit_exec"],
            "why_out": t["why_out"],
            "mfe_bricks": t["mfe_bricks"],
            "mae_bricks": t["mae_bricks"],
            "gross": t["gross"],
            "net": t["net"],
            "cum_net": eq_net,
        })

    raw_eq = 0.0
    raw_curve = [0.0]
    for t in raw_trades:
        raw_eq = round(raw_eq + t["net"], 2)
        raw_curve.append(raw_eq)

    mc_net = run_montecarlo([t["net"] for t in trades], runs=500, seed=19)
    last_b = eng.bricks[-1] if eng.bricks else None
    last_price = round(bars[-1]["c"], digits) if bars else None

    # Sync with live broker mid price if Capital.com connected
    if not skip_network:
        creds = check_credentials_present()
        if creds.get("connected"):
            try:
                cap_live = Capital("live" if cfg.get("live") else "demo")
                snap_quick, matched_epic = get_live_market_snapshot(cap_live, cfg.get("epic", "GOLD"))
                b_q, o_q = snap_quick.get("bid"), snap_quick.get("offer")
                if b_q is not None and o_q is not None:
                    last_price = round((b_q + o_q) / 2, digits)
                elif b_q is not None or o_q is not None:
                    last_price = round(b_q if b_q is not None else o_q, digits)
            except Exception:
                pass

    res = {
        "data_summary": {
            "epic": cfg["epic"],
            "resolution": cfg["resolution"],
            "bars_count": len(bars),
            "days_count": len(days),
            "digits": digits,
            "currency": spec.get("currency", "$"),
            "pip_factor": spec.get("pip_factor", 1.0),
            "name": spec.get("name", cfg["epic"]),
            "category": spec.get("category", "General"),
            "tick": cfg.get("tick", spec.get("tick", 0.01)),
            "start": bars[0]["t"].isoformat() if bars else None,
            "end": bars[-1]["t"].isoformat() if bars else None,
            "last_price": last_price,
            "total_bricks": eng.brick_cnt,
            "last_brick_size": round(last_b.brick_size, max(digits, 2)) if last_b else cfg["strategy"]["fallback_size"],
            "last_brick_close": round(eng.last_close, digits) if eng.last_close else None,
            "green_run": eng.green_run,
            "red_run": eng.red_run,
            "st_dir": eng.st_dir,
            "st_now": round(eng.st_now, digits) if eng.st_now else None,
            "filtered_count": len(filtered),
        },
        "stats_gross": stats(trades, "gross"),
        "stats_net": stats(trades, "net"),
        "raw_pine_stats_net": stats(raw_trades, "net"),
        "raw_pine_stats_gross": stats(raw_trades, "gross"),
        "sessions_net": session_breakdown(trades, "net"),
        "bricks": bricks_json,
        "signals": signals[-200:],
        "filtered_events": filtered[-100:],
        "trades": trades_json,
        "equity_curve": equity_curve,
        "raw_curve": raw_curve,
        "montecarlo": mc_net,
    }
    with _BACKTEST_CACHE_LOCK:
        _BACKTEST_CACHE[cache_key] = (now, res)
    return res


def get_bot_status(cfg: dict, skip_network: bool = False) -> dict:
    creds = check_credentials_present()
    cnt, pnl = today_stats()
    pending = None
    if PENDING_FILE.exists():
        try:
            pending = json.loads(PENDING_FILE.read_text())
        except Exception:
            pending = None

    live_log = []
    if LIVE_TRADES.exists():
        with LIVE_TRADES.open() as f:
            live_log = list(csv.DictReader(f))[-40:]

    broker_positions = []
    broker_market = {}
    if not skip_network and creds.get("connected"):
        try:
            cap = Capital("live" if cfg.get("live") else "demo")
            pos_raw = cap.positions()
            for p in pos_raw:
                pos_info = p.get("position", {})
                mkt_info = p.get("market", {})
                broker_positions.append({
                    "dealId": str(pos_info.get("dealId", "")),
                    "epic": mkt_info.get("epic", cfg["epic"]),
                    "direction": str(pos_info.get("direction", "BUY")),
                    "size": float(pos_info.get("size", 0.0)),
                    "level": float(pos_info.get("level", 0.0)),
                    "stopLevel": float(pos_info["stopLevel"]) if pos_info.get("stopLevel") is not None else None,
                    "profitLevel": float(pos_info["profitLevel"]) if pos_info.get("profitLevel") is not None else None,
                    "upl": float(pos_info.get("upl", 0.0)),
                    "createdDate": str(pos_info.get("createdDate", "")),
                })
            # Query live broker market snapshot (auto-resolves GOLD vs XAUUSD)
            snap, matched_epic = get_live_market_snapshot(cap, cfg.get("epic", "GOLD"))
            if matched_epic != cfg.get("epic"):
                cfg["epic"] = matched_epic
                save_config(cfg)

            spec = INSTRUMENT_SPECS.get(cfg.get("epic", "GOLD").upper(), INSTRUMENT_SPECS.get("GOLD", {}))
            digits = spec.get("digits", 2)
            if snap:
                bid = snap.get("bid")
                offer = snap.get("offer")
                mid = round((bid + offer) / 2, digits) if (bid is not None and offer is not None) else (bid or offer)
                spread = round(offer - bid, digits) if (bid is not None and offer is not None) else None
                broker_market = {
                    "epic": matched_epic,
                    "bid": bid,
                    "offer": offer,
                    "mid": mid,
                    "spread": spread,
                    "high": snap.get("high"),
                    "low": snap.get("low"),
                    "marketStatus": snap.get("marketStatus", "TRADEABLE"),
                    "netChange": snap.get("netChange"),
                    "percentageChange": snap.get("percentageChange"),
                }
        except Exception:
            pass

    return {
        "credentials": creds,
        "watcher": WATCHER.state(),
        "kill_switch": kill_switch_active(),
        "mode": cfg.get("mode", "semi-auto"),
        "live": bool(cfg.get("live", False)),
        "today_trades": cnt,
        "today_pnl": pnl,
        "pending_ticket": pending,
        "live_positions": broker_positions,
        "market": broker_market,
        "live_log": list(reversed(live_log)),
    }


def fetch_candles_with_fallback(cap: Capital, epic: str, resolution: str, start: datetime, end: datetime):
    candidates = resolve_epic_alias(epic)
    last_err = None
    for cand in candidates:
        try:
            bars = cap.prices(cand, resolution, start, end)
            if bars:
                return cand, bars
        except Exception as e:
            last_err = e
            continue
    if last_err:
        raise last_err
    return epic, []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj, code=200, extra_headers: list[tuple[str, str]] | None = None):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        if extra_headers:
            for hk, hv in extra_headers:
                self.send_header(hk, hv)
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length", "0"))
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_HEAD(self):
        u = urlparse(self.path)
        if u.path in ("/healthz", "/", "/index.html", "/studio", "/studio.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8" if not u.path.startswith("/healthz") else "application/json")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)

        # Public health check for Railway / Render / Fly.io load balancers
        if u.path == "/healthz":
            self._send_json({
                "status": "ok",
                "service": "renko-atr-pro-cockpit",
                "watcher_running": WATCHER.running,
                "kill_switch": kill_switch_active(),
            })
            return

        if u.path in ("/", "/index.html"):
            p = STATIC / "index.html"
            raw = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(raw)
            return

        if u.path in ("/studio", "/studio.html"):
            p = ROOT / "studio.html"
            raw = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if u.path == "/api/auth-status":
            self._send_json({
                "password_required": bool(get_dashboard_password()),
                "authenticated": is_request_authenticated(self),
            })
            return

        if not is_request_authenticated(self):
            self._send_json({"error": "Authentication required", "auth_required": True}, 401)
            return

        if u.path == "/api/state":
            cfg = load_config()
            self._send_json({
                "config": cfg,
                "bot": get_bot_status(cfg),
                "backtest": serialize_backtest(cfg),
                "instrument_specs": INSTRUMENT_SPECS,
            })
            return

        if u.path == "/api/scanner":
            from scanner import get_cached_or_scan_all
            cfg = load_config()
            creds = check_credentials_present()
            cap = None
            if creds.get("connected"):
                try:
                    cap = Capital("live" if cfg.get("live") else "demo")
                except Exception:
                    pass
            res = get_cached_or_scan_all(cfg, cap=cap)
            self._send_json(res)
            return

        self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            body = self._read_body()

            # Login endpoint (public)
            if u.path == "/api/login":
                pw = get_dashboard_password()
                given = str(body.get("password", "")).strip()
                if not pw:
                    self._send_json({"ok": True, "authenticated": True})
                    return
                if hmac.compare_digest(given, pw):
                    tok = expected_session_token(pw)
                    cookie = f"renko_session={tok}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"
                    self._send_json({"ok": True, "authenticated": True, "token": tok},
                                    extra_headers=[("Set-Cookie", cookie)])
                else:
                    self._send_json({"error": "Invalid dashboard password"}, 403)
                return

            if u.path == "/api/logout":
                cookie = "renko_session=; Path=/; HttpOnly; Max-Age=0"
                self._send_json({"ok": True}, extra_headers=[("Set-Cookie", cookie)])
                return

            # All other POST endpoints require authentication
            if not is_request_authenticated(self):
                self._send_json({"error": "Authentication required", "auth_required": True}, 401)
                return

            cfg = load_config()

            if u.path == "/api/save-broker-keys":
                api_key = str(body.get("api_key", "")).strip()
                ident = str(body.get("identifier", "")).strip()
                pw = str(body.get("password", "")).strip()
                env = str(body.get("env", "demo")).strip().lower()
                test_conn = bool(body.get("test_connection", True))

                if env not in ("demo", "live"):
                    self._send_json({"error": "Broker environment must be 'demo' or 'live'"}, 400)
                    return
                if not (api_key and ident and pw):
                    self._send_json({"error": "All fields are required: API Key, Login Email, and API Password."}, 400)
                    return

                # Test credentials against Capital.com before saving
                if test_conn:
                    try:
                        cap_test = Capital(env=env, api_key=api_key, identifier=ident, password=pw)
                        cap_test.login()
                    except Exception as e:
                        self._send_json({
                            "error": f"Capital.com authentication failed ({env.upper()}): {e}. Please verify your API Key and API Password in Capital.com Settings."
                        }, 400)
                        return

                # Save to runtime environment and .env file
                update_dotenv({
                    "CAPITAL_API_KEY": api_key,
                    "CAPITAL_IDENTIFIER": ident,
                    "CAPITAL_PASSWORD": pw,
                    "CAPITAL_ENV": env,
                })
                cfg["live"] = (env == "live")
                save_config(cfg)

                # Auto-sync recent market candles from broker
                try:
                    cap_sync = Capital(env=env, api_key=api_key, identifier=ident, password=pw)
                    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
                    start = end - timedelta(days=5)
                    matched_epic, bars = fetch_candles_with_fallback(cap_sync, cfg["epic"], cfg["resolution"], start, end)
                    if matched_epic != cfg["epic"]:
                        cfg["epic"] = matched_epic
                        save_config(cfg)
                    _, day_bars = fetch_candles_with_fallback(cap_sync, cfg["epic"], "DAY", start - timedelta(days=35), end)
                    if bars:
                        save_bars(data_path(cfg["epic"], cfg["resolution"]), bars)
                    if day_bars:
                        save_bars(data_path(cfg["epic"], "DAY"), day_bars)
                    clear_backtest_cache()
                except Exception:
                    pass

                WATCHER.start()

                self._send_json({
                    "ok": True,
                    "message": f"Successfully connected to Capital.com ({env.upper()})! 24/7 Watcher is now active and monitoring market.",
                    "bot": get_bot_status(cfg),
                    "backtest": serialize_backtest(cfg),
                })
                return

            if u.path == "/api/test-broker-connection":
                creds = check_credentials_present()
                if not creds["connected"]:
                    self._send_json({"error": "No broker credentials configured yet."}, 400)
                    return
                try:
                    cap = Capital()
                    cap.login()
                    self._send_json({
                        "ok": True,
                        "message": f"Connection active! Session verified with Capital.com ({cap.env.upper()}).",
                        "bot": get_bot_status(cfg),
                    })
                except Exception as e:
                    self._send_json({"error": f"Capital.com check failed: {e}"}, 400)
                return

            if u.path == "/api/disconnect-broker":
                update_dotenv({
                    "CAPITAL_API_KEY": None,
                    "CAPITAL_IDENTIFIER": None,
                    "CAPITAL_PASSWORD": None,
                })
                self._send_json({
                    "ok": True,
                    "message": "Broker disconnected. Keys cleared from memory.",
                    "bot": get_bot_status(cfg),
                })
                return

            if u.path == "/api/close-position":
                deal_id = str(body.get("dealId", "")).strip()
                if not deal_id:
                    self._send_json({"error": "Missing dealId"}, 400)
                    return
                try:
                    cap = Capital("live" if cfg.get("live") else "demo")
                    conf = cap.close_position(deal_id)
                    from bot import log_event
                    log_event("MANUAL_CLOSE", "CLOSE", 0, conf.get("level"), None, deal_id,
                              realized_pnl=conf.get("profit", 0.0), note=conf.get("dealStatus", "CLOSED"))
                    self._send_json({
                        "ok": True,
                        "message": f"Closed Capital.com deal {deal_id}",
                        "bot": get_bot_status(cfg)
                    })
                except Exception as e:
                    self._send_json({"error": f"Failed to close deal {deal_id}: {e}"}, 400)
                return

            if u.path == "/api/watcher":
                act = body.get("action")
                if act == "start":
                    WATCHER.start()
                    msg = "24/7 Cloud Watcher started."
                elif act == "stop":
                    WATCHER.stop()
                    msg = "24/7 Cloud Watcher paused."
                elif act == "step":
                    msg = WATCHER.run_single_step()
                else:
                    msg = "Unknown watcher action"
                self._send_json({"ok": True, "message": msg, "bot": get_bot_status(cfg)})
                return

            if u.path == "/api/scanner":
                from scanner import get_cached_or_scan_all
                force = bool(body.get("force", False))
                creds = check_credentials_present()
                cap = None
                if creds.get("connected"):
                    try:
                        cap = Capital("live" if cfg.get("live") else "demo")
                    except Exception:
                        pass
                res = get_cached_or_scan_all(cfg, cap=cap, force=force)
                self._send_json(res)
                return

            if u.path == "/api/switch-instrument":
                target_epic = str(body.get("epic", "GOLD")).upper()
                if target_epic == "XAUUSD":
                    target_epic = "GOLD"
                cfg["epic"] = target_epic
                if target_epic in INSTRUMENT_SPECS:
                    spec = INSTRUMENT_SPECS[target_epic]
                    cfg["tick"] = spec["tick"]
                    cfg["strategy"]["fallback_size"] = spec["fallback_size"]
                save_config(cfg)

                # Decouple broker candle synchronization into a background worker so chart responds in <15ms
                creds = check_credentials_present()
                if creds.get("connected"):
                    def _bg_sync(epic_to_sync, resolution, is_live):
                        try:
                            cap = Capital("live" if is_live else "demo")
                            end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
                            start = end - timedelta(days=15)
                            matched, bars = fetch_candles_with_fallback(cap, epic_to_sync, resolution, start, end)
                            _, day_bars = fetch_candles_with_fallback(cap, matched, "DAY", start - timedelta(days=35), end)
                            if bars:
                                save_bars(data_path(matched, resolution), bars)
                            if day_bars:
                                save_bars(data_path(matched, "DAY"), day_bars)
                            clear_backtest_cache()
                        except Exception:
                            pass
                    threading.Thread(target=_bg_sync, args=(target_epic, cfg.get("resolution", "MINUTE"), bool(cfg.get("live"))), daemon=True).start()

                self._send_json({
                    "ok": True,
                    "config": cfg,
                    "bot": get_bot_status(cfg, skip_network=True),
                    "backtest": serialize_backtest(cfg, use_cache=True, skip_network=True),
                    "instrument_specs": INSTRUMENT_SPECS,
                })
                return

            if u.path == "/api/config":
                old_epic = str(cfg.get("epic", "GOLD")).upper()
                for k in ("epic", "resolution", "price", "tick", "size", "sizing_mode", "risk_per_trade_usd", "mode", "live"):
                    if k in body:
                        cfg[k] = body[k]
                if cfg.get("epic", "").upper() == "XAUUSD":
                    cfg["epic"] = "GOLD"
                new_epic = str(cfg.get("epic", "GOLD")).upper()
                if new_epic != old_epic and new_epic in INSTRUMENT_SPECS:
                    spec = INSTRUMENT_SPECS[new_epic]
                    if "tick" not in body:
                        cfg["tick"] = spec["tick"]
                    if "strategy" not in body or "fallback_size" not in body.get("strategy", {}):
                        cfg["strategy"]["fallback_size"] = spec["fallback_size"]
                if "strategy" in body and isinstance(body["strategy"], dict):
                    cfg["strategy"].update(body["strategy"])
                if "risk" in body and isinstance(body["risk"], dict):
                    for rk, rv in body["risk"].items():
                        if rk == "sessions" and isinstance(rv, dict):
                            cfg["risk"].setdefault("sessions", {}).update(rv)
                        else:
                            cfg["risk"][rk] = rv
                save_config(cfg)
                clear_backtest_cache()
                self._send_json({
                    "config": cfg,
                    "bot": get_bot_status(cfg),
                    "backtest": serialize_backtest(cfg, use_cache=False),
                    "instrument_specs": INSTRUMENT_SPECS,
                })
                return

            if u.path == "/api/sample-data":
                days = int(body.get("days", 12))
                seed = int(body.get("seed", 42))
                generate_sample_gold_data(cfg["epic"], cfg["resolution"], days_intraday=days, seed=seed)
                clear_backtest_cache()
                self._send_json({
                    "config": cfg,
                    "bot": get_bot_status(cfg),
                    "backtest": serialize_backtest(cfg, use_cache=False),
                })
                return

            if u.path in ("/api/fetch-capital", "/api/sync-market"):
                creds = check_credentials_present()
                if not (creds.get("connected") or (creds.get("api_key") and creds.get("identifier"))):
                    self._send_json({
                        "ok": False,
                        "error": "Capital.com API is not connected yet. Click 'Connect Broker' to enter your API credentials (Demo or Live)."
                    }, 400)
                    return
                try:
                    days = int(body.get("days", 15))
                    cap = Capital("live" if cfg.get("live") else "demo")
                    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
                    start = end - timedelta(days=days)
                    matched_epic, bars = fetch_candles_with_fallback(cap, cfg["epic"], cfg["resolution"], start, end)
                    if matched_epic != cfg["epic"]:
                        cfg["epic"] = matched_epic
                        save_config(cfg)
                    _, day_bars = fetch_candles_with_fallback(cap, cfg["epic"], "DAY", start - timedelta(days=35), end)
                    if bars:
                        save_bars(data_path(cfg["epic"], cfg["resolution"]), bars)
                    if day_bars:
                        save_bars(data_path(cfg["epic"], "DAY"), day_bars)
                    clear_backtest_cache()

                    bot_st = get_bot_status(cfg)
                    bt_st = serialize_backtest(cfg)
                    mkt = bot_st.get("market", {})
                    mid_px = mkt.get("mid") or (bars[-1]["c"] if bars else None)
                    spec = INSTRUMENT_SPECS.get(cfg.get("epic", "GOLD").upper(), INSTRUMENT_SPECS.get("GOLD", {}))
                    digits = spec.get("digits", 2)

                    self._send_json({
                        "ok": True,
                        "fetched_bars": len(bars),
                        "live_price": mid_px,
                        "bid": mkt.get("bid"),
                        "offer": mkt.get("offer"),
                        "spread": mkt.get("spread"),
                        "message": f"Synced {len(bars)} {cfg['epic']} candles from Capital.com ({cap.env.upper()})! Live Price: {mid_px:.{digits}f}" if mid_px else "Market synced.",
                        "config": cfg,
                        "bot": bot_st,
                        "backtest": bt_st,
                    })
                except Exception as e:
                    self._send_json({"ok": False, "error": f"Capital.com sync failed: {e}"}, 400)
                return

            if u.path == "/api/upload-csv":
                csv_text = body.get("csv", "")
                rows = []
                for r in csv.DictReader(io.StringIO(csv_text)):
                    t_raw = r.get("time") or r.get("timestamp") or r.get("date") or r.get("Date")
                    if not t_raw:
                        continue
                    try:
                        if t_raw.isdigit():
                            t = datetime.fromtimestamp(int(t_raw), tz=timezone.utc)
                        else:
                            t = datetime.fromisoformat(t_raw.replace("Z", "+00:00"))
                            if t.tzinfo is None:
                                t = t.replace(tzinfo=timezone.utc)
                        o = float(r.get("open") or r.get("Open") or r.get("o"))
                        h = float(r.get("high") or r.get("High") or r.get("h"))
                        l = float(r.get("low") or r.get("Low") or r.get("l"))
                        c = float(r.get("close") or r.get("Close") or r.get("c"))
                        rows.append({
                            "t": t, "o": o, "h": h, "l": l, "c": c,
                            "o_bid": o - 0.14, "o_ask": o + 0.14,
                            "h_bid": h - 0.14, "h_ask": h + 0.14,
                            "l_bid": l - 0.14, "l_ask": l + 0.14,
                            "c_bid": c - 0.14, "c_ask": c + 0.14,
                        })
                    except Exception:
                        continue
                if not rows:
                    self._send_json({"error": "No valid OHLC rows found in CSV"}, 400)
                    return
                rows.sort(key=lambda x: x["t"])
                save_bars(data_path(cfg["epic"], cfg["resolution"]), rows)
                self._send_json({
                    "ok": True,
                    "imported_bars": len(rows),
                    "config": cfg,
                    "bot": get_bot_status(cfg),
                    "backtest": serialize_backtest(cfg),
                })
                return

            if u.path == "/api/simulate-ticket":
                bt = serialize_backtest(cfg)
                ds = bt["data_summary"]
                spec = INSTRUMENT_SPECS.get(cfg.get("epic", "GOLD").upper(), INSTRUMENT_SPECS.get("GOLD", {}))
                digits = spec.get("digits", 2)
                half_sp = spec.get("half_sp", 0.14)
                bs = ds["last_brick_size"] or spec.get("fallback_size", 3.50)
                lc = ds["last_brick_close"] or spec.get("base_px", 2680.0)
                action = body.get("action", "BUY")
                side = 1 if action == "BUY" else -1
                k_stop = float(cfg["risk"].get("safety_stop_bricks", 3.0))
                stop = round(lc - side * k_stop * bs, digits)
                now_utc = datetime.now(timezone.utc)
                ttl = int(cfg["risk"].get("ticket_ttl_seconds", 180))
                ticket = {
                    "created_at": now_utc.isoformat(),
                    "expires_at": (now_utc + timedelta(seconds=ttl)).isoformat(),
                    "bar_time": ds["end"] or now_utc.isoformat(),
                    "epic": cfg["epic"],
                    "action": action,
                    "reverse": False,
                    "size": cfg["size"],
                    "trigger_brick_px": lc,
                    "last_brick_close": lc,
                    "brick_size": bs,
                    "safety_stop": stop,
                    "bid": round(lc - half_sp, digits),
                    "ask": round(lc + half_sp, digits),
                    "deal_ids": [],
                    "simulated": True,
                }
                JOURNAL.mkdir(parents=True, exist_ok=True)
                PENDING_FILE.write_text(json.dumps(ticket, indent=2))
                self._send_json({"ok": True, "bot": get_bot_status(cfg)})
                return

            if u.path == "/api/ticket-action":
                act = body.get("decision")
                if act == "reject":
                    if PENDING_FILE.exists():
                        PENDING_FILE.unlink()
                    self._send_json({"ok": True, "message": "Ticket rejected and discarded.", "bot": get_bot_status(cfg)})
                    return
                if act == "approve":
                    if not PENDING_FILE.exists():
                        self._send_json({"error": "No pending ticket to approve."}, 400)
                        return
                    t = json.loads(PENDING_FILE.read_text())
                    if "expires_at" in t:
                        if datetime.now(timezone.utc) > datetime.fromisoformat(t["expires_at"]):
                            PENDING_FILE.unlink()
                            self._send_json({"error": "Ticket expired (TTL guard). Discarded for safety."}, 400)
                            return
                    creds = check_credentials_present()
                    if t.get("simulated") and not (creds["api_key"] and creds["identifier"] and creds["password"]):
                        from bot import log_event
                        px = t["ask"] if t["action"] == "BUY" else t["bid"]
                        log_event("SIM_OPEN", t["action"], t["size"], px, t["safety_stop"], "SIM-DEAL-001", note="PAPER_SIM")
                        PENDING_FILE.unlink()
                        self._send_json({
                            "ok": True,
                            "message": f"Paper-approved {t['action']} {t['size']} {t['epic']} @ {px} (stop {t['safety_stop']})",
                            "bot": get_bot_status(cfg),
                        })
                        return
                    from bot import execute_decision
                    cap = Capital("live" if cfg.get("live") else "demo")
                    execute_decision(cap, cfg, t["action"], t["reverse"], t["last_brick_close"],
                                     t["brick_size"], t["deal_ids"], size=t.get("size"))
                    PENDING_FILE.unlink()
                    self._send_json({"ok": True, "message": "Order sent to Capital.com!", "bot": get_bot_status(cfg)})
                    return

            if u.path == "/api/kill-switch":
                active = bool(body.get("active", True))
                set_kill_switch(active)
                if active:
                    WATCHER.stop()
                msg = "EMERGENCY STOP engaged." if active else "Kill switch cleared. Bot resumed."
                if active:
                    creds = check_credentials_present()
                    if creds["api_key"] and creds["identifier"] and creds["password"]:
                        try:
                            from bot import log_event
                            cap = Capital("live" if cfg.get("live") else "demo")
                            raw_pos = cap.positions()
                            closed_count = 0
                            for p in raw_pos:
                                did = p.get("position", {}).get("dealId")
                                sz = float(p.get("position", {}).get("size", 0.0))
                                ep = p.get("market", {}).get("epic", "")
                                if did:
                                    try:
                                        conf = cap.close_position(did)
                                        log_event("FLATTEN", "CLOSE", sz, conf.get("level"), None, did, note=f"[{ep}] KILL_SWITCH")
                                        closed_count += 1
                                    except Exception as ce:
                                        print(f"Error closing {did}: {ce}")
                            msg += f" (Closed {closed_count} open deals across account)"
                        except Exception as e:
                            msg += f" (Broker flatten note: {e})"
                self._send_json({"ok": True, "message": msg, "bot": get_bot_status(cfg)})
                return

            self._send_json({"error": "Unknown endpoint"}, 404)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)


def main():
    load_env()
    default_host = os.environ.get("HOST", "0.0.0.0")
    default_port = int(os.environ.get("PORT", "8765"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=default_host)
    ap.add_argument("--port", type=int, default=default_port)
    a = ap.parse_args()

    cfg = load_config()
    ensure_data(cfg)

    if os.environ.get("AUTO_START_WATCHER", "").lower() in ("1", "true", "yes") or check_credentials_present().get("connected"):
        WATCHER.start()
        print("24/7 Watcher auto-started (Broker credentials active).")

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"Renko ATR Pro Cloud Dashboard running at http://{a.host}:{a.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
