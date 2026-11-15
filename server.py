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
import html
import io
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from backtest import (DATA, JOURNAL, INSTRUMENT_SPECS, data_path, ensure_data, generate_sample_gold_data,
                      load_config, save_bars, save_config, session_breakdown,
                      simulate, stats, write_trades)
from bot import (LIVE_TRADES, PENDING_FILE, _PENDING_CONFIRM, _PENDING_RUNNERS, _HANDLED_RUNS,
                 _oms_state_path, resolve_epic_alias, step_once, step_multi_market)
from capital_api import (WS_URL, Capital, CapitalError, CapitalTickStreamer, RES_MINUTES, SSL_CTX,
                         get_live_tick, load_env, update_live_tick)
from montecarlo import run_montecarlo
from risk import kill_switch_active, set_kill_switch, today_stats

ROOT = Path(__file__).parent
STATIC = ROOT / "static"
GST_TZ = timezone(timedelta(hours=4), name="GST")


def now_gst() -> datetime:
    """Return current time in UAE Gulf Standard Time (UTC+4)."""
    return datetime.now(GST_TZ)


def fmt_gst(dt_or_iso=None, fmt: str = "%Y-%m-%d %H:%M:%S GST") -> str:
    """Convert any UTC datetime or ISO-8601 string into UAE Gulf Standard Time (UTC+4)."""
    if dt_or_iso is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(dt_or_iso, datetime):
        dt = dt_or_iso if dt_or_iso.tzinfo else dt_or_iso.replace(tzinfo=timezone.utc)
    else:
        raw = str(dt_or_iso).strip()
        if not raw:
            return "--"
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            return raw
    return dt.astimezone(GST_TZ).strftime(fmt)


# ----------------------------------------------------------------------------
# Telegram Notification Helper (optional)
# ----------------------------------------------------------------------------
def send_telegram_with_status(text: str) -> dict:
    """Send a Telegram Bot API message and return delivery diagnostics."""
    load_env()
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not tok or not chat:
        return {"ok": False, "detail": "Telegram Bot Token or Chat ID is not configured yet."}
    url = f"https://api.telegram.org/bot{tok}/sendMessage"
    payload = json.dumps({"chat_id": chat, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8, context=SSL_CTX) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(raw) if raw else {}
            if data.get("ok"):
                return {"ok": True, "detail": "Telegram notification sent."}
            desc = str(data.get("description", "Unknown Telegram error"))
            return {"ok": False, "detail": desc}
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="ignore")
            err_json = json.loads(raw) if raw else {}
            desc = str(err_json.get("description", raw or str(e)))
            code = err_json.get("error_code", e.code)
        except Exception:
            desc = raw or str(e)
            code = getattr(e, "code", 400)
        if code == 400 and "chat not found" in desc.lower():
            desc = f"{desc} — open https://t.me/RENKO2026BOT in Telegram and tap Start"
        return {"ok": False, "detail": desc}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def send_telegram(text: str):
    try:
        send_telegram_with_status(text)
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
        self._last_digest_date: str | None = None
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
            monitored = summary.get("monitored", 19)
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
                    g_now = now_gst()
                    g_day = g_now.strftime("%Y-%m-%d")
                    # Daily NY-close digest at 02:00 GST (22:00 UTC)
                    if (
                        g_now.hour == 2 and g_now.minute < 5
                        and self._last_digest_date != g_day
                        and os.environ.get("TELEGRAM_BOT_TOKEN")
                        and os.environ.get("TELEGRAM_CHAT_ID")
                    ):
                        self._last_digest_date = g_day
                        try:
                            send_telegram(build_daily_telegram_digest(cfg))
                        except Exception:
                            pass
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
TICK_STREAMER = CapitalTickStreamer()


# ----------------------------------------------------------------------------
# Phase 6: Daily Telegram P&L Digest, 2-Way Bot Commands & Parameter Optimizer
# ----------------------------------------------------------------------------
def build_daily_telegram_digest(cfg: dict | None = None) -> str:
    cur_cfg = cfg or load_config()
    cnt, pnl = today_stats()
    bt = serialize_backtest(cur_cfg, use_cache=True, skip_network=True)
    sn = bt.get("stats_net", {})
    tca = bt.get("tca_attribution", {})
    usual = tca.get("usual", {})
    runner = tca.get("runner", {})
    w_state = "RUNNING" if WATCHER.running else "PAUSED"
    mode_lbl = str(cur_cfg.get("mode", "auto")).upper()
    return (
        f"📊 RENKO CLOUD DAILY DIGEST ({fmt_gst()})\n"
        f"• Market: {cur_cfg.get('epic', 'GOLD')} ({cur_cfg.get('resolution', 'MINUTE')})\n"
        f"• Bot: {w_state} | Mode: {mode_lbl} | KillSwitch: {'ON' if kill_switch_active() else 'OFF'}\n"
        f"• Today's Live Trades: {cnt} | Realized P&L: ${float(pnl):+.2f}\n"
        f"— Model E Strategy Window —\n"
        f"• Usual 3B-TP: {usual.get('trades', 0)} tr | WR {usual.get('win_rate_%', 0)}% | ${float(usual.get('net', 0)):+.2f}\n"
        f"• Runner +1B: {runner.get('trades', 0)} tr | WR {runner.get('win_rate_%', 0)}% | ${float(runner.get('net', 0)):+.2f}\n"
        f"• Combined Net: ${float(sn.get('net', 0)):+.2f} | PF {sn.get('profit_factor', 0)} | "
        f"Sharpe {sn.get('sharpe', 0)} | Calmar {sn.get('calmar', 0)} | 95% VaR -${float(sn.get('daily_var_95', 0)):.2f}"
    )


