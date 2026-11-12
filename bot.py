"""Live / Semi-Auto Renko ATR Bot for Capital.com."""
from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backtest import load_config, pick
from capital_api import Capital, CapitalError, RES_MINUTES
from renko import DailyAtr, Params, RenkoEngine, brick_size_from_atr
from risk import (check_entry_allowed, compute_size, kill_switch_active,
                  safety_stop_level, set_kill_switch, should_trail_stop, today_stats)

ROOT = Path(__file__).parent
JOURNAL = ROOT / "journal"
PENDING_FILE = JOURNAL / "pending_ticket.json"
LIVE_TRADES = JOURNAL / "live_trades.csv"


def log_event(event: str, side: str, size: float, price: float | None, stop: float | None,
              deal_id: str = "", realized_pnl: float = 0.0, note: str = ""):
    JOURNAL.mkdir(parents=True, exist_ok=True)
    new_file = not LIVE_TRADES.exists()
    with LIVE_TRADES.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["time", "event", "side", "size", "price", "stop", "deal_id", "realized_pnl", "note"])
        w.writerow([
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            event, side, size, price if price is not None else "",
            stop if stop is not None else "", deal_id, realized_pnl or "", note
        ])


EPIC_ALIASES = {
    "GOLD": ["GOLD", "XAUUSD", "XAU/USD"],
    "XAUUSD": ["XAUUSD", "GOLD", "XAU/USD"],
    "SILVER": ["SILVER", "XAGUSD", "XAG/USD"],
    "XAGUSD": ["XAGUSD", "SILVER", "XAG/USD"],
    "OIL_CRUDE": ["OIL_CRUDE", "USCRUDE", "WTI", "USOIL", "OIL", "LIGHTCRUDE"],
    "OIL_BRENT": ["OIL_BRENT", "BRENT", "UKOIL"],
    "EURUSD": ["EURUSD", "EUR/USD"],
    "GBPUSD": ["GBPUSD", "GBP/USD"],
    "USDJPY": ["USDJPY", "USD/JPY"],
    "AUDUSD": ["AUDUSD", "AUD/USD"],
    "USDCAD": ["USDCAD", "USD/CAD"],
    "USDCHF": ["USDCHF", "USD/CHF"],
    "US500": ["US500", "SP500", "USA500", "US500_M"],
    "US100": ["US100", "NASDAQ", "USTECH100", "US100_M"],
    "US30": ["US30", "WALLSTREET", "US30_M"],
    "GER40": ["GER40", "DAX", "DE40"],
    "UK100": ["UK100", "FTSE"],
    "JP225": ["JP225", "NIKKEI"],
}


def resolve_epic_alias(epic: str) -> list[str]:
    up = epic.upper().replace("/", "")
    if up in EPIC_ALIASES:
        return list(dict.fromkeys([epic] + EPIC_ALIASES[up]))
    for k, aliases in EPIC_ALIASES.items():
        clean_aliases = [a.upper().replace("/", "") for a in aliases]
        if up in clean_aliases:
            return list(dict.fromkeys([epic] + aliases))
    return [epic]


def warmup_engine(cap: Capital, cfg: dict):
    """Fetch recent history to warm up DailyAtr and RenkoEngine without repainting."""
    epic = cfg["epic"]
    res = cfg["resolution"]
    price_type = cfg.get("price", "mid")
    p = Params.from_dict(cfg["strategy"])

    end = datetime.now(timezone.utc)
    bars = []
    days = []
    for cand in resolve_epic_alias(epic):
        try:
            b_try = cap.prices(cand, res, end - timedelta(days=5), end)
            if b_try:
                bars = b_try
                cfg["epic"] = cand
                try:
                    days = cap.prices(cand, "DAY", end - timedelta(days=35), end)
                except Exception:
                    pass
                break
        except Exception:
            continue

    if not bars:
        try:
            from backtest import ensure_data
            bars, _ = ensure_data(cfg)
        except Exception:
            bars = []

    bar_delta = timedelta(minutes=RES_MINUTES.get(res, 1))
    closed_bars = [b for b in bars if b["t"] + bar_delta <= end] if bars else []

    datr = DailyAtr([d["t"] for d in days], [pick(d, "h", price_type) for d in days],
                    [pick(d, "l", price_type) for d in days], [pick(d, "c", price_type) for d in days], p.atr_len) if days else None
    eng = RenkoEngine(p)
    tick = cfg["tick"]
    last_n = 0
    last_bs = p.fallback_size
    for b in closed_bars:
        last_bs = brick_size_from_atr(datr.at(b["t"]) if datr else None, p, tick)
        last_n = eng.on_bar(b["t"], pick(b, "h", price_type), pick(b, "l", price_type),
                            pick(b, "c", price_type), last_bs)
    return eng, datr, closed_bars, last_n, last_bs


