import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from renko import (DailyAtr, Params, RenkoEngine, brick_size_from_atr, renko_step,  # noqa: E402
                   round_to_tick, wilder_atr)
import backtest  # noqa: E402
import risk  # noqa: E402


class TestStep(unittest.TestCase):
    def test_first_brick_needs_one_size(self):
        self.assertEqual(renko_step(100, 0, 109.99, 100, 10), (0, 0, None))
        self.assertEqual(renko_step(100, 0, 125, 100, 10), (1, 2, 100))
        self.assertEqual(renko_step(100, 0, 100, 85, 10), (-1, 1, 100))

    def test_no_trend_up_wins_ties(self):
        self.assertEqual(renko_step(100, 0, 112, 88, 10), (1, 1, 100))

    def test_continuation_one_brick(self):
        self.assertEqual(renko_step(100, 1, 131, 95, 10), (1, 3, 100))
        self.assertEqual(renko_step(100, -1, 105, 79, 10), (-1, 2, 100))

    def test_reversal_needs_two_bricks(self):
        self.assertEqual(renko_step(100, 1, 100, 81, 10), (0, 0, None))
        self.assertEqual(renko_step(100, 1, 100, 80, 10), (-1, 1, 90))
        self.assertEqual(renko_step(100, 1, 100, 65, 10), (-1, 2, 90))
        self.assertEqual(renko_step(100, -1, 120, 100, 10), (1, 1, 110))

    def test_continuation_checked_before_reversal(self):
        self.assertEqual(renko_step(100, 1, 110, 80, 10), (1, 1, 100))


class TestHelpers(unittest.TestCase):
    def test_round_to_tick(self):
        self.assertEqual(round_to_tick(7.345, 0.01), 7.35)
        self.assertEqual(round_to_tick(7.344, 0.01), 7.34)

    def test_brick_size(self):
        p = Params()
        self.assertEqual(brick_size_from_atr(None, p, 0.01), 10.0)
        self.assertEqual(brick_size_from_atr(36.789, p, 0.01), 7.36)
        self.assertEqual(brick_size_from_atr(0.0001, p, 0.01), 0.01)

    def test_wilder_atr_seed_and_rma(self):
        a = wilder_atr([10, 12, 11], [8, 9, 9], [9, 11, 10], 2)
        self.assertIsNone(a[0])
        self.assertAlmostEqual(a[1], (2 + 3) / 2)
        self.assertAlmostEqual(a[2], (2.5 * 1 + 2) / 2)

    def test_daily_atr_uses_previous_day(self):
        t0 = datetime(2026, 11, 2, tzinfo=timezone.utc)
        days = [t0 + timedelta(days=i) for i in range(4)]
        d = DailyAtr(days, [10, 10, 10, 10], [0, 0, 0, 0], [5, 5, 5, 5], 2)
        self.assertIsNone(d.at(days[1] + timedelta(hours=3)))
        self.assertEqual(d.at(days[2] + timedelta(hours=3)), 10)