def handle_telegram_command(text: str, cfg: dict | None = None) -> str:
    """Process an authorized 2-way Telegram command from the owner's Chat ID."""
    cur_cfg = cfg or load_config()
    raw = str(text or "").strip()
    if not raw:
        return "Empty command. Send /help for commands."
    cmd = raw.split()[0].lower().split("@")[0]

    if cmd in ("/start", "/help"):
        return (
            f"🤖 RENKOBOT Remote Control ({fmt_gst()})\n"
            f"/status — Live worker, market & kill-switch state\n"
            f"/positions — Open Capital.com deals & UPL\n"
            f"/pnl or /digest — Daily P&L & Model E attribution\n"
            f"/dna [EPIC] — 15-Year Optimal DNA settings (all 19 markets)\n"
            f"/scanner — 19-market active signals summary\n"
            f"/pause — Pause 24/7 watcher\n"
            f"/resume — Clear kill switch & start watcher\n"
            f"/flatten — EMERGENCY KILL SWITCH & pause"
        )

    if cmd == "/status":
        cnt, pnl = today_stats()
        return (
            f"📡 RENKO COCKPIT STATUS ({fmt_gst()})\n"
            f"• Active Market: {cur_cfg.get('epic', 'GOLD')} ({cur_cfg.get('resolution', 'MINUTE')})\n"
            f"• 24/7 Watcher: {'RUNNING' if WATCHER.running else 'PAUSED'} ({WATCHER.last_status})\n"
            f"• Mode: {str(cur_cfg.get('mode', 'auto')).upper()} | Env: {'LIVE' if cur_cfg.get('live') else 'DEMO'}\n"
            f"• Kill Switch: {'ACTIVE' if kill_switch_active() else 'OFF'}\n"
            f"• Today Live: {cnt} trades | ${float(pnl):+.2f}"
        )

    if cmd in ("/pnl", "/digest"):
        return build_daily_telegram_digest(cur_cfg)

    if cmd in ("/dna", "/best"):
        from research_15yr_matrix import get_or_build_matrix
        mat = get_or_build_matrix()
        parts = raw.split()
        target = parts[1].upper() if len(parts) > 1 else ""
        if target == "XAUUSD":
            target = "GOLD"
        rows = mat.get("instruments", [])
        if target:
            hit = next((r for r in rows if r.get("epic") == target), None)
            if not hit:
                return f"🧬 No 15Y DNA profile found for '{target}'. Send /dna to see all {len(rows)} markets."
            op = hit.get("optimal_params", {})
            return (
                f"🧬 {hit['epic']} ({hit['name']}) — 15Y OPTIMAL DNA\n"
                f"• ATR({op.get('atr_len')}, /{op.get('atr_div')}) | ST {op.get('st_factor')} | Confirm {op.get('confirm_minutes')}m\n"
                f"• Model E: TP {op.get('tp_bricks')}B | SL {op.get('safety_stop_bricks')}B | BE {op.get('breakeven_after_bricks')}B | Sess {op.get('sessions_label')}\n"
                f"• 15Y Proj Net: ${float(hit.get('net_15y', 0)):+,.2f} | WR {hit.get('win_rate')}% | PF {hit.get('profit_factor')} | Sharpe {hit.get('sharpe')} | {hit.get('eras_profitable')}/5 Eras\n"
                f"• Why: {hit.get('rationale', '')}"
            )
        top = sorted(rows, key=lambda x: (x.get("eras_profitable", 0), x.get("net_15y", 0)), reverse=True)[:6]
        lines = [f"🏆 15-YEAR OPTIMAL DNA LEADERBOARD ({fmt_gst()}):"]
        for r in top:
            op = r.get("optimal_params", {})
            lines.append(
                f"• {r['epic']}: ATR({op.get('atr_len')},/{op.get('atr_div')}) ST {op.get('st_factor')} TP {op.get('tp_bricks')}B "
                f"{op.get('sessions_label')} → PF {r.get('profit_factor')} | ${float(r.get('net_15y', 0)):+,.0f} ({r.get('eras_profitable')}/5)"
            )
        lines.append("Send /dna <SYMBOL> (e.g. /dna US100) for full details.")
        return "\n".join(lines)

    if cmd == "/positions":
        creds = check_credentials_present()
        if not creds.get("connected"):
            return f"💼 Open Deals ({fmt_gst()}): Broker credentials not connected (offline/sample)."
        try:
            cap = Capital("live" if cur_cfg.get("live") else "demo")
            pos_raw = cap.positions()
            if not pos_raw:
                return f"💼 Open Deals ({fmt_gst()}): 0 open positions on Capital.com ({cap.env.upper()})."
            lines = [f"💼 Open Capital.com Deals ({len(pos_raw)} | {fmt_gst()}):"]
            for p in pos_raw[:10]:
                pi = p.get("position", {})
                mi = p.get("market", {})
                lines.append(
                    f"• {mi.get('epic', '?')} {pi.get('direction', '')} x{pi.get('size', 0)} @ {pi.get('level', '')} "
                    f"| SL {pi.get('stopLevel', '--')} | UPL ${float(pi.get('upl', 0)):+.2f}"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"💼 Position query error: {e}"

    if cmd == "/scanner":
        from scanner import get_cached_or_scan_all
        res = get_cached_or_scan_all(cur_cfg, cap=None)
        items = res.get("items", [])
        active = [r for r in items if r.get("signal_type") in ("BUY", "SELL") and not r.get("veto")]
        lines = [
            f"📊 {len(items)}-Market Scanner ({fmt_gst()}): {res.get('ready_count', 0)}/{len(items)} READY | {len(active)} ACTIVE SIGNALS"
        ]
        for r in (active or items[:6]):
            lines.append(f"• {r.get('epic')}: {r.get('signal')} @ {r.get('price')} ({r.get('filter_status')})")
        return "\n".join(lines)

    if cmd == "/pause":
        WATCHER.stop()
        return f"⏸ 24/7 Watcher PAUSED via Telegram ({fmt_gst()})."

    if cmd == "/resume":
        set_kill_switch(False)
        WATCHER.start()
        return f"▶ 24/7 Watcher RESUMED via Telegram ({fmt_gst()})."

    if cmd == "/flatten":
        set_kill_switch(True)
        WATCHER.stop()
        closed_cnt = 0
        creds = check_credentials_present()
        if creds.get("connected"):
            try:
                from bot import log_event
                cap = Capital("live" if cur_cfg.get("live") else "demo")
                for p in cap.positions():
                    did = p.get("position", {}).get("dealId")
                    sz = float(p.get("position", {}).get("size", 0.0))
                    ep = p.get("market", {}).get("epic", "")
                    if did:
                        try:
                            conf = cap.close_position(did)
                            log_event("FLATTEN", "CLOSE", sz, conf.get("level"), None, did, note=f"[{ep}] TG_FLATTEN")
                            closed_cnt += 1
                        except Exception:
                            pass
            except Exception:
                pass
        return f"🛑 EMERGENCY KILL SWITCH ENGAGED via Telegram ({fmt_gst()}). Closed {closed_cnt} deal(s)."

    return f"Unknown command '{cmd}'. Send /help for available commands."


class TelegramCommandPoller:
    """Polls Telegram getUpdates so the bot owner can control the cockpit remotely."""

    def __init__(self):
        self.running = False
        self._offset = 0
        self._boot_epoch = time.time() - 120.0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self):
        with self._lock:
            if self.running:
                return
            self.running = True
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self):
        self.running = False

    def poll_once(self) -> int:
        load_env()
        tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        expected_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not tok or not expected_chat:
            return 0
        url = (
            f"https://api.telegram.org/bot{tok}/getUpdates"
            f"?offset={self._offset}&timeout=1&allowed_updates=%5B%22message%22%5D"
        )
        handled = 0
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=6, context=SSL_CTX) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore") or "{}")
        except Exception:
            return 0
        if not data.get("ok"):
            return 0
        for upd in data.get("result", []):
            uid = int(upd.get("update_id", 0))
            if uid >= self._offset:
                self._offset = uid + 1
            msg = upd.get("message") or {}
            msg_date = float(msg.get("date", 0) or 0)
            chat_id = str((msg.get("chat") or {}).get("id", "")).strip()
            text = str(msg.get("text", "")).strip()
            if not text or chat_id != expected_chat:
                continue
            if self._offset == uid + 1 and msg_date and msg_date < self._boot_epoch:
                continue
            if text.startswith("/"):
                reply = handle_telegram_command(text)
                send_telegram_with_status(reply)
                handled += 1
        return handled

    def _loop(self):
        while self.running:
            try:
                self.poll_once()
            except Exception:
                pass
            time.sleep(4.0)