def broker_position(cap: Capital, epic: str):
    """Inspect open positions on Capital.com for this epic (including aliases)."""
    cands = set(resolve_epic_alias(epic))
    try:
        raw_pos = cap.positions()
    except Exception:
        raw_pos = []
    positions = [p for p in raw_pos if p.get("market", {}).get("epic") in cands]
    if not positions:
        return 0, [], None, 0.0
    direction = positions[0].get("position", {}).get("direction", "BUY")
    pos_dir = 1 if direction == "BUY" else -1
    deal_ids = [p["position"]["dealId"] for p in positions]
    stop_level = positions[0]["position"].get("stopLevel")
    total_size = sum(float(p["position"].get("size", 0)) for p in positions)
    return pos_dir, deal_ids, stop_level, total_size


def execute_decision(cap: Capital, cfg: dict, action: str, reverse: bool,
                     last_close: float, bs: float, deal_ids: list[str], size: float | None = None):
    epic = cfg["epic"]
    sz = size if size is not None else compute_size(cfg, bs)
    if action in ("EXIT", "COVER") or (reverse and deal_ids):
        for did in deal_ids:
            try:
                conf = cap.close_position(did)
                log_event("CLOSE", action, sz, conf.get("level"), None, did,
                          realized_pnl=conf.get("profit", 0.0), note=conf.get("dealStatus", ""))
                print(f"Closed deal {did}: status={conf.get('dealStatus')} level={conf.get('level')}")
            except Exception as e:
                print(f"Error closing deal {did}: {e}")
                log_event("ERROR", action, sz, None, None, did, note=str(e))
        if action in ("EXIT", "COVER"):
            return

    if action in ("BUY", "SELL"):
        side = 1 if action == "BUY" else -1
        stop = safety_stop_level(side, last_close, bs, cfg)
        conf = None
        last_err = None
        for cand in resolve_epic_alias(epic):
            try:
                conf = cap.open_position(cand, action, sz, stop_level=stop)
                cfg["epic"] = cand
                break
            except Exception as e:
                last_err = e
                continue
        if conf:
            did = conf.get("dealId", "")
            evt = "REVERSE_OPEN" if reverse else "OPEN"
            log_event(evt, action, sz, conf.get("level"), stop, did, note=conf.get("dealStatus", ""))
            print(f"Opened {action} {sz} {cfg['epic']}: dealId={did} status={conf.get('dealStatus')} "
                  f"level={conf.get('level')} stop={stop}")
        else:
            log_event("ERROR", action, sz, None, stop, note=f"Order failed: {last_err}")
            print(f"Failed to open {action}: {last_err}")


