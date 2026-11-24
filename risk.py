"""Risk guard & Gold-specific session/sizing helpers for the Renko ATR bot."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from renko import round_to_tick

ROOT = Path(__file__).parent
JOURNAL = ROOT / "journal"
STOP_FILE = ROOT / "STOP"


def kill_switch_active() -> bool:
    return STOP_FILE.exists()


def set_kill_switch(active: bool = True):
    if active:
        STOP_FILE.write_text("STOPPED AT " + datetime.now(timezone.utc).isoformat() + "\n")
    elif STOP_FILE.exists():
        STOP_FILE.unlink()


def session_of(t: datetime) -> str:
    """Classify UTC timestamp into Gold trading session: asian, london, or new_york."""
    h = t.hour
    if 0 <= h < 7:
        return "asian"
    if 7 <= h < 13:
        return "london"
    return "new_york"


def in_rollover_blackout(t: datetime) -> bool:
    """Gold CFD daily rollover & reopen spread spike window: 20:45 to 22:15 UTC."""
    hm = t.hour * 60 + t.minute
    return (20 * 60 + 45) <= hm <= (22 * 60 + 15)


def in_macro_blackout(t: datetime, epic: str | None = None) -> bool:
    """DST-aware Tier-1 US Macro Release Blackout in America/New_York time:
    - 08:15-09:00 ET (CPI / NFP / PPI / GDP)
    - 13:45-14:45 ET (FOMC Rate Decision & Press Conf)
    - Wed 10:15-10:45 ET (EIA Oil Inventory for Energy)
    """
    from zoneinfo import ZoneInfo
    et = (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    hm = et.hour * 60 + et.minute
    if (8 * 60 + 15) <= hm <= (8 * 60 + 50) or (13 * 60 + 45) <= hm <= (14 * 60 + 30):
        return True
    ep = str(epic or "").upper()
    if ep in ("OIL_CRUDE", "OIL_BRENT", "WTI", "BRENT") and et.weekday() == 2:
        if (10 * 60 + 15) <= hm <= (10 * 60 + 45):
            return True
    return False


# Reference USD value of 1 unit of quote currency for non-USD-quoted instruments
_DEFAULT_QUOTE_TO_USD = {
    "USDJPY": 1.0 / 155.0,
    "JP225": 1.0 / 155.0,
    "J225": 1.0 / 155.0,
    "JPN225": 1.0 / 155.0,
    "NIKKEI": 1.0 / 155.0,
    "NI225": 1.0 / 155.0,
    "USDCAD": 1.0 / 1.39,
    "USDCHF": 1.0 / 0.88,
    "GER40": 1.09,
    "DE40": 1.09,
    "DAX": 1.09,
    "UK100": 1.29,
    "FTSE": 1.29,
}

# Verified Capital.com minimum deal sizes (prevents 2.7x over-risking on high-priced assets like BTCUSD)
_MIN_DEAL_SIZE = {
    "BTCUSD": 0.001,
    "BTC/USD": 0.001,
    "BITCOIN": 0.001,
    "ETHUSD": 0.001,
    "ETH/USD": 0.001,
    "ETHEREUM": 0.001,
    "GOLD": 0.01,
    "XAUUSD": 0.01,
    "US100": 0.01,
    "US30": 0.01,
    "US500": 0.01,
    "GER40": 0.01,
    "DE40": 0.01,
    "UK100": 0.01,
    "FTSE": 0.01,
}


def quote_to_usd(epic: str | None, price: float | None = None) -> float:
    """Return USD value of 1.0 unit of the instrument's quote currency."""
    ep = str(epic or "").upper().replace("/", "")
    if ep in ("USDJPY", "USDCAD", "USDCHF"):
        if price is not None and float(price) > 0:
            return 1.0 / float(price)
        return _DEFAULT_QUOTE_TO_USD[ep]
    return _DEFAULT_QUOTE_TO_USD.get(ep, 1.0)


