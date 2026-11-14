"""Live / Semi-Auto Renko ATR Bot for Capital.com."""
from __future__ import annotations

import argparse
import copy
import csv
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backtest import load_config, pick, INSTRUMENT_SPECS
from capital_api import Capital, CapitalError, RES_MINUTES
from renko import DailyAtr, Params, RenkoEngine, brick_size_from_atr, round_to_tick
from risk import (check_correlation_allowed, check_entry_allowed, compute_size, kill_switch_active,
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


_DAYS_CACHE: dict[str, tuple[float, list]] = {}
_DAYS_CACHE_TTL = 3600.0  # 1 hour TTL for daily ATR candles
_BARS_CACHE: dict[str, tuple[float, list, str]] = {}
_BARS_CACHE_TTL = 55.0  # Cache full broker intraday warmups for 55s across scanner/watcher
_RECENT_ENTRIES: dict[str, float] = {}
ENTRY_DEBOUNCE_SECONDS = 120.0  # Prevent duplicate order submissions within 2 minutes
_PENDING_CONFIRM: dict[str, dict] = {}
_PENDING_RUNNERS: dict[str, dict] = {}
_HANDLED_RUNS: dict[str, int] = {}


def _oms_state_path() -> Path:
    return LIVE_TRADES.parent / "oms_state.json"


def _save_oms_state():
    try:
        p = _oms_state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "pending_confirm": _PENDING_CONFIRM,
            "pending_runners": _PENDING_RUNNERS,
            "handled_runs": _HANDLED_RUNS,
        }, indent=2))
    except Exception:
        pass


def _load_oms_state():
    try:
        p = _oms_state_path()
        if p.exists():
            d = json.loads(p.read_text())
            if isinstance(d.get("pending_confirm"), dict):
                _PENDING_CONFIRM.update(d["pending_confirm"])
            if isinstance(d.get("pending_runners"), dict):
                _PENDING_RUNNERS.update(d["pending_runners"])
            if isinstance(d.get("handled_runs"), dict):
                _HANDLED_RUNS.update({k: int(v) for k, v in d["handled_runs"].items()})
    except Exception:
        pass


_load_oms_state()


def warmup_engine(cap: Capital, cfg: dict, allow_synthetic: bool = True):
    """Fetch recent history to warm up DailyAtr and RenkoEngine without repainting."""
    orig_epic = cfg["epic"]
    res = cfg["resolution"]
    price_type = cfg.get("price", "mid")
    p = Params.from_dict(cfg["strategy"])

    end = datetime.now(timezone.utc)
    bars = []
    days = []

    now_ts = time.time()
    if orig_epic in _DAYS_CACHE and now_ts - _DAYS_CACHE[orig_epic][0] < _DAYS_CACHE_TTL:
        days = _DAYS_CACHE[orig_epic][1]

    cache_key = f"{orig_epic}:{res}"
    if cache_key in _BARS_CACHE and now_ts - _BARS_CACHE[cache_key][0] < _BARS_CACHE_TTL:
        _, cached_bars, cached_cand = _BARS_CACHE[cache_key]
        bars = cached_bars
        cfg["epic"] = cached_cand

    if not bars:
        for cand in resolve_epic_alias(orig_epic):
            try:
                b_try = cap.prices(cand, res, end - timedelta(days=5), end)
                if b_try:
                    bars = b_try
                    cfg["epic"] = cand
                    if not days:
                        try:
                            days = cap.prices(cand, "DAY", end - timedelta(days=35), end)
                            if days:
                                _DAYS_CACHE[orig_epic] = (now_ts, days)
                        except Exception:
                            pass
                    if len(b_try) >= 50:
                        _BARS_CACHE[cache_key] = (now_ts, b_try, cand)
                        try:
                            from backtest import data_path, save_bars
                            save_bars(data_path(orig_epic, res), b_try)
                            if days and len(days) >= 5:
                                save_bars(data_path(orig_epic, "DAY"), days)
                        except Exception:
                            pass
                    break
            except Exception:
                continue

    if not bars and allow_synthetic:
        try:
            from backtest import ensure_data
            bars, fallback_days = ensure_data(cfg)
            if not days:
                days = fallback_days
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