def cmd_status(a, cfg):
    cap = Capital("live" if cfg.get("live") else "demo")
    eng, _, closed_bars, last_n, bs = warmup_engine(cap, cfg)
    pos_dir, deal_ids, stop_lvl, total_sz = broker_position(cap, cfg["epic"])
    mkt = cap.market(cfg["epic"]).get("snapshot", {})
    bid, ask = mkt.get("bid"), mkt.get("offer")
    cnt, pnl = today_stats()

    print(f"Environment : {cap.env.upper()} | Mode: {cfg.get('mode', 'semi-auto')} | Kill switch: {'ACTIVE' if kill_switch_active() else 'off'}")
    print(f"Market      : {cfg['epic']} ({cfg['resolution']}) | Bid: {bid} | Ask: {ask} | Status: {mkt.get('marketStatus')}")
    if closed_bars:
        print(f"Last bar    : {closed_bars[-1]['t'].isoformat()} | Close: {closed_bars[-1]['c']:.2f}")
    print(f"Renko state : {eng.brick_cnt} bricks | Size: {bs:.2f} | Last brick close: {eng.last_close:.2f}")
    print(f"Runs & ST   : Green run={eng.green_run} | Red run={eng.red_run} | Supertrend={'BULL' if eng.st_dir==1 else 'BEAR'} ({eng.st_now:.2f})")
    pos_label = "LONG" if pos_dir > 0 else ("SHORT" if pos_dir < 0 else "FLAT")
    print(f"Position    : {pos_label} (size={total_sz}, deals={deal_ids}, stop={stop_lvl})")
    print(f"Today stats : {cnt} entries | Realized P&L: {pnl:.2f}")
    last_c = closed_bars[-1]["c"] if closed_bars else None
    d = eng.decide(pos_dir, last_n, bar_close=last_c, brick_size=bs)
    print(f"Signal now  : {d.action or 'NONE'} (reverse={d.reverse}, filter={d.filtered_reason or 'none'})")
    if PENDING_FILE.exists():
        print(f"Pending tkt : {PENDING_FILE.read_text().strip()}")


def step_once(cap: Capital, cfg: dict):
    eng, _, closed_bars, last_n, bs = warmup_engine(cap, cfg)
    if not closed_bars:
        print("No closed bars returned.")
        return
    pos_dir, deal_ids, current_stop, _ = broker_position(cap, cfg["epic"])
    mkt = {}
    for cand in resolve_epic_alias(cfg["epic"]):
        try:
            res = cap.market(cand)
            if res and res.get("snapshot"):
                mkt = res.get("snapshot", {})
                cfg["epic"] = cand
                break
        except Exception:
            continue
    bid, ask = mkt.get("bid"), mkt.get("offer")

    if pos_dir != 0 and deal_ids and eng.last_close is not None:
        new_stop = safety_stop_level(pos_dir, eng.last_close, bs, cfg)
        if should_trail_stop(pos_dir, current_stop, new_stop, cfg["tick"]):
            for did in deal_ids:
                cap.update_stop(did, new_stop)
            print(f"Trailed safety stop on {deal_ids} -> {new_stop}")

    last_c = closed_bars[-1]["c"]
    d = eng.decide(pos_dir, last_n, bar_close=last_c, brick_size=bs)
    last_bar_t = closed_bars[-1]["t"].isoformat()
    st_label = "BULL" if eng.st_dir == 1 else "BEAR"
    print(f"[{last_bar_t}] brick={eng.last_close:.2f} (bs={bs:.2f}) G={eng.green_run} R={eng.red_run} "
          f"ST={st_label} pos={pos_dir} -> signal={d.action or 'NONE'} ({d.filtered_reason or 'ok'})")

    # Record scan pulse in activity log so trader sees live monitoring in dashboard
    need_scan_log = False
    if not LIVE_TRADES.exists() or LIVE_TRADES.stat().st_size < 120:
        need_scan_log = True
    elif last_n > 0:
        need_scan_log = True

    if need_scan_log:
        status_note = f"G:{eng.green_run} R:{eng.red_run} | ST:{st_label} | Brick:${bs:.2f} | Sig:{d.action or 'Waiting'}"
        log_event("SCAN", st_label, 0.0, eng.last_close, None, "", note=status_note)

    if not d.action:
        return

    sz = compute_size(cfg, bs)
    if d.action in ("BUY", "SELL"):
        ok, reason = check_entry_allowed(cfg, bid, ask, bs)
        if not ok:
            print(reason)
            log_event("BLOCKED", d.action, sz, bid if d.action == "SELL" else ask, None, note=reason)
            return

    if cfg.get("mode", "semi-auto") == "semi-auto":
        side = 1 if d.action == "BUY" else (-1 if d.action == "SELL" else 0)
        stop = safety_stop_level(side, eng.last_close, bs, cfg) if side else None
        ttl = int(cfg.get("risk", {}).get("ticket_ttl_seconds", 180))
        now_utc = datetime.now(timezone.utc)
        ticket = {
            "created_at": now_utc.isoformat(),
            "expires_at": (now_utc + timedelta(seconds=ttl)).isoformat(),
            "bar_time": last_bar_t,
            "epic": cfg["epic"],
            "action": d.action,
            "reverse": d.reverse,
            "size": sz,
            "trigger_brick_px": d.trigger_brick_px,
            "last_brick_close": eng.last_close,
            "brick_size": bs,
            "safety_stop": stop,
            "bid": bid,
            "ask": ask,
            "deal_ids": deal_ids,
        }
        JOURNAL.mkdir(parents=True, exist_ok=True)
        PENDING_FILE.write_text(json.dumps(ticket, indent=2))
        print("\n=== SEMI-AUTO TICKET READY ===")
        print(json.dumps(ticket, indent=2))
        print("Run `python3 bot.py approve` (or click Approve in the Dashboard) before expiry.")
    else:
        execute_decision(cap, cfg, d.action, d.reverse, eng.last_close, bs, deal_ids, size=sz)