def compute_size(cfg: dict, brick_size: float, price: float | None = None) -> float:
    """Compute position size in units, converting non-USD quote currencies into USD risk."""
    if cfg.get("sizing_mode", "fixed") == "risk_usd" and brick_size > 0:
        risk_usd = float(cfg.get("risk_per_trade_usd", 50.0))
        stop_bricks = max(2.0, float(cfg.get("risk", {}).get("safety_stop_bricks", 3.0)))
        ep = str(cfg.get("epic") or "").upper().replace("/", "")
        q2u = quote_to_usd(ep, price=price)
        raw = risk_usd / (stop_bricks * brick_size * q2u)
        min_sz = _MIN_DEAL_SIZE.get(ep, 0.1)
        prec = 3 if (min_sz < 0.01 and raw < 1.0) else 2
        return max(min_sz, round(raw, prec))
    return float(cfg.get("size", 1.0))


def safety_stop_level(side: int, last_brick_close: float, brick_size: float, cfg: dict) -> float:
    """Calculate the broker-side trailing safety stop level (side: +1 LONG, -1 SHORT)."""
    k = max(2.0, float(cfg["risk"].get("safety_stop_bricks", 3)))
    tick = cfg.get("tick", 0.01)
    raw = last_brick_close - side * k * brick_size
    return round_to_tick(raw, tick)


def should_trail_stop(side: int, current_stop: float | None, new_stop: float, tick: float) -> bool:
    """Only move the stop in the favorable direction by at least 1 tick."""
    if current_stop is None:
        return True
    if side > 0:
        return new_stop >= current_stop + tick
    return new_stop <= current_stop - tick


def today_stats(trades_csv: Path | None = None, now: datetime | None = None) -> tuple[int, float]:
    """Return (trades_today_count, realized_pnl_today) from journal/live_trades.csv."""
    path = trades_csv or (JOURNAL / "live_trades.csv")
    if not path.exists():
        return 0, 0.0
    day_str = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    count = 0
    pnl = 0.0
    with path.open() as f:
        for row in csv.DictReader(f):
            t_str = row.get("time", "")
            if t_str.startswith(day_str):
                if row.get("event") in ("OPEN", "REVERSE_OPEN"):
                    count += 1
                try:
                    pnl += float(row.get("realized_pnl") or 0.0)
                except ValueError:
                    pass
    return count, round(pnl, 2)