TG_POLLER = TelegramCommandPoller()


def run_parameter_optimizer(cfg: dict, body: dict | None = None) -> dict:
    """Run instrument-aware grid search over ATR Divisor × Usual TP Bricks × Confirm Minutes."""
    req = body if isinstance(body, dict) else {}
    base_cfg = copy.deepcopy(cfg)
    clean_epic = "GOLD" if str(base_cfg.get("epic", "GOLD")).upper() == "XAUUSD" else str(base_cfg.get("epic", "GOLD")).upper()
    if not req.get("preserve_current"):
        apply_instrument_dna_to_config(base_cfg, clean_epic)

    spec = INSTRUMENT_SPECS.get(clean_epic, {})
    cat = spec.get("category", "Metals")
    if req.get("atr_divisors"):
        divisors = [int(d) for d in req["atr_divisors"]]
    else:
        default_divs = [8, 10, 12, 15] if cat in ("Crypto", "Indices") else [7, 9, 12, 15]
        dna_div = int(round(float(base_cfg.get("strategy", {}).get("atr_div", 10))))
        if 4 <= dna_div <= 48 and dna_div not in default_divs:
            # Replace the closest non-extreme divisor or expand/trim to 4 divisors spanning the DNA
            merged = sorted(set(default_divs + [dna_div]))
            while len(merged) > 4:
                # Drop whichever candidate is furthest from dna_div while keeping endpoints balanced
                candidates = [x for x in merged if x != dna_div]
                drop_x = min(candidates, key=lambda x: (abs(x - dna_div), -x))
                merged.remove(drop_x)
            divisors = merged
        else:
            divisors = default_divs

    tp_list = req.get("tp_bricks") or [2.0, 2.5, 3.0]
    conf_list = req.get("confirm_minutes") or [0, 2, 3]
    bars, days = ensure_data(base_cfg)
    use_stop = bool(base_cfg.get("risk", {}).get("safety_stop_in_backtest", True))

    rows = []
    for div in divisors:
        for tp in tp_list:
            for conf in conf_list:
                tcfg = copy.deepcopy(base_cfg)
                tcfg["strategy"]["atr_div"] = int(div)
                tcfg["strategy"]["tp_bricks"] = float(tp)
                tcfg["strategy"]["confirm_minutes"] = int(conf)
                trades, eng, _, _ = simulate(bars, days, tcfg, use_stop=use_stop)
                st = stats(trades, "net")
                last_bs = eng.bricks[-1].brick_size if eng.bricks else float(tcfg["strategy"].get("fallback_size", 1.0))
                rows.append({
                    "atr_div": int(div),
                    "tp_bricks": float(tp),
                    "confirm_minutes": int(conf),
                    "brick_size": round(last_bs, 4),
                    "trades": int(st.get("trades", 0)),
                    "win_rate": float(st.get("win_rate_%", 0.0)),
                    "profit_factor": float(st.get("profit_factor", 0.0)),
                    "max_dd": float(st.get("max_drawdown", 0.0)),
                    "sharpe": float(st.get("sharpe", 0.0)),
                    "calmar": float(st.get("calmar", 0.0)),
                    "net": float(st.get("net", 0.0)),
                })

    rows.sort(
        key=lambda r: (
            1 if r["trades"] > 0 else 0,
            r["calmar"],
            r["profit_factor"],
            r["net"],
        ),
        reverse=True,
    )
    for idx, r in enumerate(rows, 1):
        r["rank"] = idx

    return {
        "ok": True,
        "epic": clean_epic,
        "resolution": base_cfg.get("resolution", "MINUTE"),
        "tested_combos": len(rows),
        "scanned_at_gst": fmt_gst(),
        "results": rows,
    }


