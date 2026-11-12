"""Backtester & Sample Gold Data Generator for the Renko ATR strategy.

  python3 backtest.py sample                     # generate realistic 15-day Gold 1m + 45-day DAY candles
  python3 backtest.py fetch --days 30            # download MINUTE + DAY candles from Capital.com -> data/
  python3 backtest.py run                        # replay, stats, journal/backtest_trades.csv
  python3 backtest.py run --no-stop              # pure Pine logic (no safety stop)
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from renko import DailyAtr, Params, RenkoEngine, brick_size_from_atr
from risk import compute_size, in_rollover_blackout, session_of

ROOT = Path(__file__).parent
DATA = ROOT / "data"
JOURNAL = ROOT / "journal"


def load_config():
    return json.loads((ROOT / "config.json").read_text())


def save_config(cfg: dict):
    (ROOT / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")


def data_path(epic, res):
    return DATA / ("%s_%s.json" % (epic, res))


def save_bars(path, bars):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [dict(r, t=r["t"].isoformat()) for r in bars]
    path.write_text(json.dumps(rows))


def load_bars(path):
    rows = json.loads(path.read_text())
    for r in rows:
        r["t"] = datetime.fromisoformat(r["t"])
    return rows


INSTRUMENT_SPECS = {
    "GOLD": {"name": "Gold / USD", "category": "Metals", "currency": "$", "base_px": 2640.0, "tick": 0.01, "digits": 2, "scale": 1.0, "half_sp": 0.14, "fallback_size": 3.50, "pip_factor": 1.0},
    "XAUUSD": {"name": "Gold / USD", "category": "Metals", "currency": "$", "base_px": 2640.0, "tick": 0.01, "digits": 2, "scale": 1.0, "half_sp": 0.14, "fallback_size": 3.50, "pip_factor": 1.0},
    "SILVER": {"name": "Silver / USD", "category": "Metals", "currency": "$", "base_px": 31.50, "tick": 0.001, "digits": 3, "scale": 31.5 / 2640.0, "half_sp": 0.01, "fallback_size": 0.08, "pip_factor": 1.0},
    "OIL_CRUDE": {"name": "Crude Oil (WTI)", "category": "Energy", "currency": "$", "base_px": 72.00, "tick": 0.01, "digits": 2, "scale": 72.0 / 2640.0, "half_sp": 0.03, "fallback_size": 0.20, "pip_factor": 1.0},
    "OIL_BRENT": {"name": "Brent Crude Oil", "category": "Energy", "currency": "$", "base_px": 76.00, "tick": 0.01, "digits": 2, "scale": 76.0 / 2640.0, "half_sp": 0.03, "fallback_size": 0.20, "pip_factor": 1.0},
    "EURUSD": {"name": "EUR / USD", "category": "Forex", "currency": "", "base_px": 1.0850, "tick": 0.0001, "digits": 4, "scale": 1.085 / 2640.0, "half_sp": 0.0001, "fallback_size": 0.0008, "pip_factor": 10000.0},
    "GBPUSD": {"name": "GBP / USD", "category": "Forex", "currency": "", "base_px": 1.2950, "tick": 0.0001, "digits": 4, "scale": 1.295 / 2640.0, "half_sp": 0.00012, "fallback_size": 0.0010, "pip_factor": 10000.0},
    "USDJPY": {"name": "USD / JPY", "category": "Forex", "currency": "", "base_px": 153.00, "tick": 0.01, "digits": 2, "scale": 153.0 / 2640.0, "half_sp": 0.015, "fallback_size": 0.15, "pip_factor": 100.0},
    "AUDUSD": {"name": "AUD / USD", "category": "Forex", "currency": "", "base_px": 0.6650, "tick": 0.0001, "digits": 4, "scale": 0.665 / 2640.0, "half_sp": 0.0001, "fallback_size": 0.0006, "pip_factor": 10000.0},
    "USDCAD": {"name": "USD / CAD", "category": "Forex", "currency": "", "base_px": 1.3850, "tick": 0.0001, "digits": 4, "scale": 1.385 / 2640.0, "half_sp": 0.0001, "fallback_size": 0.0007, "pip_factor": 10000.0},
    "USDCHF": {"name": "USD / CHF", "category": "Forex", "currency": "", "base_px": 0.8850, "tick": 0.0001, "digits": 4, "scale": 0.885 / 2640.0, "half_sp": 0.0001, "fallback_size": 0.0007, "pip_factor": 10000.0},
    "US500": {"name": "S&P 500 Index", "category": "Indices", "currency": "$", "base_px": 5850.0, "tick": 0.1, "digits": 1, "scale": 5850.0 / 2640.0, "half_sp": 0.35, "fallback_size": 5.0, "pip_factor": 1.0},
    "US100": {"name": "Nasdaq 100 Index", "category": "Indices", "currency": "$", "base_px": 20800.0, "tick": 0.5, "digits": 1, "scale": 20800.0 / 2640.0, "half_sp": 1.2, "fallback_size": 25.0, "pip_factor": 1.0},
    "US30": {"name": "Dow Jones 30", "category": "Indices", "currency": "$", "base_px": 43500.0, "tick": 1.0, "digits": 1, "scale": 43500.0 / 2640.0, "half_sp": 2.0, "fallback_size": 35.0, "pip_factor": 1.0},
    "GER40": {"name": "DAX 40 Index", "category": "Indices", "currency": "", "base_px": 19400.0, "tick": 1.0, "digits": 1, "scale": 19400.0 / 2640.0, "half_sp": 1.5, "fallback_size": 18.0, "pip_factor": 1.0},
    "UK100": {"name": "FTSE 100 Index", "category": "Indices", "currency": "", "base_px": 8200.0, "tick": 1.0, "digits": 1, "scale": 8200.0 / 2640.0, "half_sp": 1.0, "fallback_size": 10.0, "pip_factor": 1.0},
    "JP225": {"name": "Nikkei 225", "category": "Indices", "currency": "", "base_px": 39000.0, "tick": 5.0, "digits": 0, "scale": 39000.0 / 2640.0, "half_sp": 5.0, "fallback_size": 50.0, "pip_factor": 1.0},
}


def generate_sample_gold_data(epic="GOLD", res="MINUTE", days_intraday=12, seed=42):
    """Generate realistic 1-minute & daily candles across asset classes (Gold, Oil, Forex, Indices)."""
    spec = INSTRUMENT_SPECS.get(epic.upper(), INSTRUMENT_SPECS["GOLD"])
    scale = spec["scale"]
    px = spec["base_px"]
    digits = spec["digits"]
    base_sp = spec["half_sp"]

    rng = random.Random(seed)
    end_day = datetime(2026, 11, 6, 21, 0, tzinfo=timezone.utc)
    start_day = end_day - timedelta(days=45)

    # 1. Build 45 days of daily candles first (so ATR(5) is warm)
    day_bars = []
    cur = start_day.replace(hour=0, minute=0)
    intraday_start = end_day - timedelta(days=days_intraday)

    while cur < intraday_start:
        if cur.weekday() < 5:
            drift = rng.gauss(1.2 * scale, 14.0 * scale)
            hi = px + abs(rng.gauss(13.0 * scale, 5.0 * scale)) + max(0, drift)
            lo = px - abs(rng.gauss(13.0 * scale, 5.0 * scale)) + min(0, drift)
            cl = max(lo + 1.0 * scale, min(hi - 1.0 * scale, px + drift))
            day_bars.append({
                "t": cur, "o": round(px, digits), "h": round(hi, digits),
                "l": round(lo, digits), "c": round(cl, digits),
                "o_bid": round(px - base_sp, digits), "o_ask": round(px + base_sp, digits),
                "h_bid": round(hi - base_sp, digits), "h_ask": round(hi + base_sp, digits),
                "l_bid": round(lo - base_sp, digits), "l_ask": round(lo + base_sp, digits),
                "c_bid": round(cl - base_sp, digits), "c_ask": round(cl + base_sp, digits),
            })
            px = cl
        cur += timedelta(days=1)

    # 2. Build 1-minute candles for the last `days_intraday` days and aggregate matching DAY bars
    min_bars = []
    t = intraday_start.replace(hour=0, minute=0)
    regime_drift = 0.0
    regime_left = 0

    while t <= end_day:
        if t.weekday() >= 5 or t.hour == 21:  # weekend or 21:00-22:00 UTC rollover break
            t += timedelta(minutes=1)
            continue

        if regime_left <= 0:
            # Pick a regime: strong trend up, strong trend down, or mean-reverting range
            sess = session_of(t)
            if sess == "asian":
                regime_drift = rng.choice([-0.03, 0.0, 0.03]) * scale
                regime_left = rng.randint(60, 180)
            else:
                regime_drift = rng.choice([-0.16, -0.10, 0.0, 0.11, 0.18]) * scale
                regime_left = rng.randint(45, 160)
        regime_left -= 1

        sess = session_of(t)
        vol = (0.42 if sess == "asian" else (0.78 if sess == "london" else 0.95)) * scale

        # Occasional news candle at 12:30 UTC (US macro release)
        is_news = (t.hour == 12 and t.minute == 30 and rng.random() < 0.35)
        if is_news:
            step = rng.choice([-1, 1]) * rng.uniform(14.0 * scale, 24.0 * scale)
        else:
            step = regime_drift + rng.gauss(0, vol)

        o = px
        c = px + step
        wick_up = abs(rng.gauss(0, vol * 0.65))
        wick_dn = abs(rng.gauss(0, vol * 0.65))
        h = max(o, c) + wick_up
        l = min(o, c) - wick_dn

        # Spread: ~normal, ~5x in rollover window or news minute
        half_sp = (base_sp * 5.0) if (in_rollover_blackout(t) or is_news) else base_sp
        min_bars.append({
            "t": t,
            "o": round(o, digits), "h": round(h, digits), "l": round(l, digits), "c": round(c, digits),
            "o_bid": round(o - half_sp, digits), "o_ask": round(o + half_sp, digits),
            "h_bid": round(h - half_sp, digits), "h_ask": round(h + half_sp, digits),
            "l_bid": round(l - half_sp, digits), "l_ask": round(l + half_sp, digits),
            "c_bid": round(c - half_sp, digits), "c_ask": round(c + half_sp, digits),
        })
        px = c
        t += timedelta(minutes=1)

    # Aggregate intraday into daily bars by UTC date
    by_day = {}
    for b in min_bars:
        d0 = b["t"].replace(hour=0, minute=0, second=0, microsecond=0)
        if d0 not in by_day:
            by_day[d0] = dict(b, t=d0)
        else:
            cur_d = by_day[d0]
            if b["h"] > cur_d["h"]:
                cur_d["h"], cur_d["h_bid"], cur_d["h_ask"] = b["h"], b["h_bid"], b["h_ask"]
            if b["l"] < cur_d["l"]:
                cur_d["l"], cur_d["l_bid"], cur_d["l_ask"] = b["l"], b["l_bid"], b["l_ask"]
            cur_d["c"], cur_d["c_bid"], cur_d["c_ask"] = b["c"], b["c_bid"], b["c_ask"]

    for d0 in sorted(by_day):
        day_bars.append(by_day[d0])

    save_bars(data_path(epic, res), min_bars)
    save_bars(data_path(epic, "DAY"), day_bars)
    return min_bars, day_bars


def ensure_data(cfg):
    p_int = data_path(cfg["epic"], cfg["resolution"])
    p_day = data_path(cfg["epic"], "DAY")
    if not p_int.exists() or not p_day.exists():
        return generate_sample_gold_data(cfg["epic"], cfg["resolution"])
    return load_bars(p_int), load_bars(p_day)


def pick(r, k, price):
    if price == "mid":
        return r[k]
    v = r.get("%s_%s" % (k, price))
    return v if v is not None else r[k]


def simulate(bars, days, cfg, use_stop=True, start=None, end=None):
    p = Params.from_dict(cfg["strategy"])
    r_cfg = cfg.get("risk", {})
    tick, price = cfg["tick"], cfg.get("price", "mid")
    epic_upper = str(cfg.get("epic", "GOLD")).upper()
    spec = INSTRUMENT_SPECS.get(epic_upper, INSTRUMENT_SPECS.get("GOLD", {}))
    digits = spec.get("digits", 2)
    bs_digits = digits if digits > 0 else 0
    k_stop = float(r_cfg.get("safety_stop_bricks", 3.0))
    be_bricks = float(r_cfg.get("breakeven_after_bricks", 0.0))
    max_sp_frac = float(r_cfg.get("max_spread_frac_of_brick", 0.0))
    rollover_blk = bool(r_cfg.get("rollover_blackout", False))
    sess_allowed = r_cfg.get("sessions", {"asian": True, "london": True, "new_york": True})

    datr = DailyAtr([d["t"] for d in days], [pick(d, "h", price) for d in days],
                    [pick(d, "l", price) for d in days], [pick(d, "c", price) for d in days], p.atr_len)
    eng = RenkoEngine(p)
    pos, stop, trade, trades = 0, None, None, []
    signals = []
    filtered_events = []

    def open_trade(side, r, why, bs, trig_px):
        nonlocal trade
        px = pick(r, "c", price)
        exe = r.get("c_ask") if side > 0 else r.get("c_bid")
        exe_px = exe if exe is not None else px
        sz = compute_size(cfg, bs)
        trig = trig_px if trig_px is not None else px
        overshoot = round((px - trig) * side, digits)
        trade = {
            "side": "LONG" if side > 0 else "SHORT",
            "size": sz,
            "session": session_of(r["t"]),
            "entry_time": r["t"],
            "entry_brick_idx": eng.brick_cnt - 1,
            "trigger_px": round(trig, digits),
            "entry": round(px, digits),
            "entry_exec": round(exe_px, digits),
            "overshoot": overshoot,
            "brick_size": round(bs, bs_digits),
            "why_in": why,
            "mfe_px": 0.0,
            "mae_px": 0.0,
        }
        eng.mark_run_traded()

    def close_trade(r, px, why, exec_px=None):
        nonlocal trade
        side = 1 if trade["side"] == "LONG" else -1
        sz = trade["size"]
        bs = trade["brick_size"] or 1.0
        if exec_px is None:
            e = r.get("c_bid") if side > 0 else r.get("c_ask")
            exec_px = e if e is not None else px
        trade.update(
            exit_time=r["t"],
            exit_brick_idx=eng.brick_cnt - 1,
            exit=round(px, digits),
            exit_exec=round(exec_px, digits),
            why_out=why,
            mfe_bricks=round(trade["mfe_px"] / bs, 2),
            mae_bricks=round(trade["mae_px"] / bs, 2),
            gross=round((px - trade["entry"]) * side * sz, 2),
            net=round((exec_px - trade["entry_exec"]) * side * sz, 2),
        )
        trades.append(trade)
        trade = None

    for r in bars:
        t = r["t"]
        bs = brick_size_from_atr(datr.at(t), p, tick)
        live_window = (start is None or t >= start) and (end is None or t < end)
        h, l, c, o = (pick(r, k, price) for k in ("h", "l", "c", "o"))

        # Track MFE / MAE while trade is open
        if trade is not None and pos != 0:
            if pos > 0:
                trade["mfe_px"] = max(trade["mfe_px"], h - trade["entry"])
                trade["mae_px"] = max(trade["mae_px"], trade["entry"] - l)
            else:
                trade["mfe_px"] = max(trade["mfe_px"], trade["entry"] - l)
                trade["mae_px"] = max(trade["mae_px"], h - trade["entry"])

        # Safety stop resting at the broker
        if use_stop and pos != 0 and stop is not None:
            if pos > 0 and l <= stop:
                fill = min(stop, o)
                close_trade(r, fill, "SAFETY_STOP", fill)
                pos, stop = 0, None
            elif pos < 0 and h >= stop:
                fill = max(stop, o)
                close_trade(r, fill, "SAFETY_STOP", fill)
                pos, stop = 0, None

        n = eng.on_bar(t, h, l, c, bs)
        if not live_window:
            continue

        d = eng.decide(pos, n, bar_close=c, brick_size=bs)
        if d.filtered_reason:
            filtered_events.append({"t": t.isoformat(), "reason": d.filtered_reason, "c": c, "brick_idx": eng.brick_cnt - 1})

        # Check session / rollover / spread filters on new entries (BUY / SELL)
        if d.action in ("BUY", "SELL"):
            sess = session_of(t)
            bid_c, ask_c = r.get("c_bid"), r.get("c_ask")
            spread = (ask_c - bid_c) if (bid_c is not None and ask_c is not None) else 0.0
            veto = ""
            if rollover_blk and in_rollover_blackout(t):
                veto = "rollover_blackout"
            elif not sess_allowed.get(sess, True):
                veto = f"session_{sess}"
            elif max_sp_frac > 0 and bs > 0 and spread > max_sp_frac * bs:
                veto = f"spread ({spread:.{digits}f})"

            if veto:
                filtered_events.append({"t": t.isoformat(), "reason": veto, "c": c, "brick_idx": eng.brick_cnt - 1})
                # If we were reversing an open trade, still close the open trade!
                if pos > 0 and d.action == "SELL":
                    d.action = "EXIT"
                elif pos < 0 and d.action == "BUY":
                    d.action = "COVER"
                else:
                    d.action = ""

        if d.action:
            signals.append({"t": t.isoformat(), "action": d.action, "c": c, "bs": bs, "brick_idx": eng.brick_cnt - 1})

        if d.action == "BUY":
            if pos < 0:
                close_trade(r, c, "REVERSE")
            open_trade(1, r, "BUY", bs, d.trigger_brick_px)
            pos, stop = 1, eng.last_close - k_stop * bs
        elif d.action == "SELL":
            if pos > 0:
                close_trade(r, c, "REVERSE")
            open_trade(-1, r, "SELL", bs, d.trigger_brick_px)
            pos, stop = -1, eng.last_close + k_stop * bs
        elif d.action == "EXIT" and pos > 0:
            close_trade(r, c, "EXIT")
            pos, stop = 0, None
        elif d.action == "COVER" and pos < 0:
            close_trade(r, c, "COVER")
            pos, stop = 0, None

        # Trail the safety stop & optional breakeven lock as bricks form
        if pos > 0 and stop is not None and trade is not None:
            stop = max(stop, eng.last_close - k_stop * bs)
            if be_bricks > 0 and trade["mfe_px"] >= be_bricks * bs:
                stop = max(stop, trade["entry_exec"])
        elif pos < 0 and stop is not None and trade is not None:
            stop = min(stop, eng.last_close + k_stop * bs)
            if be_bricks > 0 and trade["mfe_px"] >= be_bricks * bs:
                stop = min(stop, trade["entry_exec"])

    if trade is not None:
        close_trade(bars[-1], pick(bars[-1], "c", price), "END_OF_DATA")
    return trades, eng, signals, filtered_events


def stats(trades, key):
    pnl = [t[key] for t in trades]
    if not pnl:
        return {"trades": 0, "win_rate_%": 0.0, "net": 0.0, "avg_trade": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0, "profit_factor": 0.0,
                "max_drawdown": 0.0, "longest_losing_streak": 0,
                "avg_mfe_bricks": 0.0, "avg_mae_bricks": 0.0, "avg_overshoot": 0.0}
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    eq = peak = mdd = 0.0
    streak = worst = 0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
        streak = streak + 1 if x <= 0 else 0
        worst = max(worst, streak)
    gl = -sum(losses)
    mfe = sum(t.get("mfe_bricks", 0.0) for t in trades) / len(trades)
    mae = sum(t.get("mae_bricks", 0.0) for t in trades) / len(trades)
    over = sum(t.get("overshoot", 0.0) for t in trades) / len(trades)
    return {
        "trades": len(pnl),
        "win_rate_%": round(100 * len(wins) / len(pnl), 1),
        "net": round(sum(pnl), 2),
        "avg_trade": round(sum(pnl) / len(pnl), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "profit_factor": round(sum(wins) / gl, 2) if gl > 0 else 99.0,
        "max_drawdown": round(mdd, 2),
        "longest_losing_streak": worst,
        "avg_mfe_bricks": round(mfe, 2),
        "avg_mae_bricks": round(mae, 2),
        "avg_overshoot": round(over, 2),
    }


def session_breakdown(trades, key="net"):
    out = {}
    for s in ("asian", "london", "new_york"):
        sub = [t for t in trades if t.get("session") == s]
        out[s] = stats(sub, key)
    return out


def write_trades(trades, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["side", "size", "session", "entry_time", "trigger_px", "entry", "entry_exec",
            "overshoot", "brick_size", "why_in", "exit_time", "exit", "exit_exec",
            "why_out", "mfe_bricks", "mae_bricks", "gross", "net"]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for t in trades:
            w.writerow({k: (t[k].isoformat() if hasattr(t.get(k), "isoformat") else t.get(k)) for k in cols})


def cmd_sample(a, cfg):
    min_bars, day_bars = generate_sample_gold_data(cfg["epic"], cfg["resolution"], days_intraday=a.days)
    print(f"Generated {len(min_bars)} sample 1m GOLD bars and {len(day_bars)} daily bars in data/.")


def cmd_fetch(a, cfg):
    from capital_api import Capital
    cap = Capital()
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=a.days)
    m = cap.market(cfg["epic"])
    inst = m.get("instrument", {})
    rules = m.get("dealingRules", {})
    print("Market:", inst.get("name"), "| epic", cfg["epic"], "| env", cap.env)
    print("Min size:", rules.get("minDealSize"), "| snapshot:", {k: m.get("snapshot", {}).get(k) for k in ("bid", "offer", "marketStatus")})
    bars = cap.prices(cfg["epic"], cfg["resolution"], start, end)
    days = cap.prices(cfg["epic"], "DAY", start - timedelta(days=35), end)
    save_bars(data_path(cfg["epic"], cfg["resolution"]), bars)
    save_bars(data_path(cfg["epic"], "DAY"), days)
    if bars:
        print("Saved %d %s bars (%s -> %s) and %d daily bars." % (
            len(bars), cfg["resolution"], bars[0]["t"], bars[-1]["t"], len(days)))
    else:
        print("No intraday bars returned.")


def cmd_run(a, cfg):
    bars, days = ensure_data(cfg)
    start = datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc) if a.start else None
    end = datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc) if a.end else None
    use_stop = cfg["risk"]["safety_stop_in_backtest"] and not a.no_stop
    trades, eng, _, filtered = simulate(bars, days, cfg, use_stop=use_stop, start=start, end=end)
    out = JOURNAL / "backtest_trades.csv"
    write_trades(trades, out)
    print("Data: %d %s bars %s -> %s | bricks formed: %d | last brick size %.2f" % (
        len(bars), cfg["resolution"], bars[0]["t"], bars[-1]["t"], eng.brick_cnt,
        abs(eng.bricks[-1].close - eng.bricks[-1].open) if eng.bricks else 0))
    print("Safety stop: %s | Filtered entries skipped: %d" % (
        "ON (%s bricks)" % cfg["risk"]["safety_stop_bricks"] if use_stop else "OFF (pure Pine logic)",
        len(filtered)))
    for key, label in (("gross", "Gross (TradingView-comparable)"), ("net", "Net of bid/ask spread")):
        print("\n%s:" % label)
        for k, v in stats(trades, key).items():
            print("  %-22s %s" % (k, v))
    print("\nSession breakdown (Net P&L):")
    for s, st in session_breakdown(trades, "net").items():
        print(f"  {s.upper():10s}: {st['trades']:3d} trades | win {st['win_rate_%']:5.1f}% | net ${st['net']:8.2f} | PF {st['profit_factor']}")
    print("\nTrades written to", out)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--days", type=int, default=12)
    f = sub.add_parser("fetch")
    f.add_argument("--days", type=int, default=30)
    r = sub.add_parser("run")
    r.add_argument("--from", dest="start")
    r.add_argument("--to", dest="end")
    r.add_argument("--no-stop", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    {"sample": cmd_sample, "fetch": cmd_fetch, "run": cmd_run}[a.cmd](a, cfg)


if __name__ == "__main__":
    main()