def daily_loss_bypass_active(r_cfg: dict, now: datetime | None = None) -> bool:
    """Admin override for the daily-loss block. Valid only for the UTC day it was enabled
    (stored as risk.daily_loss_bypass_date = 'YYYY-MM-DD'), so it auto-expires at 00:00 UTC."""
    d = str(r_cfg.get("daily_loss_bypass_date") or "")
    if not d:
        return False
    return d == (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


_BROKER_PNL_CACHE: dict[str, tuple[float, float]] = {}


def fetch_broker_today_pnl(cap, now: datetime | None = None, ttl_sec: float = 60.0) -> float | None:
    """Fetch today's realized TRADE P&L directly from Capital.com (includes broker-side SL hits)."""
    if cap is None or not hasattr(cap, "_req"):
        return None
    import time as _time
    t_now = now or datetime.now(timezone.utc)
    day_str = t_now.strftime("%Y-%m-%d")
    cached = _BROKER_PNL_CACHE.get(day_str)
    now_ts = _time.time()
    if cached and (now_ts - cached[0]) < ttl_sec:
        return cached[1]
    try:
        tx = cap._req("GET", f"/history/transactions?from={day_str}T00:00:00&type=TRADE")
        rows = tx.get("transactions", []) if isinstance(tx, dict) else []
        total = 0.0
        for r in rows:
            if str(r.get("status", "")).upper() in ("PROCESSED", ""):
                try:
                    total += float(r.get("size") or 0.0)
                except (TypeError, ValueError):
                    pass
        total = round(total, 2)
        _BROKER_PNL_CACHE[day_str] = (now_ts, total)
        return total
    except Exception:
        return cached[1] if cached else None


def _is_crypto_epic(epic: str | None) -> bool:
    up = str(epic or "").upper().replace("/", "")
    return up in ("BTCUSD", "ETHUSD", "SOLUSD", "BITCOIN", "ETHEREUM", "SOLANA")


def check_session_and_schedule_allowed(cfg: dict, now: datetime | None = None) -> tuple[bool, str]:
    """Fast pre-check of kill switch, weekend close, blackout windows, and enabled sessions."""
    if kill_switch_active():
        return False, "BLOCK: Kill switch (STOP file) is active"
    r_cfg = cfg.get("risk", {})
    t_now = now or datetime.now(timezone.utc)
    epic = cfg.get("epic")

    # Block non-Crypto assets during weekend market closure when weekend_gate is active (live bot)
    if r_cfg.get("weekend_gate", False) and not _is_crypto_epic(epic):
        wd = t_now.weekday()
        if wd == 5 or (wd == 4 and t_now.hour >= 21) or (wd == 6 and t_now.hour < 22):
            return False, f"BLOCK: Weekend market closure for {epic or 'CFD'} (UTC)"

    if r_cfg.get("rollover_blackout") and in_rollover_blackout(t_now):
        return False, "BLOCK: Daily Gold rollover blackout window (20:45-22:15 UTC)"
    if r_cfg.get("macro_blackout", False) and in_macro_blackout(t_now, epic=epic):
        return False, "BLOCK: Tier-1 Macro News Blackout window (America/New_York DST-aware)"
    sessions = r_cfg.get("sessions", {"asian": True, "london": True, "new_york": True})
    sess = session_of(t_now)
    if not sessions.get(sess, True):
        return False, f"BLOCK: Session '{sess}' is disabled in config"
    if not sessions.get("asian", True) and t_now.hour >= 21:
        return False, "BLOCK: Post-NY close overnight dead zone (21:00-07:00 UTC) disabled with Asian session"
    return True, "PASS"


def check_entry_allowed(cfg: dict, bid: float | None, ask: float | None, brick_size: float,
                        trades_csv: Path | None = None, now: datetime | None = None,
                        trig_px: float | None = None, side: int | None = None,
                        broker_pnl: float | None = None) -> tuple[bool, str]:
    """Check whether a new entry (BUY or SELL) is permitted by risk rules."""
    t_now = now or datetime.now(timezone.utc)
    sched_ok, sched_reason = check_session_and_schedule_allowed(cfg, t_now)
    if not sched_ok:
        return False, sched_reason

    r_cfg = cfg.get("risk", {})
    count, csv_pnl = today_stats(trades_csv, t_now)
    pnl = min(csv_pnl, float(broker_pnl)) if broker_pnl is not None else csv_pnl

    max_trades = r_cfg.get("max_trades_per_day", 0)
    if max_trades and max_trades > 0 and count >= max_trades:
        return False, f"BLOCK: Max trades per day reached ({count}/{max_trades})"

    max_loss = r_cfg.get("max_daily_loss", 0)
    if (max_loss and max_loss > 0 and pnl <= -abs(max_loss)
            and not daily_loss_bypass_active(r_cfg, t_now)):
        return False, f"BLOCK: Daily loss limit reached ({pnl:.2f} <= -{abs(max_loss):.2f})"

    if bid is not None and ask is not None and bid > ask:
        return False, f"BLOCK: Inverted quote detected (bid={bid} > ask={ask})"

    max_spread_frac = r_cfg.get("max_spread_frac_of_brick", 0)
    if max_spread_frac and max_spread_frac > 0 and bid is not None and ask is not None and brick_size > 0:
        spread = ask - bid
        limit = max_spread_frac * brick_size
        if spread > limit:
            return False, f"BLOCK: Spread {spread:.2f} exceeds {max_spread_frac*100:.0f}% of brick ({limit:.2f})"

    max_slippage = float(r_cfg.get("max_slippage_bricks", 0.35))
    if max_slippage > 0 and trig_px is not None and side is not None and brick_size > 0:
        curr_px = ask if side > 0 else bid
        if curr_px is not None:
            slip_dist = (curr_px - trig_px) * side
            if slip_dist > max_slippage * brick_size:
                return False, f"BLOCK: Live slippage {slip_dist:.2f} ({slip_dist/brick_size:.2f} brk) exceeds limit ({max_slippage:.2f} brk)"

    return True, "PASS"


USD_POLARITY: dict[str, int] = {
    "GOLD": 1,
    "XAUUSD": 1,
    "SILVER": 1,
    "EURUSD": 1,
    "GBPUSD": 1,
    "AUDUSD": 1,
    "USDCAD": -1,
    "USDCHF": -1,
    "USDJPY": -1,
}

ASSET_CLUSTERS: dict[str, set[str]] = {
    "ENERGY": {"OIL_CRUDE", "OIL_BRENT"},
    "US_INDICES": {"US500", "US100", "US30"},
    "EU_INDICES": {"GER40", "UK100"},
    "CRYPTO": {"BTCUSD", "ETHUSD", "SOLUSD"},
}

_CORR_ALIAS_MAP: dict[str, str] = {
    "XAUUSD": "GOLD",
    "XAU/USD": "GOLD",
    "XAGUSD": "SILVER",
    "XAG/USD": "SILVER",
    "USCRUDE": "OIL_CRUDE",
    "WTI": "OIL_CRUDE",
    "BRENT": "OIL_BRENT",
    "UKOIL": "OIL_BRENT",
    "SP500": "US500",
    "US500": "US500",
    "NAS100": "US100",
    "USTEC": "US100",
    "DOW": "US30",
    "DJ30": "US30",
    "WS30": "US30",
    "DAX": "GER40",
    "DE40": "GER40",
    "FTSE": "UK100",
    "NIKKEI": "JP225",
    "J225": "JP225",
    "JPN225": "JP225",
    "NI225": "JP225",
    "BITCOIN": "BTCUSD",
    "BTC/USD": "BTCUSD",
    "ETHEREUM": "ETHUSD",
    "ETH/USD": "ETHUSD",
    "SOLANA": "SOLUSD",
    "SOL/USD": "SOLUSD",
}


def _canonical_corr_epic(epic: str) -> str:
    up = str(epic or "").upper().replace("/", "")
    return _CORR_ALIAS_MAP.get(up, up)


def check_correlation_allowed(epic: str, side: int, open_positions: list[dict],
                              cfg: dict) -> tuple[bool, str]:
    """Enforce cluster exclusivity and USD directional exposure caps across distinct epics."""
    if not open_positions or side not in (1, -1):
        return True, "PASS"

    target_canon = _canonical_corr_epic(epic)
    open_map: dict[str, int] = {}
    for p in open_positions:
        raw_ep = p.get("market", {}).get("epic", "") or p.get("epic", "")
        other_canon = _canonical_corr_epic(raw_ep)
        if not other_canon or other_canon == target_canon:
            continue  # Never block Model E 2nd leg (+1B Runner) on the same instrument
        direction = str(p.get("position", {}).get("direction", "") or p.get("direction", "")).upper()
        if direction in ("BUY", "LONG"):
            open_map[other_canon] = 1
        elif direction in ("SELL", "SHORT"):
            open_map[other_canon] = -1

    for cluster_name, members in ASSET_CLUSTERS.items():
        if target_canon in members:
            for other_ep, other_side in open_map.items():
                if other_ep in members and other_side == side:
                    return False, f"BLOCK: Correlated {cluster_name} position already open ({other_ep})"

    max_corr = int(cfg.get("risk", {}).get("max_correlated_positions", 2))
    if max_corr > 0 and target_canon in USD_POLARITY:
        target_usd_dir = side * USD_POLARITY[target_canon]
        same_usd_count = sum(
            1 for other_ep, other_side in open_map.items()
            if other_ep in USD_POLARITY and (other_side * USD_POLARITY[other_ep]) == target_usd_dir
        )
        if same_usd_count >= max_corr:
            return False, f"BLOCK: Correlated USD directional cap reached ({same_usd_count}/{max_corr})"

    return True, "PASS"
