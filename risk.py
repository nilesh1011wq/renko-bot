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
                        trades_csv: Path | None = None, now: datetime | None = None) -> tuple[bool, str]:
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

    return True, "PASS"