def broker_position(cap: Capital, epic: str, return_raw: bool = False):
    """Inspect open positions on Capital.com for this epic (including aliases)."""
    cands = set(resolve_epic_alias(epic))
    try:
        raw_pos = cap.positions()
    except Exception:
        raw_pos = []
    positions = [p for p in raw_pos if p.get("market", {}).get("epic") in cands]
    if not positions:
        return (0, [], None, 0.0, []) if return_raw else (0, [], None, 0.0)
    direction = positions[0].get("position", {}).get("direction", "BUY")
    pos_dir = 1 if direction == "BUY" else -1
    deal_ids = [p["position"]["dealId"] for p in positions]
    stop_level = positions[0]["position"].get("stopLevel")
    total_size = sum(float(p["position"].get("size", 0)) for p in positions)
    if return_raw:
        return pos_dir, deal_ids, stop_level, total_size, positions
    return pos_dir, deal_ids, stop_level, total_size


def _trail_positions(cap: Capital, positions: list[dict], pos_dir: int,
                     last_close: float | None, bs: float, cfg: dict) -> list[dict]:
    """Trail stops per position individually while preserving any existing profitLevel (TP)."""
    if pos_dir == 0 or not positions or last_close is None:
        return []
    epic = cfg["epic"]
    new_stop = safety_stop_level(pos_dir, last_close, bs, cfg)
    dual_mode = bool(cfg.get("strategy", {}).get("dual_order_mode", False))
    actions = []
    for p in positions:
        pos_info = p.get("position", {})
        did = pos_info.get("dealId")
        if not did:
            continue
        p_stop = pos_info.get("stopLevel")
        p_tp = pos_info.get("profitLevel")
        candidate_stop = new_stop
        # Live Runner Breakeven Ratchet: once USUAL (with TP) has exited and only Runner (no TP) remains
        if dual_mode and len(positions) == 1 and p_tp is None and bs > 0:
            entry_lvl = pos_info.get("level")
            if entry_lvl is not None:
                entry_f = float(entry_lvl)
                if (last_close - entry_f) * pos_dir >= bs:
                    be_stop = round_to_tick(entry_f, cfg["tick"])
                    candidate_stop = max(candidate_stop, be_stop) if pos_dir > 0 else min(candidate_stop, be_stop)
        if should_trail_stop(pos_dir, p_stop, candidate_stop, cfg["tick"]):
            try:
                try:
                    if p_tp is not None:
                        cap.update_stop(did, candidate_stop, profit_level=p_tp)
                    else:
                        cap.update_stop(did, candidate_stop)
                except TypeError:
                    cap.update_stop(did, candidate_stop)
                print(f"[{epic}] Trailed safety stop on {did} -> {candidate_stop}")
                actions.append({"epic": epic, "action": "TRAIL_STOP", "deal_id": did, "new_stop": candidate_stop})
            except Exception as e:
                print(f"[{epic}] Error trailing stop: {e}")
    return actions