def apply_instrument_dna_to_config(cfg: dict, epic: str) -> dict:
    """Apply the 15-Year Optimal parameter DNA for `epic` into `cfg` cleanly."""
    from research_15yr_matrix import get_or_build_matrix

    clean_epic = "GOLD" if str(epic or "GOLD").upper() == "XAUUSD" else str(epic or "GOLD").upper()
    cfg["epic"] = clean_epic
    if clean_epic in INSTRUMENT_SPECS:
        spec = INSTRUMENT_SPECS[clean_epic]
        cfg["tick"] = spec["tick"]
        cfg.setdefault("strategy", {})["fallback_size"] = spec["fallback_size"]

    mat = get_or_build_matrix()
    prof = mat.get("profiles_by_epic", {}).get(clean_epic) or (
        cfg.get("risk", {}).get("instrument_profiles", {}).get(clean_epic)
    )
    applied = {}
    if isinstance(prof, dict) and prof:
        cfg.setdefault("strategy", {})
        cfg.setdefault("risk", {})
        for sk in ("atr_len", "atr_div", "st_factor", "green_buy", "red_sell",
                   "tp_bricks", "runner_offset_bricks", "confirm_minutes", "use_htf_filter"):
            if sk in prof:
                cfg["strategy"][sk] = prof[sk]
        for rk in ("safety_stop_bricks", "breakeven_after_bricks", "max_spread_frac_of_brick",
                   "rollover_blackout", "macro_blackout"):
            if rk in prof:
                cfg["risk"][rk] = prof[rk]
        if isinstance(prof.get("sessions"), dict):
            cfg["risk"]["sessions"] = copy.deepcopy(prof["sessions"])
        cfg["risk"]["use_instrument_profiles"] = True
        cfg["risk"].setdefault("instrument_profiles", {})[clean_epic] = copy.deepcopy(prof)
        applied = copy.deepcopy(prof)
    return applied

CLOUD_STATE_DIR = Path(os.environ.get("RENDER_DISK_PATH", str(JOURNAL)))
CLOUD_STATE_DB = CLOUD_STATE_DIR / "cloud_state.sqlite"
_CLOUD_DB_LOCK = threading.Lock()


def _persist_snapshot_to_sqlite(snap: dict, db_path: Path | None = None):
    p = db_path or CLOUD_STATE_DB
    try:
        with _CLOUD_DB_LOCK:
            p.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(p))
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS cloud_state "
                    "(id INTEGER PRIMARY KEY, updated_at TEXT, payload_json TEXT)"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO cloud_state (id, updated_at, payload_json) VALUES (1, ?, ?)",
                    (snap.get("ts", datetime.now(timezone.utc).isoformat()), json.dumps(snap)),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def _load_snapshot_from_sqlite(db_path: Path | None = None) -> dict | None:
    p = db_path or CLOUD_STATE_DB
    if not p.exists():
        return None
    try:
        with _CLOUD_DB_LOCK:
            conn = sqlite3.connect(str(p))
            try:
                cur = conn.execute("SELECT payload_json FROM cloud_state WHERE id=1")
                row = cur.fetchone()
                if row and row[0]:
                    return json.loads(row[0])
            finally:
                conn.close()
    except Exception:
        pass
    return None


def build_cloud_backup_snapshot(cfg: dict | None = None) -> dict:
    cur_cfg = cfg or load_config()
    oms_data = {
        "pending_confirm": dict(_PENDING_CONFIRM),
        "pending_runners": dict(_PENDING_RUNNERS),
        "handled_runs": dict(_HANDLED_RUNS),
    }
    oms_file = _oms_state_path()
    if oms_file.exists():
        try:
            oms_data = json.loads(oms_file.read_text())
        except Exception:
            pass
    trades_rows = []
    if LIVE_TRADES.exists():
        try:
            with LIVE_TRADES.open() as f:
                trades_rows = list(csv.DictReader(f))[-250:]
        except Exception:
            trades_rows = []
    tg_tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    snap = {
        "ok": True,
        "ts": datetime.now(timezone.utc).isoformat(),
        "config": cur_cfg,
        "oms_state": oms_data,
        "live_trades": trades_rows,
        "telegram_config": {
            "bot_token": tg_tok,
            "chat_id": tg_chat,
        } if (tg_tok and tg_chat) else {},
    }
    _persist_snapshot_to_sqlite(snap)
    return snap