def cmd_watch(a, cfg):
    if cfg.get("live") and not a.confirm_live:
        raise SystemExit("Refusing to run with live=true without --confirm-live.")
    cap = Capital("live" if cfg.get("live") else "demo")
    if a.once:
        step_once(cap, cfg)
        return
    step_sec = max(15, RES_MINUTES[cfg["resolution"]] * 60 // 2)
    print(f"Watching {cfg['epic']} ({cfg['resolution']}) on {cap.env.upper()} every {step_sec}s. Ctrl+C to stop.")
    while not kill_switch_active():
        try:
            step_once(cap, cfg)
        except CapitalError as e:
            print("API error:", e)
        time.sleep(step_sec)


def cmd_approve(a, cfg):
    if not PENDING_FILE.exists():
        raise SystemExit("No pending ticket in journal/pending_ticket.json")
    t = json.loads(PENDING_FILE.read_text())
    if "expires_at" in t and not a.force:
        exp = datetime.fromisoformat(t["expires_at"])
        if datetime.now(timezone.utc) > exp:
            PENDING_FILE.unlink()
            raise SystemExit(f"Ticket expired at {t['expires_at']} (TTL guard). Discarded.")
    cap = Capital("live" if cfg.get("live") else "demo")
    execute_decision(cap, cfg, t["action"], t["reverse"], t["last_brick_close"],
                     t["brick_size"], t["deal_ids"], size=t.get("size"))
    PENDING_FILE.unlink()


def cmd_reject(a, cfg):
    if PENDING_FILE.exists():
        PENDING_FILE.unlink()
        print("Pending ticket discarded.")
    else:
        print("No pending ticket.")


def cmd_flatten(a, cfg):
    set_kill_switch(True)
    cap = Capital("live" if cfg.get("live") else "demo")
    _, deal_ids, _, size = broker_position(cap, cfg["epic"])
    for did in deal_ids:
        conf = cap.close_position(did)
        log_event("FLATTEN", "CLOSE", size, conf.get("level"), None, did, note="KILL_SWITCH")
        print(f"Emergency closed {did}: {conf.get('dealStatus')}")
    print("STOP kill switch engaged. All positions closed.")


def cmd_resume(a, cfg):
    set_kill_switch(False)
    print("STOP kill switch cleared.")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    w = sub.add_parser("watch")
    w.add_argument("--once", action="store_true")
    w.add_argument("--confirm-live", action="store_true")
    ap_cmd = sub.add_parser("approve")
    ap_cmd.add_argument("--force", action="store_true")
    sub.add_parser("reject")
    sub.add_parser("flatten")
    sub.add_parser("resume")
    a = ap.parse_args()
    cfg = load_config()
    {"status": cmd_status, "watch": cmd_watch, "approve": cmd_approve,
     "reject": cmd_reject, "flatten": cmd_flatten, "resume": cmd_resume}[a.cmd](a, cfg)


if __name__ == "__main__":
    main()