def execute_decision(cap: Capital, cfg: dict, action: str, reverse: bool,
                     last_close: float, bs: float, deal_ids: list[str], size: float | None = None,
                     profit_level: float | None = None, note_tag: str = ""):
    epic = cfg["epic"]
    sz = size if size is not None else compute_size(cfg, bs)
    if action in ("EXIT", "COVER") or (reverse and deal_ids):
        for did in deal_ids:
            try:
                conf = cap.close_position(did)
                log_event("CLOSE", action, sz, conf.get("level"), None, did,
                          realized_pnl=conf.get("profit", 0.0), note=f"[{epic}] {conf.get('dealStatus', '')}")
                print(f"[{epic}] Closed deal {did}: status={conf.get('dealStatus')} level={conf.get('level')}")
            except Exception as e:
                print(f"[{epic}] Error closing deal {did}: {e}")
                log_event("ERROR", action, sz, None, None, did, note=f"[{epic}] {e}")
        if action in ("EXIT", "COVER"):
            return

    if action in ("BUY", "SELL"):
        side = 1 if action == "BUY" else -1
        stop = safety_stop_level(side, last_close, bs, cfg)
        conf = None
        last_err = None
        for cand in resolve_epic_alias(epic):
            try:
                try:
                    if profit_level is not None:
                        conf = cap.open_position(cand, action, sz, stop_level=stop, profit_level=profit_level)
                    else:
                        conf = cap.open_position(cand, action, sz, stop_level=stop)
                except TypeError:
                    conf = cap.open_position(cand, action, sz, stop_level=stop)
                cfg["epic"] = cand
                break
            except Exception as e:
                last_err = e
                continue
        if conf:
            did = conf.get("dealId", "")
            evt = "REVERSE_OPEN" if reverse else "OPEN"
            tag_str = f" {note_tag}" if note_tag else ""
            log_event(evt, action, sz, conf.get("level"), stop, did, note=f"[{epic}]{tag_str} {conf.get('dealStatus', '')}")
            print(f"[{epic}]{tag_str} Opened {action} {sz} {cfg['epic']}: dealId={did} status={conf.get('dealStatus')} "
                  f"level={conf.get('level')} stop={stop} tp={profit_level}")
        else:
            log_event("ERROR", action, sz, None, stop, note=f"[{epic}] Order failed: {last_err}")
            print(f"[{epic}] Failed to open {action}: {last_err}")


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


def _close_epic_positions(cap: Capital, epic: str, deal_ids: list[str], action: str,
                          total_sz: float, actions_taken: list[dict] | None = None) -> int:
    closed = 0
    for did in deal_ids:
        try:
            conf = cap.close_position(did)
            log_event("CLOSE", action or "REVERSE_CLOSE", total_sz, conf.get("level"), None, did,
                      realized_pnl=conf.get("profit", 0.0), note=f"[{epic}] {conf.get('dealStatus', '')}")
            print(f"[{epic}] Closed deal {did}: status={conf.get('dealStatus')} level={conf.get('level')}")
            if actions_taken is not None:
                actions_taken.append({"epic": epic, "action": action or "CLOSE", "deal_id": did})
            closed += 1
        except Exception as e:
            print(f"[{epic}] Error closing deal {did}: {e}")
            log_event("ERROR", action or "CLOSE", total_sz, None, None, did, note=f"[{epic}] {e}")
    return closed


def step_once(cap: Capital, cfg: dict):
    try:
        return _step_once_impl(cap, cfg)
    finally:
        _save_oms_state()


