"""Multi-Market Strategy Scanner.
Continuously monitors 15 instruments (Metals, Energy, Forex, Indices)
evaluating Renko bricks, Supertrend direction, and institutional filter rules.
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from backtest import INSTRUMENT_SPECS, ensure_data, simulate
from risk import in_rollover_blackout, session_of
from capital_api import Capital

logger = logging.getLogger("renko.scanner")

ALL_SCAN_EPICS = [
    "GOLD", "SILVER",
    "OIL_CRUDE", "OIL_BRENT",
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF",
    "US500", "US100", "US30", "GER40", "UK100", "JP225",
]


def scan_single_instrument(epic: str, base_cfg: dict, cap: Optional[Capital] = None) -> Dict[str, Any]:
    """Evaluate one instrument against the Renko ATR + Supertrend strategy."""
    spec = INSTRUMENT_SPECS.get(epic.upper(), INSTRUMENT_SPECS["GOLD"])
    digits = spec.get("digits", 2)
    currency = spec.get("currency", "$")
    pip_factor = spec.get("pip_factor", 1.0)
    name = spec.get("name", epic)
    category = spec.get("category", "General")

    # Clone config with symbol-specific overrides
    cfg = copy.deepcopy(base_cfg)
    cfg["epic"] = epic
    cfg["tick"] = spec["tick"]
    cfg["strategy"]["fallback_size"] = spec["fallback_size"]

    # 1. Fetch / ensure data
    bars, days = ensure_data(cfg)
    if not bars:
        return {
            "epic": epic,
            "name": name,
            "category": category,
            "currency": currency,
            "digits": digits,
            "pip_factor": pip_factor,
            "price": spec["base_px"],
            "spread": spec["half_sp"] * 2,
            "brick_size": spec["fallback_size"],
            "green_run": 0,
            "red_run": 0,
            "st_dir": 0,
            "st_now": None,
            "signal": "NO DATA",
            "signal_type": "NEUTRAL",
            "filter_status": "WAITING_DATA",
            "veto": None,
            "bars_count": 0,
            "time": datetime.now(timezone.utc).isoformat(),
        }

    # 2. Run simulation / engine
    use_stop = bool(cfg.get("risk", {}).get("safety_stop_in_backtest", True))
    trades, eng, signals, filtered = simulate(bars, days, cfg, use_stop=use_stop)

    last_b = eng.bricks[-1] if eng.bricks else None
    cur_px = round(bars[-1]["c"], digits)
    cur_spread = round(spec["half_sp"] * 2, digits)

    # If broker connected, attempt quick live snapshot
    if cap:
        try:
            from bot import resolve_epic_alias
            candidates = resolve_epic_alias(epic)
        except Exception:
            candidates = [epic]
        for cand in candidates:
            try:
                res = cap.market(cand)
                if res and res.get("snapshot"):
                    sn = res["snapshot"]
                    b, o = sn.get("bid"), sn.get("offer")
                    if b is not None and o is not None:
                        cur_px = round((b + o) / 2, digits)
                        cur_spread = round(o - b, digits)
                        break
            except Exception:
                continue

    bs = round(last_b.brick_size, max(digits, 2)) if last_b else spec["fallback_size"]
    st_val = round(eng.st_now, digits) if eng.st_now is not None else None
    st_dir = eng.st_dir
    g_run = eng.green_run
    r_run = eng.red_run

    # 3. Strategy Signal Classification
    now_utc = datetime.now(timezone.utc)
    sess = session_of(now_utc)
    allowed_sessions = cfg.get("risk", {}).get("sessions", {})
    session_ok = allowed_sessions.get(sess, True)

    in_blackout = bool(cfg.get("risk", {}).get("rollover_blackout", True)) and in_rollover_blackout(now_utc)
    max_sp_frac = float(cfg.get("risk", {}).get("max_spread_frac_of_brick", 0.35))
    spread_veto = (max_sp_frac > 0 and bs > 0 and (cur_spread / bs) > max_sp_frac)

    veto_reason = None
    if in_blackout:
        veto_reason = "Rollover Blackout (20:45–22:15 UTC)"
    elif not session_ok:
        veto_reason = f"{sess.title()} Session Filtered"
    elif spread_veto:
        veto_reason = f"Spread Wide ({cur_spread} > {round(max_sp_frac*bs, digits)})"

    # Signal logic: Configured green_buy / red_sell brick threshold matching Supertrend
    green_buy = max(1, int(cfg.get("strategy", {}).get("green_buy", 2)))
    red_sell = max(1, int(cfg.get("strategy", {}).get("red_sell", 2)))
    if g_run == green_buy and st_dir == 1:
        signal_type = "BUY"
        signal_label = "🟢 BUY SIGNAL" if not veto_reason else "⚠️ BUY (VETOED)"
    elif r_run == red_sell and st_dir == -1:
        signal_type = "SELL"
        signal_label = "🔴 SELL SIGNAL" if not veto_reason else "⚠️ SELL (VETOED)"
    elif g_run > green_buy and st_dir == 1:
        signal_type = "HOLD_LONG"
        signal_label = f"LONG RUN (G:{g_run})"
    elif r_run > red_sell and st_dir == -1:
        signal_type = "HOLD_SHORT"
        signal_label = f"SHORT RUN (R:{r_run})"
    else:
        signal_type = "NEUTRAL"
        signal_label = f"Trend Shift ({'G:'+str(g_run) if g_run else 'R:'+str(r_run)})"

    filter_status = "READY" if not veto_reason else veto_reason

    return {
        "epic": epic,
        "name": name,
        "category": category,
        "currency": currency,
        "digits": digits,
        "pip_factor": pip_factor,
        "price": cur_px,
        "spread": cur_spread,
        "brick_size": bs,
        "green_run": g_run,
        "red_run": r_run,
        "st_dir": st_dir,
        "st_now": st_val,
        "signal": signal_label,
        "signal_type": signal_type,
        "filter_status": filter_status,
        "veto": veto_reason,
        "total_bricks": eng.brick_cnt,
        "time": now_utc.isoformat(),
    }


import threading
import time

_SCAN_CACHE: Dict[str, Any] = {}
_SCAN_CACHE_TIME: float = 0.0
_SCAN_LOCK = threading.Lock()
SCAN_TTL_SECONDS = 15.0


def scan_all_instruments(base_cfg: dict, cap: Optional[Capital] = None) -> Dict[str, Any]:
    """Perform a full scan over all 16 supported global instruments."""
    items = []
    buy_signals = 0
    sell_signals = 0

    for epic in ALL_SCAN_EPICS:
        try:
            row = scan_single_instrument(epic, base_cfg, cap=cap)
            if row["signal_type"] == "BUY" and not row["veto"]:
                buy_signals += 1
            elif row["signal_type"] == "SELL" and not row["veto"]:
                sell_signals += 1
            items.append(row)
        except Exception as e:
            logger.error("Error scanning %s: %s", epic, e)
            spec = INSTRUMENT_SPECS.get(epic.upper(), {})
            items.append({
                "epic": epic,
                "name": spec.get("name", epic),
                "category": spec.get("category", "General"),
                "currency": spec.get("currency", "$"),
                "digits": spec.get("digits", 2),
                "price": spec.get("base_px", 0.0),
                "spread": spec.get("half_sp", 0.0) * 2,
                "brick_size": spec.get("fallback_size", 1.0),
                "pip_factor": spec.get("pip_factor", 1.0),
                "signal": "ERROR",
                "signal_type": "NEUTRAL",
                "filter_status": str(e),
                "veto": None,
                "time": datetime.now(timezone.utc).isoformat(),
            })

    veto_count = sum(1 for r in items if r.get("veto"))
    ready_count = len(items) - veto_count

    return {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "total_instruments": len(items),
        "active_buy_signals": buy_signals,
        "active_sell_signals": sell_signals,
        "total_active_signals": buy_signals + sell_signals,
        "veto_count": veto_count,
        "ready_count": ready_count,
        "items": items,
    }


def get_cached_or_scan_all(base_cfg: dict, cap: Optional[Capital] = None, force: bool = False) -> Dict[str, Any]:
    """Return cached scan results if fresh, otherwise re-scan all instruments."""
    global _SCAN_CACHE, _SCAN_CACHE_TIME
    now = time.time()
    with _SCAN_LOCK:
        if not force and _SCAN_CACHE and (now - _SCAN_CACHE_TIME) < SCAN_TTL_SECONDS:
            return _SCAN_CACHE

        res = scan_all_instruments(base_cfg, cap=cap)
        _SCAN_CACHE = res
        _SCAN_CACHE_TIME = time.time()
        return res
