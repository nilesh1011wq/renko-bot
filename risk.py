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


def compute_size(cfg: dict, brick_size: float) -> float:
    """Compute position size in oz/units: either fixed or risk_usd / (2 * brick_size)."""
    if cfg.get("sizing_mode", "fixed") == "risk_usd" and brick_size > 0:
        risk_usd = float(cfg.get("risk_per_trade_usd", 50.0))
        stop_bricks = max(2.0, float(cfg.get("risk", {}).get("safety_stop_bricks", 3.0)))
        raw = risk_usd / (stop_bricks * brick_size)
        return max(0.1, round(raw, 2))
    return float(cfg.get("size", 1.0))


def safety_stop_level(side: int, last_brick_close: float, brick_size: float, cfg: dict) -> float:
    """Calculate the broker-side trailing safety stop level (side: +1 LONG, -1 SHORT)."""
    k = cfg["risk"].get("safety_stop_bricks", 3)
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


def check_entry_allowed(cfg: dict, bid: float | None, ask: float | None, brick_size: float,
                        trades_csv: Path | None = None, now: datetime | None = None,
                        trig_px: float | None = None, side: int | None = None) -> tuple[bool, str]:
    """Check whether a new entry (BUY or SELL) is permitted by risk rules."""
    if kill_switch_active():
        return False, "BLOCK: Kill switch (STOP file) is active"

    r_cfg = cfg.get("risk", {})
    t_now = now or datetime.now(timezone.utc)

    if r_cfg.get("rollover_blackout") and in_rollover_blackout(t_now):
        return False, "BLOCK: Daily Gold rollover blackout window (20:45-22:15 UTC)"

    sessions = r_cfg.get("sessions", {"asian": True, "london": True, "new_york": True})
    sess = session_of(t_now)
    if not sessions.get(sess, True):
        return False, f"BLOCK: Session '{sess}' is disabled in config"

    count, pnl = today_stats(trades_csv, t_now)

    max_trades = r_cfg.get("max_trades_per_day", 0)
    if max_trades and max_trades > 0 and count >= max_trades:
        return False, f"BLOCK: Max trades per day reached ({count}/{max_trades})"

    max_loss = r_cfg.get("max_daily_loss", 0)
    if max_loss and max_loss > 0 and pnl <= -abs(max_loss):
        return False, f"BLOCK: Daily loss limit reached ({pnl:.2f} <= -{abs(max_loss):.2f})"

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