class TestEngine(unittest.TestCase):
    def feed(self, eng, closes, bs=10):
        out = []
        for i, c in enumerate(closes):
            out.append(eng.on_bar(i, c, c, c, bs))
        return out

    def test_bricks_and_runs(self):
        e = RenkoEngine(Params())
        self.feed(e, [100, 110, 120, 125])
        self.assertEqual([(b.open, b.close) for b in e.bricks], [(100, 110), (110, 120)])
        self.assertEqual((e.green_run, e.red_run, e.trend, e.last_close), (2, 0, 1, 120))
        self.feed(e, [100])
        self.assertEqual((e.bricks[-1].open, e.bricks[-1].close), (110, 100))
        self.assertEqual((e.green_run, e.red_run), (0, 1))

    def test_supertrend_on_bricks(self):
        e = RenkoEngine(Params(st_len=1, st_factor=2.1))
        self.feed(e, [100, 110, 120])
        self.assertEqual(e.st_dir, 1)
        self.assertAlmostEqual(e.st_lower, 94.0)
        self.feed(e, [100])
        self.assertEqual(e.st_dir, 1)
        self.feed(e, [90])
        self.assertEqual(e.st_dir, -1)

    def test_signals(self):
        e = RenkoEngine(Params())
        n = self.feed(e, [100, 110, 120])[-1]
        self.assertEqual(e.decide(0, n).action, "BUY")
        n = self.feed(e, [100])[-1]
        self.assertEqual(e.decide(1, n).action, "EXIT")
        n = self.feed(e, [90])[-1]
        d = e.decide(0, n)
        self.assertEqual(d.action, "SELL")
        n = self.feed(e, [110])[-1]
        self.assertEqual(e.decide(-1, n).action, "COVER")

    def test_reverse_flag(self):
        e = RenkoEngine(Params(use_st=False))
        n = self.feed(e, [100, 110, 120])[-1]
        d = e.decide(-1, n)
        self.assertEqual((d.action, d.reverse), ("BUY", True))

    def test_entry_mode_close_only(self):
        e = RenkoEngine(Params(entry_mode="close"))
        self.feed(e, [100, 110, 120])
        self.assertEqual(e.decide(0, 0).action, "")
        self.assertEqual(RenkoEngine(Params()).decide(0, 0).action, "")

    def test_pro_filters_one_trade_per_run_and_news_spike(self):
        e = RenkoEngine(Params(one_trade_per_run=True, max_bricks_on_entry_bar=3))
        n = self.feed(e, [100, 110, 120])[-1]
        d1 = e.decide(0, n, bar_close=120, brick_size=10)
        self.assertEqual(d1.action, "BUY")
        e.mark_run_traded()
        # Same run, stopped out (pos=0), next bar adds 1 more green brick -> blocked by one_trade_per_run
        n2 = self.feed(e, [130])[-1]
        d2 = e.decide(0, n2, bar_close=130, brick_size=10)
        self.assertEqual(d2.action, "")
        self.assertEqual(d2.filtered_reason, "one_trade_per_run")


class TestSimulate(unittest.TestCase):
    def test_round_trip(self):
        cfg = backtest.load_config()
        cfg["sizing_mode"] = "fixed"
        cfg["size"] = 1.0
        cfg["risk"]["safety_stop_in_backtest"] = False
        cfg["risk"]["rollover_blackout"] = False
        cfg["risk"]["sessions"] = {"asian": True, "london": True, "new_york": True}
        cfg["strategy"]["one_trade_per_run"] = False
        cfg["strategy"]["max_bricks_on_entry_bar"] = 0
        cfg["strategy"]["max_overshoot_bricks"] = 0.0
        cfg["strategy"]["grid_anchor"] = False
        t0 = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
        bars = []
        for i, c in enumerate([100, 110, 120, 100, 90, 110]):
            bars.append({"t": t0 + timedelta(minutes=i), "o": c, "h": c, "l": c, "c": c})
        trades, eng, sig, _ = backtest.simulate(bars, [], cfg, use_stop=False)
        self.assertEqual([s["action"] for s in sig], ["BUY", "EXIT", "SELL", "COVER"])
        self.assertEqual([(t["side"], t["gross"]) for t in trades], [("LONG", -20), ("SHORT", -20)])


class TestRisk(unittest.TestCase):
    def test_safety_stop_and_trailing(self):
        cfg = backtest.load_config()
        self.assertEqual(risk.safety_stop_level(1, 2700.0, 5.0, cfg), 2685.0)
        self.assertEqual(risk.safety_stop_level(-1, 2700.0, 5.0, cfg), 2715.0)
        self.assertTrue(risk.should_trail_stop(1, 2680.0, 2685.0, 0.01))
        self.assertFalse(risk.should_trail_stop(1, 2685.0, 2680.0, 0.01))
        self.assertTrue(risk.should_trail_stop(-1, 2720.0, 2715.0, 0.01))
        self.assertFalse(risk.should_trail_stop(-1, 2715.0, 2720.0, 0.01))

    def test_spread_and_rollover_filter(self):
        cfg = backtest.load_config()
        t_ok = datetime(2026, 11, 4, 14, 0, tzinfo=timezone.utc)
        ok, _ = risk.check_entry_allowed(cfg, 2700.0, 2700.5, 4.0, now=t_ok)
        self.assertTrue(ok)
        ok, msg = risk.check_entry_allowed(cfg, 2700.0, 2702.0, 4.0, now=t_ok)
        self.assertFalse(ok)
        self.assertIn("Spread", msg)
        t_roll = datetime(2026, 11, 4, 21, 10, tzinfo=timezone.utc)
        ok, msg = risk.check_entry_allowed(cfg, 2700.0, 2700.2, 4.0, now=t_roll)
        self.assertFalse(ok)
        self.assertIn("rollover", msg)


if __name__ == "__main__":
    unittest.main()
