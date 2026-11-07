"""Renko ATR Strategy (Long + Short) -- exact Pine v6 port + optional Pro Trader filters.

Pure logic, no network. Both the backtester and live bot drive this module.

Pine reference -> Python:
  f_step()                      -> renko_step()
  Supertrend-on-bricks block    -> RenkoEngine._add_brick()
  STRATEGY LOGIC block          -> RenkoEngine.decide()
  barstate.isconfirmed          -> on_bar() is only ever called with CLOSED bars

Pro Trader filters (each optional, can be toggled ON/OFF to compare vs Raw Pine):
  1. one_trade_per_run          -> blocks re-entering the same brick run after a stop/flatten
  2. max_bricks_on_entry_bar    -> skips news spikes where a single candle prints > N bricks
  3. max_overshoot_bricks       -> skips if bar close is > k bricks worse than the trigger brick
  4. grid_anchor                -> snaps initial brick origin to a multiple of brick_size
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional


@dataclass
class Params:
    atr_len: int = 5
    atr_div: float = 5.0
    round_tick: bool = True
    fallback_size: float = 10.0
    use_hl: bool = True
    enable_long: bool = True
    green_buy: int = 2
    red_exit: int = 1
    enable_short: bool = True
    red_sell: int = 2
    green_cover: int = 1
    entry_mode: str = "hold"          # "hold" = While condition holds, "close" = On brick close only
    use_st: bool = True
    st_len: int = 1
    st_factor: float = 2.1
    exit_on_st: bool = False
    # --- Pro filters (OFF by default in pure Pine mode, configurable in config.json / UI) ---
    one_trade_per_run: bool = False
    max_bricks_on_entry_bar: int = 0  # 0 = disabled, e.g. 3 = skip if >3 bricks in 1 candle
    max_overshoot_bricks: float = 0.0 # 0.0 = disabled, e.g. 1.5 = skip if bar close overshoots trigger brick
    grid_anchor: bool = False         # snap initial last_close to nearest brick_size multiple

    @classmethod
    def from_dict(cls, d: dict) -> "Params":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def round_to_tick(x: float, tick: float) -> float:
    """Pine math.round_to_mintick: nearest multiple of the tick, ties away from zero."""
    q = (Decimal(str(x)) / Decimal(str(tick))).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return float(q * Decimal(str(tick)))


def brick_size_from_atr(atr: Optional[float], p: Params, tick: float) -> float:
    """BRICK SIZE = ATR(len) on the 23-hr timeframe / divisor."""
    if atr is None:
        size = p.fallback_size
    else:
        raw = atr / p.atr_div
        size = round_to_tick(raw, tick) if p.round_tick else raw
    return max(size, tick)


def wilder_atr(highs, lows, closes, length):
    """Pine ta.atr(length): RMA of true range, seeded with an SMA. Returns a list (None until ready)."""
    out = []
    prev_close = None
    rma = None
    trs = []
    for h, l, c in zip(highs, lows, closes):
        tr = h - l if prev_close is None else max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        if rma is None:
            if len(trs) >= length:
                rma = sum(trs[-length:]) / length
        else:
            rma = (rma * (length - 1) + tr) / length
        out.append(rma)
        prev_close = c
    return out


def renko_step(lc: float, tr: int, hh: float, ll: float, bs: float):
    """Pine f_step: continue = 1 brick move, reversal = 2 brick move.
    Returns (direction, number_of_bricks, start_level)."""
    up_move = hh - lc
    dn_move = lc - ll
    d, nn, st = 0, 0, None
    if tr == 0:
        if up_move >= bs and up_move >= dn_move:
            d, nn, st = 1, int(up_move // bs), lc
        elif dn_move >= bs:
            d, nn, st = -1, int(dn_move // bs), lc
    elif tr == 1:
        if up_move >= bs:
            d, nn, st = 1, int(up_move // bs), lc
        elif dn_move >= 2 * bs:
            d, nn, st = -1, int(dn_move // bs) - 1, lc - bs
    else:
        if dn_move >= bs:
            d, nn, st = -1, int(dn_move // bs), lc
        elif up_move >= 2 * bs:
            d, nn, st = 1, int(up_move // bs) - 1, lc + bs
    return d, nn, st


@dataclass
class Brick:
    index: int
    open: float
    close: float
    direction: int
    time: object
    st_value: Optional[float]
    st_dir: int
    brick_size: float = 0.0


@dataclass
class Decision:
    action: str                      # "BUY", "SELL", "EXIT", "COVER" or ""
    reverse: bool = False            # BUY that covers a short / SELL that exits a long
    filtered_reason: str = ""        # non-empty if a Pro filter vetoed an entry
    trigger_brick_px: Optional[float] = None


@dataclass
class RenkoEngine:
    p: Params
    last_close: Optional[float] = None
    trend: int = 0
    green_run: int = 0
    red_run: int = 0
    brick_cnt: int = 0
    run_id: int = 0
    last_traded_run_id: int = -1
    # supertrend-on-bricks state
    st_atr: Optional[float] = None
    st_upper: Optional[float] = None
    st_lower: Optional[float] = None
    p_close: Optional[float] = None
    st_sum: float = 0.0
    st_cnt: int = 0
    st_dir: int = 0
    st_now: Optional[float] = None
    bricks: list[Brick] = field(default_factory=list)
    max_stored: int = 5000

    def _add_brick(self, o: float, c2: float, d0: int, t, bs: float):
        p = self.p
        h, l = max(o, c2), min(o, c2)
        trv = (h - l) if self.p_close is None else max(h - l, abs(h - self.p_close), abs(l - self.p_close))
        self.st_cnt += 1
        if self.st_cnt <= p.st_len:
            self.st_sum += trv
            atr_v = self.st_sum / self.st_cnt
        else:
            atr_v = (self.st_atr * (p.st_len - 1) + trv) / p.st_len
        self.st_atr = atr_v
        mid = (o + c2) / 2
        ub = mid + p.st_factor * atr_v
        lb = mid - p.st_factor * atr_v
        if self.st_lower is None:
            self.st_lower, self.st_upper, self.st_dir = lb, ub, d0
        else:
            nl = lb if (lb > self.st_lower or self.p_close < self.st_lower) else self.st_lower
            nu = ub if (ub < self.st_upper or self.p_close > self.st_upper) else self.st_upper
            if self.st_dir == 1:
                self.st_dir = -1 if c2 < nl else 1
            else:
                self.st_dir = 1 if c2 > nu else -1
            self.st_lower, self.st_upper = nl, nu
        self.st_now = self.st_lower if self.st_dir == 1 else self.st_upper
        self.p_close = c2
        self.bricks.append(Brick(self.brick_cnt, o, c2, d0, t, self.st_now, self.st_dir, bs))
        self.brick_cnt += 1
        if len(self.bricks) > self.max_stored:
            self.bricks.pop(0)

    def on_bar(self, t, high: float, low: float, close: float, brick_size: float) -> int:
        """Feed ONE closed bar. Returns the number of new bricks it formed."""
        hi = high if self.p.use_hl else close
        lo = low if self.p.use_hl else close
        if self.last_close is None:
            if self.p.grid_anchor and brick_size > 0:
                self.last_close = round(close / brick_size) * brick_size
            else:
                self.last_close = close
            return 0
        d0, n0, s0 = renko_step(self.last_close, self.trend, hi, lo, brick_size)
        if n0 <= 0:
            return 0
        prev_trend = self.trend
        o = s0
        for _ in range(n0):
            c2 = o + d0 * brick_size
            self._add_brick(o, c2, d0, t, brick_size)
            o = c2
        self.last_close = o
        self.trend = d0
        if d0 != prev_trend:
            self.run_id += 1
        if d0 == 1:
            self.green_run += n0
            self.red_run = 0
        else:
            self.red_run += n0
            self.green_run = 0
        return n0

    def trigger_brick_price(self, side: int) -> Optional[float]:
        """Return the close price of the brick where the current run first reached green_buy / red_sell."""
        if not self.bricks or self.last_close is None:
            return None
        bs = self.bricks[-1].brick_size or 0.0
        if side > 0 and self.green_run >= self.p.green_buy:
            extra = self.green_run - self.p.green_buy
            return self.last_close - extra * bs
        if side < 0 and self.red_run >= self.p.red_sell:
            extra = self.red_run - self.p.red_sell
            return self.last_close + extra * bs
        return self.last_close

    def decide(self, position: int, new_bricks: int, bar_close: Optional[float] = None,
               brick_size: float = 0.0) -> Decision:
        """STRATEGY LOGIC: position is +1 long, -1 short, 0 flat. One order per bar."""
        p = self.p
        st_bull, st_bear = self.st_dir == 1, self.st_dir == -1
        entry_ok = p.entry_mode == "hold" or new_bricks > 0
        long_entry = p.enable_long and entry_ok and self.green_run >= p.green_buy and (not p.use_st or st_bull)
        short_entry = p.enable_short and entry_ok and self.red_run >= p.red_sell and (not p.use_st or st_bear)
        long_exit = self.red_run >= p.red_exit or (p.exit_on_st and st_bear)
        short_exit = self.green_run >= p.green_cover or (p.exit_on_st and st_bull)

        go_long = position <= 0 and long_entry
        go_short = (not go_long) and position >= 0 and short_entry

        if go_long or go_short:
            side = 1 if go_long else -1
            trig_px = self.trigger_brick_price(side)
            if p.one_trade_per_run and self.last_traded_run_id == self.run_id:
                if position < 0 and short_exit:
                    return Decision("COVER", filtered_reason="one_trade_per_run")
                if position > 0 and long_exit:
                    return Decision("EXIT", filtered_reason="one_trade_per_run")
                return Decision("", filtered_reason="one_trade_per_run")

            if p.max_bricks_on_entry_bar > 0 and new_bricks > p.max_bricks_on_entry_bar:
                if position < 0 and short_exit:
                    return Decision("COVER", filtered_reason=f"news_spike ({new_bricks} bricks)")
                if position > 0 and long_exit:
                    return Decision("EXIT", filtered_reason=f"news_spike ({new_bricks} bricks)")
                return Decision("", filtered_reason=f"news_spike ({new_bricks} bricks)")

            if p.max_overshoot_bricks > 0 and bar_close is not None and trig_px is not None and brick_size > 0:
                overshoot = (bar_close - trig_px) * side / brick_size
                if overshoot > p.max_overshoot_bricks:
                    if position < 0 and short_exit:
                        return Decision("COVER", filtered_reason=f"overshoot ({overshoot:.2f} bricks)")
                    if position > 0 and long_exit:
                        return Decision("EXIT", filtered_reason=f"overshoot ({overshoot:.2f} bricks)")
                    return Decision("", filtered_reason=f"overshoot ({overshoot:.2f} bricks)")

            if go_long:
                return Decision("BUY", reverse=position < 0, trigger_brick_px=trig_px)
            return Decision("SELL", reverse=position > 0, trigger_brick_px=trig_px)

        if position > 0 and long_exit:
            return Decision("EXIT")
        if position < 0 and short_exit:
            return Decision("COVER")
        return Decision("")

    def mark_run_traded(self):
        self.last_traded_run_id = self.run_id


class DailyAtr:
    """For an intraday bar, the ATR of the last COMPLETED daily (23-hr) candle before
    the daily candle that contains the bar -- i.e. no repainting, like the Pine [1]."""

    def __init__(self, day_starts, day_highs, day_lows, day_closes, length):
        self.starts = list(day_starts)
        self.atr = wilder_atr(day_highs, day_lows, day_closes, length)
        self._i = 0

    def at(self, t) -> Optional[float]:
        while self._i + 1 < len(self.starts) and self.starts[self._i + 1] <= t:
            self._i += 1
        if not self.starts or self.starts[self._i] > t:
            return None
        prev = self._i - 1
        return self.atr[prev] if prev >= 0 else None