def _step_once_impl(cap: Capital, cfg: dict):
    eng, _, closed_bars, last_n, bs = warmup_engine(cap, cfg, allow_synthetic=False)
    if not closed_bars:
        print("No closed bars returned.")
        return
    epic = cfg["epic"]
    tick = cfg["tick"]
    pos_dir, deal_ids, current_stop, total_sz, epic_positions = broker_position(cap, epic, return_raw=True)
    mkt = {}
    for cand in resolve_epic_alias(epic):
        try:
            res = cap.market(cand)
            if res and res.get("snapshot"):
                mkt = res.get("snapshot", {})
                cfg["epic"] = cand
                epic = cand
                break
        except Exception:
            continue
    bid, ask = mkt.get("bid"), mkt.get("offer")

    _trail_positions(cap, epic_positions, pos_dir, eng.last_close, bs, cfg)

    if eng.p.one_trade_per_run and _HANDLED_RUNS.get(epic) == eng.run_id:
        eng.mark_run_traded()

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
        lc_px = round_to_tick(eng.last_close, tick) if eng.last_close is not None else None
        log_event("SCAN", st_label, 0.0, lc_px, None, "", note=status_note)

    max_bar_age = max(900, RES_MINUTES.get(cfg.get("resolution", "MINUTE"), 1) * 300)
    bar_age_sec = (datetime.now(timezone.utc) - closed_bars[-1]["t"]).total_seconds()
    if bar_age_sec > max_bar_age:
        return

    if epic in _PENDING_RUNNERS:
        runner = _PENDING_RUNNERS[epic]
        rev_cond = (
            (runner["side"] > 0 and (eng.red_run >= 1 or eng.st_dir != 1))
            or (runner["side"] < 0 and (eng.green_run >= 1 or eng.st_dir != -1))
            or d.action in ("EXIT", "COVER")
        )
        if rev_cond:
            _PENDING_RUNNERS.pop(epic, None)
            log_event("BLOCKED", runner["action"], runner["size"], round_to_tick(last_c, tick), None,
                      note=f"[{epic}] runner_+1b_unfilled (cancelled)")
        elif (runner["side"] > 0 and last_c >= runner["target_px"]) or (runner["side"] < 0 and last_c <= runner["target_px"]):
            _PENDING_RUNNERS.pop(epic, None)
            execute_decision(cap, cfg, runner["action"], False, eng.last_close, bs, [],
                             size=runner["size"], profit_level=None, note_tag="RUNNER +1B")

    if pos_dir != 0 and (d.action in ("EXIT", "COVER") or (d.reverse and d.action in ("BUY", "SELL"))):
        _close_epic_positions(cap, epic, deal_ids, d.action or "REVERSE_CLOSE", total_sz)
        pos_dir, deal_ids = 0, []
        d.reverse = False
        if d.action in ("EXIT", "COVER"):
            return

    confirm_minutes = int(cfg.get("strategy", {}).get("confirm_minutes", 0))
    if epic in _PENDING_CONFIRM:
        pc = _PENDING_CONFIRM[epic]
        still_ok = (
            (eng.green_run >= eng.p.green_buy and (not eng.p.use_st or eng.st_dir == 1) and eng.run_id == pc["run_id"])
            if pc["side"] > 0
            else (eng.red_run >= eng.p.red_sell and (not eng.p.use_st or eng.st_dir == -1) and eng.run_id == pc["run_id"])
        )
        if not still_ok:
            _PENDING_CONFIRM.pop(epic, None)
            _HANDLED_RUNS[epic] = pc["run_id"]
            log_event("BLOCKED", pc["action"], compute_size(cfg, bs), round_to_tick(last_c, tick), None,
                      note=f"[{epic}] 5m_confirm_failed")
            return
        if time.time() >= pc["t0_epoch"] + confirm_minutes * 60:
            _PENDING_CONFIRM.pop(epic, None)
            _HANDLED_RUNS[epic] = pc["run_id"]
            d.action = pc["action"]
            d.reverse = False
            d.trigger_brick_px = round_to_tick(last_c, tick)
        else:
            return
    elif d.action in ("BUY", "SELL") and confirm_minutes > 0 and pos_dir == 0:
        _PENDING_CONFIRM[epic] = {
            "side": 1 if d.action == "BUY" else -1,
            "action": d.action,
            "t0_epoch": time.time(),
            "run_id": eng.run_id,
        }
        lc_px = round_to_tick(eng.last_close, tick) if eng.last_close is not None else None
        log_event("CONFIRM_WAIT", d.action, 0.0, lc_px, None, "",
                  note=f"[{epic}] Waiting {confirm_minutes}m confirmation")
        return

    if not d.action:
        return

    sz = compute_size(cfg, bs)
    if d.action in ("BUY", "SELL"):
        side_val = 1 if d.action == "BUY" else (-1 if d.action == "SELL" else 0)
        ok, reason = check_entry_allowed(cfg, bid, ask, bs, trig_px=d.trigger_brick_px, side=side_val)
        if not ok:
            print(reason)
            px_log = bid if d.action == "SELL" else ask
            log_event("BLOCKED", d.action, sz, round_to_tick(px_log, tick) if px_log is not None else None, None, note=reason)
            return

    if cfg.get("mode", "semi-auto") == "semi-auto":
        _HANDLED_RUNS[epic] = eng.run_id
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
        _HANDLED_RUNS[epic] = eng.run_id
        dual_mode = bool(cfg.get("strategy", {}).get("dual_order_mode", False))
        if d.action in ("BUY", "SELL") and dual_mode:
            side_val = 1 if d.action == "BUY" else -1
            tp_bricks = float(cfg.get("strategy", {}).get("tp_bricks", 3.0))
            runner_offset = float(cfg.get("strategy", {}).get("runner_offset_bricks", 1.0))
            tp_lvl = round_to_tick(last_c + side_val * tp_bricks * bs, tick)
            execute_decision(cap, cfg, d.action, d.reverse, eng.last_close, bs, deal_ids, size=sz,
                             profit_level=tp_lvl, note_tag=f"USUAL {tp_bricks:g}B-TP")
            _PENDING_RUNNERS[epic] = {
                "side": side_val,
                "action": d.action,
                "target_px": round_to_tick(last_c + side_val * runner_offset * bs, tick),
                "size": sz,
                "run_id": eng.run_id,
            }
        else:
            execute_decision(cap, cfg, d.action, d.reverse, eng.last_close, bs, deal_ids, size=sz)