def restore_cloud_backup_snapshot(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {"ok": False, "restored": False}
    tg_cfg = payload.get("telegram_config")
    if isinstance(tg_cfg, dict):
        tg_tok = str(tg_cfg.get("bot_token", "")).strip()
        tg_chat = str(tg_cfg.get("chat_id", "")).strip()
        if tg_tok and tg_chat:
            update_dotenv({
                "TELEGRAM_BOT_TOKEN": tg_tok,
                "TELEGRAM_CHAT_ID": tg_chat,
            })

    if isinstance(payload.get("config"), dict):
        cfg = load_config()
        src = payload["config"]
        for k in ("epic", "resolution", "price", "tick", "size", "sizing_mode",
                  "risk_per_trade_usd", "mode", "live"):
            if k in src:
                cfg[k] = src[k]
        if isinstance(src.get("strategy"), dict):
            cfg["strategy"].update(src["strategy"])
        if isinstance(src.get("risk"), dict):
            for rk, rv in src["risk"].items():
                if rk == "sessions" and isinstance(rv, dict):
                    cfg["risk"].setdefault("sessions", {}).update(rv)
                else:
                    cfg["risk"][rk] = rv
        save_config(cfg)
        clear_backtest_cache()

    if isinstance(payload.get("oms_state"), dict):
        oms = payload["oms_state"]
        _PENDING_CONFIRM.clear()
        _PENDING_CONFIRM.update(oms.get("pending_confirm", {}))
        _PENDING_RUNNERS.clear()
        _PENDING_RUNNERS.update(oms.get("pending_runners", {}))
        _HANDLED_RUNS.clear()
        _HANDLED_RUNS.update(oms.get("handled_runs", {}))
        oms_file = _oms_state_path()
        oms_file.parent.mkdir(parents=True, exist_ok=True)
        oms_file.write_text(json.dumps(oms, indent=2))

    rows = payload.get("live_trades")
    if isinstance(rows, list) and rows and (not LIVE_TRADES.exists() or LIVE_TRADES.stat().st_size < 120):
        LIVE_TRADES.parent.mkdir(parents=True, exist_ok=True)
        cols = ["time", "event", "side", "size", "price", "stop", "deal_id", "realized_pnl", "note"]
        with LIVE_TRADES.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                if isinstance(r, dict):
                    w.writerow([r.get(c, "") for c in cols])

    snap = build_cloud_backup_snapshot()
    return {"ok": True, "restored": True, "ts": snap["ts"]}


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
    tg_tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    has_creds = bool(key and ident and pw)
    return {
        "connected": has_creds,
        "api_key": bool(key),
        "identifier": bool(ident),
        "password": bool(pw),
        "masked_identifier": mask_identifier(ident) if ident else None,
        "masked_key": mask_key(key) if key else None,
        "env": env,
        "telegram": bool(tg_tok and tg_chat),
        "telegram_chat_id": tg_chat,
        "telegram_masked_token": mask_key(tg_tok) if tg_tok else None,
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
                spec = INSTRUMENT_SPECS.get(epic.upper(), INSTRUMENT_SPECS.get("GOLD", {}))
                update_live_tick(epic, snap.get("bid"), snap.get("offer"), digits=spec.get("digits", 2))
                with _MARKET_SNAP_LOCK:
                    _MARKET_SNAP_CACHE[epic] = (now, snap, cand)
                return snap, cand
        except Exception:
            continue
    return {}, epic


_BACKTEST_CACHE: dict[str, tuple[float, dict]] = {}
_BACKTEST_CACHE_LOCK = threading.Lock()
BACKTEST_CACHE_TTL = 60.0  # seconds


def _bucket_filter_reason(reason: str) -> str:
    r = str(reason or "other").strip().lower()
    if r.startswith("overshoot"):
        return "Overshoot Guard"
    if "one_trade_per_run" in r:
        return "One-Trade-Per-Run Lock"
    if "news_spike" in r:
        return "News Spike Guard"
    if "5m_confirm" in r:
        return "5m Confirmation Gate"
    if "runner_+1b" in r:
        return "Runner +1B Unfilled"
    if "rollover" in r:
        return "Rollover Blackout"
    if "spread" in r:
        return "Wide Spread Veto"
    if "session" in r:
        return "Session Filter"
    return str(reason or "Other")


def _leg_summary(sub_trades: list[dict]) -> dict:
    n = len(sub_trades)
    if not n:
        return {
            "trades": 0, "win_rate_%": 0.0, "gross": 0.0, "net": 0.0,
            "spread_cost": 0.0, "avg_net": 0.0, "tp_hits": 0, "avg_mfe_bricks": 0.0,
        }
    wins = sum(1 for t in sub_trades if float(t.get("net", 0.0)) > 0)
    gross = round(sum(float(t.get("gross", 0.0)) for t in sub_trades), 2)
    net = round(sum(float(t.get("net", 0.0)) for t in sub_trades), 2)
    sp_cost = round(gross - net, 2)
    tp_hits = sum(1 for t in sub_trades if "TP" in str(t.get("why_out", "")))
    avg_mfe = round(sum(float(t.get("mfe_bricks", 0.0)) for t in sub_trades) / n, 2)
    return {
        "trades": n,
        "win_rate_%": round(100.0 * wins / n, 1),
        "gross": gross,
        "net": net,
        "spread_cost": sp_cost,
        "avg_net": round(net / n, 2),
        "tp_hits": tp_hits,
        "avg_mfe_bricks": avg_mfe,
    }


def _compute_tca_attribution(trades: list[dict], raw_trades: list[dict], filtered: list[dict]) -> dict:
    usual_trades = [t for t in trades if t.get("leg", "SINGLE") != "RUNNER"]
    runner_trades = [t for t in trades if t.get("leg") == "RUNNER"]
    total_gross = round(sum(float(t.get("gross", 0.0)) for t in trades), 2)
    total_net = round(sum(float(t.get("net", 0.0)) for t in trades), 2)
    raw_net = round(sum(float(t.get("net", 0.0)) for t in raw_trades), 2)
    total_spread = round(total_gross - total_net, 2)
    n_tot = len(trades)
    avg_over = round(sum(float(t.get("overshoot", 0.0)) for t in trades) / n_tot, 4) if n_tot else 0.0

    counts: dict[str, int] = {}
    for ev in filtered:
        lbl = _bucket_filter_reason(ev.get("reason", ""))
        counts[lbl] = counts.get(lbl, 0) + 1
    breakdown = [{"reason": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]

    return {
        "usual": _leg_summary(usual_trades),
        "runner": _leg_summary(runner_trades),
        "costs": {
            "total_gross": total_gross,
            "total_net": total_net,
            "raw_net": raw_net,
            "total_spread_cost": total_spread,
            "avg_spread_cost": round(total_spread / n_tot, 2) if n_tot else 0.0,
            "avg_overshoot": avg_over,
            "pro_edge_vs_raw": round(total_net - raw_net, 2),
            "filtered_count": len(filtered),
        },
        "filter_breakdown": breakdown,
    }


def render_tearsheet_html(cfg: dict, bt: dict) -> str:
    """Render an institutional printable HTML quantitative strategy tear-sheet."""
    ds = bt.get("data_summary", {})
    sn = bt.get("stats_net", {})
    sg = bt.get("stats_gross", {})
    raw_sn = bt.get("raw_pine_stats_net", {})
    tca = bt.get("tca_attribution", {})
    sessions = bt.get("sessions_net", {})
    trades = bt.get("trades", [])
    strat = cfg.get("strategy", {})
    epic = html.escape(str(ds.get("epic", cfg.get("epic", "GOLD"))))
    name = html.escape(str(ds.get("name", epic)))
    gen_at = fmt_gst(datetime.now(timezone.utc), "%Y-%m-%d %H:%M GST")
    edge = round(float(sn.get("net", 0.0)) - float(raw_sn.get("net", 0.0)), 2)

    leg_rows = ""
    for lbl, st_leg in (
        ("Leg 1: USUAL (3B-TP / Single)", tca.get("usual", {})),
        ("Leg 2: RUNNER (+1B Continuation)", tca.get("runner", {})),
        ("Combined Pro Strategy (Net)", {"trades": sn.get("trades", 0), "win_rate_%": sn.get("win_rate_%", 0),
                                          "gross": sg.get("net", 0), "spread_cost": tca.get("costs", {}).get("total_spread_cost", 0),
                                          "net": sn.get("net", 0), "avg_net": sn.get("avg_trade", 0)}),
    ):
        leg_rows += (
            f"<tr><td><b>{html.escape(lbl)}</b></td>"
            f"<td>{st_leg.get('trades', 0)}</td>"
            f"<td>{st_leg.get('win_rate_%', 0)}%</td>"
            f"<td>${float(st_leg.get('gross', 0)):+.2f}</td>"
            f"<td>-${abs(float(st_leg.get('spread_cost', 0))):.2f}</td>"
            f"<td><b>${float(st_leg.get('net', 0)):+.2f}</b></td>"
            f"<td>${float(st_leg.get('avg_net', 0)):+.2f}</td></tr>"
        )

    sess_rows = ""
    for sk, sv in sessions.items():
        sess_rows += (
            f"<tr><td>{html.escape(sk.replace('_', ' ').title())}</td>"
            f"<td>{sv.get('trades', 0)}</td><td>{sv.get('win_rate_%', 0)}%</td>"
            f"<td>{sv.get('profit_factor', 0)}</td><td><b>${float(sv.get('net', 0)):+.2f}</b></td></tr>"
        )

    flt_rows = "".join(
        f"<tr><td>{html.escape(str(r['reason']))}</td><td>{r['count']}</td></tr>"
        for r in tca.get("filter_breakdown", [])
    ) or "<tr><td colspan='2'>No blocked entries recorded.</td></tr>"

    trade_rows = ""
    for t in trades[-60:]:
        trade_rows += (
            f"<tr><td>{t.get('num')}</td><td>{html.escape(str(t.get('leg', 'SINGLE')))}</td>"
            f"<td>{html.escape(str(t.get('side', '')))}</td><td>{html.escape(fmt_gst(t.get('entry_time', ''), '%Y-%m-%d %H:%M'))}</td>"
            f"<td>{t.get('entry')}</td><td>{t.get('exit')}</td>"
            f"<td>{html.escape(str(t.get('why_out', '')))}</td>"
            f"<td><b>${float(t.get('net', 0)):+.2f}</b></td></tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Institutional Tear-Sheet — {epic}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color: #0f172a; margin: 28px auto; max-width: 1020px; line-height: 1.45; }}
  .hdr {{ display:flex; justify-content:space-between; align-items:flex-end; border-bottom:2px solid #0f172a; padding-bottom:10px; margin-bottom:18px; }}
  .kpis {{ display:grid; grid-template-columns:repeat(6, 1fr); gap:10px; margin-bottom:18px; }}
  .kpi {{ border:1px solid #cbd5e1; border-radius:6px; padding:10px; background:#f8fafc; }}
  .kpi .lbl {{ font-size:10px; text-transform:uppercase; color:#475569; font-weight:700; }}
  .kpi .val {{ font-size:17px; font-weight:800; margin-top:2px; font-family:ui-monospace, monospace; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:18px; font-size:12px; }}
  th, td {{ border-bottom:1px solid #e2e8f0; padding:6px 8px; text-align:left; font-family:ui-monospace, monospace; }}
  th {{ background:#f1f5f9; font-family:inherit; font-size:11px; text-transform:uppercase; color:#334155; }}
  h2 {{ font-size:14px; margin:18px 0 8px; color:#0f172a; border-left:3px solid #0284c7; padding-left:8px; }}
  .grid2 {{ display:grid; grid-template-columns:1.2fr 1fr; gap:18px; }}
  .btn-print {{ background:#0284c7; color:#fff; border:none; border-radius:4px; padding:7px 14px; font-weight:700; cursor:pointer; }}
  @media print {{ .no-print {{ display:none !important; }} body {{ margin: 10mm; }} }}
</style></head><body>
  <div class="hdr">
    <div>
      <div style="font-size:11px; font-weight:700; color:#0284c7; letter-spacing:0.06em;">RENKO ATR PRO INSTITUTIONAL QUANT DESK</div>
      <h1 style="margin:2px 0; font-size:22px;">Strategy Performance Tear-Sheet — {epic} ({name})</h1>
      <div style="font-size:11.5px; color:#475569;">ATR({strat.get('atr_len',5)}, /{strat.get('atr_div',15)}) · ST({strat.get('st_len',1)}, {strat.get('st_factor',2.1)}) · Confirm {strat.get('confirm_minutes',0)}m · Model E Dual-Order: {'ON' if strat.get('dual_order_mode') else 'OFF'} · Generated {gen_at}</div>
    </div>
    <button class="btn-print no-print" onclick="window.print()">🖨️ Print / Save PDF</button>
  </div>
  <div class="kpis">
    <div class="kpi"><div class="lbl">Net P&amp;L</div><div class="val">${float(sn.get('net',0)):+.2f}</div></div>
    <div class="kpi"><div class="lbl">Pro Edge vs Raw</div><div class="val">${edge:+.2f}</div></div>
    <div class="kpi"><div class="lbl">Win Rate / Trades</div><div class="val">{sn.get('win_rate_%',0)}% ({sn.get('trades',0)})</div></div>
    <div class="kpi"><div class="lbl">Profit Factor / R</div><div class="val">{sn.get('profit_factor',0)} / {sn.get('payoff_ratio',0)}R</div></div>
    <div class="kpi"><div class="lbl">Sharpe / Sortino</div><div class="val">{sn.get('sharpe',0)} / {sn.get('sortino',0)}</div></div>
    <div class="kpi"><div class="lbl">Max DD / 95% VaR</div><div class="val">-${float(sn.get('max_drawdown',0)):.2f} / -${float(sn.get('daily_var_95',0)):.2f}</div></div>
  </div>
  <h2>Transaction Cost &amp; Model E Leg Attribution</h2>
  <table><thead><tr><th>Cohort</th><th>Trades</th><th>Win Rate</th><th>Gross P&amp;L</th><th>Spread Drag</th><th>Net P&amp;L</th><th>Avg Net/Trade</th></tr></thead><tbody>{leg_rows}</tbody></table>
  <div class="grid2">
    <div><h2>Session Performance (UAE GST / UTC+4)</h2><table><thead><tr><th>Session</th><th>Trades</th><th>Win Rate</th><th>PF</th><th>Net P&amp;L</th></tr></thead><tbody>{sess_rows}</tbody></table></div>
    <div><h2>Institutional Guard Vetoes ({ds.get('filtered_count',0)} Total)</h2><table><thead><tr><th>Guard / Filter</th><th>Blocked Signals</th></tr></thead><tbody>{flt_rows}</tbody></table></div>
  </div>
  <h2>Executed Trade Ledger (Latest 60)</h2>
  <table><thead><tr><th>#</th><th>Leg</th><th>Side</th><th>Entry (GST)</th><th>Entry Px</th><th>Exit Px</th><th>Exit Reason</th><th>Net P&amp;L</th></tr></thead><tbody>{trade_rows}</tbody></table>
</body></html>"""


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
        "tca_attribution": _compute_tca_attribution(trades, raw_trades, filtered),
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
        "telegram": {
            "configured": bool(creds.get("telegram")),
            "chat_id": creds.get("telegram_chat_id", ""),
            "masked_token": creds.get("telegram_masked_token"),
        },
        "watcher": WATCHER.state(),
        "kill_switch": kill_switch_active(),
        "mode": cfg.get("mode", "semi-auto"),
        "live": bool(cfg.get("live", False)),
        "today_trades": cnt,
        "today_pnl": pnl,
        "pending_ticket": pending,
        "pending_confirm": dict(_PENDING_CONFIRM),
        "pending_runners": dict(_PENDING_RUNNERS),
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
        if u.path in ("/healthz", "/", "/index.html", "/studio", "/studio.html", "/guide", "/guide.html", "/api/guide.pdf", "/guide.pdf"):
            self.send_response(200)
            if u.path.startswith("/healthz"):
                ctype = "application/json"
            elif u.path.endswith(".pdf"):
                ctype = "application/pdf"
            else:
                ctype = "text/html; charset=utf-8"
            self.send_header("Content-Type", ctype)
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

        if u.path in ("/guide", "/guide.html"):
            p = STATIC / "guide.html"
            if not p.exists():
                self._send_json({"error": "Master Guide has not been generated yet."}, 404)
                return
            raw = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(raw)
            return

        if u.path in ("/api/guide.pdf", "/guide.pdf"):
            p = ROOT / "Renko_ATR_Cloud_Cockpit_McKinsey_Master_Guide.pdf"
            if not p.exists():
                self._send_json({"error": "Master Guide PDF not found."}, 404)
                return
            raw = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", 'inline; filename="Renko_ATR_Cloud_Cockpit_McKinsey_Master_Guide.pdf"')
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

        if u.path in ("/api/status", "/api/broker-status"):
            cfg = load_config()
            bot_st = get_bot_status(cfg, skip_network=True)
            self._send_json({
                "ok": True,
                "credentials": bot_st.get("credentials", {}),
                "telegram": bot_st.get("telegram", {"configured": False, "chat_id": ""}),
                "watcher": bot_st.get("watcher", {}),
                "kill_switch": bot_st.get("kill_switch", False),
            })
            return

        if u.path == "/api/tearsheet":
            cfg = load_config()
            bt = serialize_backtest(cfg)
            raw = render_tearsheet_html(cfg, bt).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(raw)
            return

        if u.path == "/api/export-trades.csv":
            cfg = load_config()
            bt = serialize_backtest(cfg)
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["num", "leg", "side", "size", "session", "entry_time", "exit_time",
                        "trigger_px", "entry", "exit", "overshoot", "why_in", "why_out",
                        "mfe_bricks", "mae_bricks", "gross", "net", "cum_net"])
            for t in bt.get("trades", []):
                w.writerow([
                    t.get("num"), t.get("leg", "SINGLE"), t.get("side"), t.get("size"),
                    t.get("session"), t.get("entry_time"), t.get("exit_time"),
                    t.get("trigger_px"), t.get("entry"), t.get("exit"), t.get("overshoot"),
                    t.get("why_in"), t.get("why_out"), t.get("mfe_bricks"), t.get("mae_bricks"),
                    t.get("gross"), t.get("net"), t.get("cum_net"),
                ])
            raw = buf.getvalue().encode("utf-8")
            epic_safe = "".join(c for c in str(cfg.get("epic", "GOLD")).upper() if c.isalnum() or c in "_-")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="renko_trades_{epic_safe}.csv"')
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if u.path == "/api/stream":
            cfg = load_config()
            cnt, pnl = today_stats()
            pending_ticket = None
            if PENDING_FILE.exists():
                try:
                    pending_ticket = json.loads(PENDING_FILE.read_text())
                except Exception:
                    pending_ticket = None
            pulse = {
                "ok": True,
                "ts": fmt_gst(),
                "watcher": WATCHER.state(),
                "kill_switch": kill_switch_active(),
                "today_trades": cnt,
                "today_pnl": pnl,
                "pending_ticket": pending_ticket,
                "pending_confirm": dict(_PENDING_CONFIRM),
                "pending_runners": dict(_PENDING_RUNNERS),
                "live_tick": get_live_tick(cfg.get("epic", "GOLD")),
            }
            body = f"retry: 5000\nevent: pulse\ndata: {json.dumps(pulse)}\n\n".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if u.path == "/api/backup-state":
            self._send_json(build_cloud_backup_snapshot())
            return

        if u.path == "/api/matrix-15y":
            from research_15yr_matrix import get_or_build_matrix
            self._send_json(get_or_build_matrix())
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
                TICK_STREAMER.start([cfg.get("epic", "GOLD")], env=env)

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
                TICK_STREAMER.stop()
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

            if u.path == "/api/save-telegram":
                load_env()
                raw_tok = str(body.get("bot_token", "")).strip()
                if not raw_tok or raw_tok == "***" or "•" in raw_tok:
                    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
                else:
                    tok = raw_tok
                chat_id = str(body.get("chat_id", "")).strip() or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
                if not tok or not chat_id:
                    self._send_json({"ok": False, "error": "Telegram Bot Token and Chat ID are both required."}, 400)
                    return
                update_dotenv({
                    "TELEGRAM_BOT_TOKEN": tok,
                    "TELEGRAM_CHAT_ID": chat_id,
                })
                build_cloud_backup_snapshot(cfg)
                TG_POLLER.start()
                self._send_json({
                    "ok": True,
                    "message": f"Telegram alerts & 2-way remote control active for Chat ID {chat_id}.",
                    "bot": get_bot_status(cfg, skip_network=True),
                })
                return

            if u.path == "/api/test-telegram":
                txt = str(body.get("message", "")).strip() or (
                    f"🤖 Renko Cloud Bot Test Alert\n"
                    f"• Market: {cfg.get('epic', 'GOLD')} ({cfg.get('resolution', 'MINUTE')})\n"
                    f"• Chat ID: {os.environ.get('TELEGRAM_CHAT_ID', '')}\n"
                    f"• Time: {fmt_gst()}"
                )
                res = send_telegram_with_status(txt)
                code = 200 if res.get("ok") else 400
                self._send_json({
                    "ok": bool(res.get("ok")),
                    "detail": res.get("detail", ""),
                    "chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
                    "bot": get_bot_status(cfg, skip_network=True),
                }, code)
                return

            if u.path == "/api/telegram-digest":
                digest_txt = build_daily_telegram_digest(cfg)
                res = send_telegram_with_status(digest_txt)
                code = 200 if res.get("ok") else 400
                self._send_json({
                    "ok": bool(res.get("ok")),
                    "detail": res.get("detail", ""),
                    "digest": digest_txt,
                    "bot": get_bot_status(cfg, skip_network=True),
                }, code)
                return

            if u.path == "/api/optimize":
                res = run_parameter_optimizer(cfg, body)
                self._send_json(res)
                return

            if u.path == "/api/save-password":
                pw = str(body.get("password", "")).strip()
                update_dotenv({"DASHBOARD_PASSWORD": pw if pw else None})
                build_cloud_backup_snapshot(cfg)
                if pw:
                    tok = expected_session_token(pw)
                    cookie = f"renko_session={tok}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"
                    self._send_json({
                        "ok": True,
                        "password_protected": True,
                        "token": tok,
                        "message": "Dashboard password lock enabled.",
                        "bot": get_bot_status(cfg, skip_network=True),
                    }, extra_headers=[("Set-Cookie", cookie)])
                else:
                    cookie = "renko_session=; Path=/; HttpOnly; Max-Age=0"
                    self._send_json({
                        "ok": True,
                        "password_protected": False,
                        "message": "Dashboard password removed (Open Access).",
                        "bot": get_bot_status(cfg, skip_network=True),
                    }, extra_headers=[("Set-Cookie", cookie)])
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

            if u.path == "/api/scanner/toggle":
                import scanner
                target_epic = str(body.get("epic", "")).strip().upper()
                if not target_epic:
                    self._send_json({"ok": False, "error": "Missing epic"}, 400)
                    return
                risk_cfg = cfg.setdefault("risk", {})
                disabled_list = [str(x).upper() for x in risk_cfg.get("disabled_epics", [])]
                disabled_set = set(disabled_list)
                if "enabled" in body:
                    want_enabled = bool(body["enabled"])
                else:
                    want_enabled = target_epic in disabled_set
                if want_enabled:
                    disabled_set.discard(target_epic)
                else:
                    disabled_set.add(target_epic)
                risk_cfg["disabled_epics"] = sorted(disabled_set)
                save_config(cfg)
                scanner._SCAN_CACHE_TIME = 0.0
                self._send_json({
                    "ok": True,
                    "epic": target_epic,
                    "auto_enabled": target_epic not in disabled_set,
                    "disabled_epics": risk_cfg["disabled_epics"],
                })
                return

            if u.path == "/api/apply-instrument-dna":
                target_epic = str(body.get("epic", cfg.get("epic", "GOLD"))).upper()
                applied = apply_instrument_dna_to_config(cfg, target_epic)
                save_config(cfg)
                clear_backtest_cache()
                self._send_json({
                    "ok": True,
                    "epic": cfg["epic"],
                    "applied_dna": applied,
                    "config": cfg,
                    "bot": get_bot_status(cfg, skip_network=True),
                    "backtest": serialize_backtest(cfg, use_cache=False, skip_network=True),
                    "instrument_specs": INSTRUMENT_SPECS,
                })
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
                if bool(body.get("apply_dna", False)):
                    apply_instrument_dna_to_config(cfg, target_epic)
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

            if u.path == "/api/restore-state":
                res = restore_cloud_backup_snapshot(body)
                cfg = load_config()
                res["bot"] = get_bot_status(cfg)
                res["config"] = cfg
                self._send_json(res)
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
    if CLOUD_STATE_DB.exists() and (not LIVE_TRADES.exists() or not os.environ.get("TELEGRAM_BOT_TOKEN")):
        saved = _load_snapshot_from_sqlite()
        if saved:
            restore_cloud_backup_snapshot(saved)
            cfg = load_config()
    ensure_data(cfg)

    if os.environ.get("AUTO_START_WATCHER", "").lower() in ("1", "true", "yes") or check_credentials_present().get("connected"):
        WATCHER.start()
        TICK_STREAMER.start([cfg.get("epic", "GOLD")], env="live" if cfg.get("live") else "demo")
        print("24/7 Watcher & WebSocket Tick Streamer auto-started (Broker credentials active).")
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        TG_POLLER.start()
        print("Telegram 2-Way Remote Command Poller started.")

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"Renko ATR Pro Cloud Dashboard running at http://{a.host}:{a.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