def match_position_to_epic(pos_epic: str, all_epics: list[str]) -> str:
    """Resolve a broker deal's epic to the canonical instrument name."""
    up = pos_epic.upper().replace("/", "")
    for e in all_epics:
        aliases = [a.upper().replace("/", "") for a in resolve_epic_alias(e)]
        if up in aliases:
            return e
    return pos_epic.upper()


def step_multi_market(cap: Capital, cfg: dict, epics: list[str] | None = None) -> dict:
    """
    Evaluate all instruments round-the-clock and execute orders / manage stops independently.
    Runs 24/7 in the background regardless of which chart is being viewed in the cockpit.
    """
    from scanner import ALL_SCAN_EPICS

    target_epics = epics or cfg.get("monitored_epics") or ALL_SCAN_EPICS

    # 1. Fetch all currently open positions from Capital.com in one call
    try:
        raw_pos = cap.positions()
    except Exception as e:
        print(f"Error fetching open positions from broker: {e}")
        raw_pos = []

    positions_by_epic: dict[str, list[dict]] = {}
    for p in raw_pos:
        pe = p.get("market", {}).get("epic", "")
        matched = match_position_to_epic(pe, target_epics)
        positions_by_epic.setdefault(matched, []).append(p)

    total_open_count = len(raw_pos)
    max_portfolio = int(cfg.get("risk", {}).get("max_portfolio_positions", 5))
    disabled_epics = {str(x).upper() for x in cfg.get("risk", {}).get("disabled_epics", [])}

    actions_taken: list[dict] = []
    active_ui_epic = cfg.get("epic", "GOLD").upper()

    for epic in target_epics:
        try:
            # Build isolated config for this specific instrument
            inst_cfg = copy.deepcopy(cfg)
            inst_cfg["epic"] = epic
            if epic in INSTRUMENT_SPECS:
                inst_cfg["tick"] = INSTRUMENT_SPECS[epic]["tick"]
                inst_cfg["strategy"]["fallback_size"] = INSTRUMENT_SPECS[epic]["fallback_size"]

            epic_positions = positions_by_epic.get(epic, [])
            if epic_positions:
                direction = epic_positions[0].get("position", {}).get("direction", "BUY")
                pos_dir = 1 if direction == "BUY" else -1
                deal_ids = [p["position"]["dealId"] for p in epic_positions]
                current_stop = epic_positions[0]["position"].get("stopLevel")
                total_sz = sum(float(p["position"].get("size", 0)) for p in epic_positions)
            else:
                pos_dir, deal_ids, current_stop, total_sz = 0, [], None, 0.0

            eng, _, closed_bars, last_n, bs = warmup_engine(cap, inst_cfg, allow_synthetic=False)
            if not closed_bars:
                continue

            tick = inst_cfg["tick"]
            # Trailing safety stop per position while preserving any broker profitLevel (TP)
            actions_taken.extend(_trail_positions(cap, epic_positions, pos_dir, eng.last_close, bs, inst_cfg))

            if eng.p.one_trade_per_run and _HANDLED_RUNS.get(epic) == eng.run_id:
                eng.mark_run_traded()

            # Strategy decision on the confirmed candle
            last_c = closed_bars[-1]["c"]
            d = eng.decide(pos_dir, last_n, bar_close=last_c, brick_size=bs)
            last_bar_t = closed_bars[-1]["t"].isoformat()
            st_label = "BULL" if eng.st_dir == 1 else "BEAR"

            # Heartbeat pulse log for the active market shown in the cockpit
            if epic == active_ui_epic:
                need_scan_log = False
                if not LIVE_TRADES.exists() or LIVE_TRADES.stat().st_size < 120:
                    need_scan_log = True
                elif last_n > 0:
                    need_scan_log = True
                if need_scan_log:
                    status_note = f"[{epic}] G:{eng.green_run} R:{eng.red_run} | ST:{st_label} | Brick:${bs:.2f} | Sig:{d.action or 'Waiting'}"
                    lc_px = round_to_tick(eng.last_close, tick) if eng.last_close is not None else None
                    log_event("SCAN", st_label, 0.0, lc_px, None, "", note=status_note)

            max_bar_age = max(900, RES_MINUTES.get(inst_cfg.get("resolution", "MINUTE"), 1) * 300)
            bar_age_sec = (datetime.now(timezone.utc) - closed_bars[-1]["t"]).total_seconds()
            if bar_age_sec > max_bar_age:
                continue

            is_paused = epic.upper() in disabled_epics

            # Handle +1B Continuation Runner Orders (Model E Order 2)
            if not is_paused and epic in _PENDING_RUNNERS:
                runner = _PENDING_RUNNERS[epic]
                rev_cond = (
                    (runner["side"] > 0 and (eng.red_run >= 1 or eng.st_dir != 1))
                    or (runner["side"] < 0 and (eng.green_run >= 1 or eng.st_dir != -1))
                    or d.action in ("EXIT", "COVER")
                )
                if rev_cond:
                    _PENDING_RUNNERS.pop(epic, None)
                    log_event("BLOCKED", runner["action"], runner["size"], round_to_tick(last_c, tick), None,
                              note=f"[{epic}] runner_+1b_unfilled (cancelled)")
                elif ((runner["side"] > 0 and last_c >= runner["target_px"]) or
                      (runner["side"] < 0 and last_c <= runner["target_px"])) and total_open_count < max_portfolio:
                    _PENDING_RUNNERS.pop(epic, None)
                    execute_decision(cap, inst_cfg, runner["action"], False, eng.last_close, bs, [],
                                     size=runner["size"], profit_level=None, note_tag="RUNNER +1B")
                    total_open_count += 1
                    actions_taken.append({"epic": epic, "action": f"{runner['action']}_RUNNER", "size": runner["size"]})

            # Handle EXIT / COVER of active positions
            if pos_dir != 0 and (d.action in ("EXIT", "COVER") or (d.reverse and d.action in ("BUY", "SELL"))):
                closed_cnt = _close_epic_positions(cap, epic, deal_ids, d.action or "REVERSE_CLOSE", total_sz, actions_taken)
                total_open_count = max(0, total_open_count - closed_cnt)
                pos_dir, deal_ids = 0, []
                d.reverse = False
                if d.action in ("EXIT", "COVER"):
                    continue

            if is_paused:
                _PENDING_CONFIRM.pop(epic, None)
                _PENDING_RUNNERS.pop(epic, None)
                continue

            # Handle Time Confirmation Window (Q3)
            confirm_minutes = int(inst_cfg.get("strategy", {}).get("confirm_minutes", 0))
            if epic in _PENDING_CONFIRM:
                pc = _PENDING_CONFIRM[epic]
                still_ok = (
                    (eng.green_run >= eng.p.green_buy and (not eng.p.use_st or eng.st_dir == 1) and eng.run_id == pc["run_id"])
                    if pc["side"] > 0
                    else (eng.red_run >= eng.p.red_sell and (not eng.p.use_st or eng.st_dir == -1) and eng.run_id == pc["run_id"])
                )
                if not still_ok:
                    _PENDING_CONFIRM.pop(epic, None)
                    _HANDLED_RUNS[epic] = pc["run_id"]
                    log_event("BLOCKED", pc["action"], compute_size(inst_cfg, bs), round_to_tick(last_c, tick), None,
                              note=f"[{epic}] 5m_confirm_failed")
                    continue
                if time.time() >= pc["t0_epoch"] + confirm_minutes * 60:
                    _PENDING_CONFIRM.pop(epic, None)
                    _HANDLED_RUNS[epic] = pc["run_id"]
                    d.action = pc["action"]
                    d.reverse = False
                    d.trigger_brick_px = round_to_tick(last_c, tick)
                else:
                    continue
            elif d.action in ("BUY", "SELL") and confirm_minutes > 0 and pos_dir == 0:
                corr_ok, corr_reason = check_correlation_allowed(epic, 1 if d.action == "BUY" else -1, raw_pos, inst_cfg)
                if not corr_ok:
                    print(f"[{epic}] Confirmation skipped: {corr_reason}")
                    log_event("BLOCKED", d.action, 0.0, d.trigger_brick_px, None, note=f"[{epic}] {corr_reason}")
                    continue
                _PENDING_CONFIRM[epic] = {
                    "side": 1 if d.action == "BUY" else -1,
                    "action": d.action,
                    "t0_epoch": time.time(),
                    "run_id": eng.run_id,
                }
                lc_px = round_to_tick(eng.last_close, tick) if eng.last_close is not None else None
                log_event("CONFIRM_WAIT", d.action, 0.0, lc_px, None, "",
                          note=f"[{epic}] Waiting {confirm_minutes}m confirmation")
                continue

            # Handle BUY / SELL Entries
            if d.action in ("BUY", "SELL"):
                # Avoid duplicate positions on the same market
                if pos_dir != 0 and not d.reverse:
                    continue

                # Debounce to prevent duplicate submissions while broker registers position
                now_epoch = time.time()
                if now_epoch - _RECENT_ENTRIES.get(epic, 0) < ENTRY_DEBOUNCE_SECONDS:
                    continue

                # Portfolio-level risk cap
                if pos_dir == 0 and total_open_count >= max_portfolio:
                    print(f"[{epic}] Entry blocked: portfolio max open positions reached ({total_open_count}/{max_portfolio})")
                    continue

                if pos_dir == 0:
                    corr_ok, corr_reason = check_correlation_allowed(epic, 1 if d.action == "BUY" else -1, raw_pos, inst_cfg)
                    if not corr_ok:
                        print(f"[{epic}] Entry blocked: {corr_reason}")
                        log_event("BLOCKED", d.action, 0.0, d.trigger_brick_px, None, note=f"[{epic}] {corr_reason}")
                        continue

                mkt = {}
                for cand in resolve_epic_alias(epic):
                    try:
                        res = cap.market(cand)
                        if res and res.get("snapshot"):
                            mkt = res.get("snapshot", {})
                            inst_cfg["epic"] = cand
                            break
                    except Exception:
                        continue
                bid, ask = mkt.get("bid"), mkt.get("offer")

                sz = compute_size(inst_cfg, bs)
                side_val = 1 if d.action == "BUY" else (-1 if d.action == "SELL" else 0)
                ok, reason = check_entry_allowed(inst_cfg, bid, ask, bs, trig_px=d.trigger_brick_px, side=side_val)
                if not ok:
                    print(f"[{epic}] Entry blocked: {reason}")
                    if d.action:
                        px_log = bid if d.action == "SELL" else ask
                        log_event("BLOCKED", d.action, sz, round_to_tick(px_log, tick) if px_log is not None else None,
                                  None, note=f"[{epic}] {reason}")
                    continue

                if inst_cfg.get("mode", "semi-auto") == "semi-auto":
                    _RECENT_ENTRIES[epic] = now_epoch
                    _HANDLED_RUNS[epic] = eng.run_id
                    side = 1 if d.action == "BUY" else (-1 if d.action == "SELL" else 0)
                    stop = safety_stop_level(side, eng.last_close, bs, inst_cfg) if side else None
                    ttl = int(inst_cfg.get("risk", {}).get("ticket_ttl_seconds", 180))
                    now_utc = datetime.now(timezone.utc)
                    ticket = {
                        "created_at": now_utc.isoformat(),
                        "expires_at": (now_utc + timedelta(seconds=ttl)).isoformat(),
                        "bar_time": last_bar_t,
                        "epic": epic,
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
                    print(f"\n=== [{epic}] SEMI-AUTO TICKET READY ===")
                    print(json.dumps(ticket, indent=2))
                    actions_taken.append({"epic": epic, "action": "TICKET_CREATED", "ticket": ticket})
                else:
                    # Full Auto execution
                    _RECENT_ENTRIES[epic] = now_epoch
                    _HANDLED_RUNS[epic] = eng.run_id
                    dual_mode = bool(inst_cfg.get("strategy", {}).get("dual_order_mode", False))
                    if dual_mode:
                        tp_bricks = float(inst_cfg.get("strategy", {}).get("tp_bricks", 3.0))
                        runner_offset = float(inst_cfg.get("strategy", {}).get("runner_offset_bricks", 1.0))
                        tp_lvl = round_to_tick(last_c + side_val * tp_bricks * bs, tick)
                        execute_decision(cap, inst_cfg, d.action, d.reverse, eng.last_close, bs, deal_ids, size=sz,
                                         profit_level=tp_lvl, note_tag=f"USUAL {tp_bricks:g}B-TP")
                        _PENDING_RUNNERS[epic] = {
                            "side": side_val,
                            "action": d.action,
                            "target_px": round_to_tick(last_c + side_val * runner_offset * bs, tick),
                            "size": sz,
                            "run_id": eng.run_id,
                        }
                    else:
                        execute_decision(cap, inst_cfg, d.action, d.reverse, eng.last_close, bs, deal_ids, size=sz)
                    total_open_count += 1
                    raw_pos.append({"market": {"epic": epic}, "position": {"direction": d.action}})
                    actions_taken.append({"epic": epic, "action": d.action, "size": sz})
        except Exception as inst_err:
            print(f"[{epic}] Background scan error: {inst_err}")
            continue

    _save_oms_state()
    return {
        "monitored": len(target_epics),
        "open_deals": total_open_count,
        "actions": actions_taken,
    }


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
    try:
        raw_pos = cap.positions()
    except Exception:
        raw_pos = []
    closed_cnt = 0
    for p in raw_pos:
        did = p.get("position", {}).get("dealId")
        sz = float(p.get("position", {}).get("size", 0.0))
        ep = p.get("market", {}).get("epic", "")
        if did:
            try:
                conf = cap.close_position(did)
                log_event("FLATTEN", "CLOSE", sz, conf.get("level"), None, did, note=f"[{ep}] KILL_SWITCH")
                print(f"Emergency closed {ep} ({did}): {conf.get('dealStatus')}")
                closed_cnt += 1
            except Exception as e:
                print(f"Error emergency closing {did}: {e}")
    print(f"STOP kill switch engaged. Closed {closed_cnt} account positions.")


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
