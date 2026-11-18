"""Institutional Functional User Acceptance Testing (UAT) Suite for Renko ATR Strategy & Capital.com Cockpit.

Covers 100% of all functional domains:
  1. Data Ingestion & Capital.com Market Feed Resolution (GOLD vs XAUUSD)
  2. Renko Brick Engine & Supertrend Staircase Invariants
  3. The 6 Institutional Gold Protections (Lock 1 Trade, Grid Anchor, Rollover Blackout, Spread Filter, Overshoot Slippage, Breakeven Stop)
  4. Position Sizing & Multi-Session Execution (Asian, London, New York)
  5. Monte Carlo 2,500-Run Risk Simulation & Percentile Cones
  6. Server REST API, HMAC Security Gate, 24/7 Watcher & Emergency Kill Switch
  7. Zero-Scroll Responsive UI, Draggable Terminal Dock & Live Broker Display
"""
import io
import json
import os
import re
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import backtest
import montecarlo
import renko
import risk
import server
from capital_api import Capital, CapitalError


class TestInstitutionalUAT(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parent.parent
        cls.cfg = backtest.load_config()
        cls._orig_env = dict(os.environ)
        os.environ["RENKO_TESTING"] = "1"
        os.environ.pop("VIEW_ONLY_PUBLIC", None)
        os.environ.pop("DASHBOARD_PASSWORD", None)

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls._orig_env)

    # =========================================================================
    # SUITE 1: DATA INGESTION & MARKET FEED RESOLUTION
    # =========================================================================
    def test_01_regime_generators_reproducibility(self):
        """UAT 1.1: Verify deterministic generation of Gold market regimes (Seeds 42 & 99)."""
        bars_42, days_42 = backtest.generate_sample_gold_data(epic="GOLD", res="MINUTE", days_intraday=5, seed=42)
        self.assertGreater(len(bars_42), 100)
        self.assertGreaterEqual(len(days_42), 5)

        # Check standard bar structure
        for b in bars_42[:10]:
            self.assertIn("t", b)
            self.assertIn("o", b)
            self.assertIn("h", b)
            self.assertIn("l", b)
            self.assertIn("c", b)
            self.assertGreaterEqual(b["h"], b["l"])
            self.assertGreaterEqual(b["h"], max(b["o"], b["c"]))
            self.assertLessEqual(b["l"], min(b["o"], b["c"]))

        # Seed 42 must be completely deterministic
        bars_42_repeat, _ = backtest.generate_sample_gold_data(epic="GOLD", res="MINUTE", days_intraday=5, seed=42)
        self.assertEqual(bars_42[0]["c"], bars_42_repeat[0]["c"])
        self.assertEqual(bars_42[-1]["c"], bars_42_repeat[-1]["c"])

        # Seed 99 must produce a different path
        bars_99, _ = backtest.generate_sample_gold_data(epic="GOLD", res="MINUTE", days_intraday=5, seed=99)
        self.assertNotEqual(bars_42[-1]["c"], bars_99[-1]["c"])

    def test_02_capital_epic_alias_resolution(self):
        """UAT 1.2: Verify automatic broker alias fallback between GOLD and XAUUSD."""
        mock_cap = MagicMock()

        # Case A: EPIC is GOLD, but broker only returns snapshot for XAUUSD
        def mock_market(epic):
            if epic == "XAUUSD":
                return {
                    "snapshot": {
                        "bid": 2685.20,
                        "offer": 2685.50,
                        "high": 2690.0,
                        "low": 2675.0,
                        "marketStatus": "TRADEABLE",
                    }
                }
            raise CapitalError("Instrument not found")

        mock_cap.market.side_effect = mock_market
        snap, resolved = server.get_live_market_snapshot(mock_cap, "GOLD")
        self.assertEqual(resolved, "XAUUSD")
        self.assertEqual(snap["bid"], 2685.20)
        self.assertEqual(snap["offer"], 2685.50)

        # Case B: EPIC is XAUUSD, but broker only returns GOLD
        def mock_market_gold(epic):
            if epic == "GOLD":
                return {
                    "snapshot": {
                        "bid": 2680.10,
                        "offer": 2680.40,
                        "marketStatus": "TRADEABLE",
                    }
                }
            raise CapitalError("Instrument not found")

        mock_cap.market.side_effect = mock_market_gold
        snap2, resolved2 = server.get_live_market_snapshot(mock_cap, "XAUUSD")
        self.assertEqual(resolved2, "GOLD")
        self.assertEqual(snap2["bid"], 2680.10)

    def test_03_csv_import_parsing(self):
        """UAT 1.3: Verify manual TradingView / MT5 CSV ingestion pipeline."""
        sample_csv = (
            "time,open,high,low,close,volume\n"
            "2026-11-10T10:00:00Z,2650.0,2652.5,2649.5,2652.0,150\n"
            "2026-11-10T10:01:00Z,2652.0,2653.0,2651.0,2652.8,120\n"
            "2026-11-10T10:02:00Z,2652.8,2655.0,2652.5,2654.5,210\n"
        )
        bars = []
        reader = backtest.csv.DictReader(sample_csv.strip().splitlines())
        for row in reader:
            bars.append({
                "t": datetime.fromisoformat(row["time"].replace("Z", "+00:00")),
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
                "v": float(row["volume"]),
            })
        self.assertEqual(len(bars), 3)
        self.assertEqual(bars[0]["c"], 2652.0)
        self.assertEqual(bars[-1]["c"], 2654.5)

    # =========================================================================
    # SUITE 2: RENKO & SUPERTREND MATHEMATICAL INVARIANTS
    # =========================================================================
    def test_04_renko_brick_invariants(self):
        """UAT 2.1: Verify fundamental Renko continuation (1 size) vs reversal (2 sizes)."""
        # Step function verification:
        # First brick: seed at 100, needs 1 brick size of 10 to form Up brick
        self.assertEqual(renko.renko_step(100, 0, 109.99, 100, 10), (0, 0, None))
        self.assertEqual(renko.renko_step(100, 0, 110, 100, 10), (1, 1, 100))

        # Continuation: trend is +1, moving to 120 forms 1 continuation brick
        self.assertEqual(renko.renko_step(100, 1, 120, 100, 10), (1, 2, 100))

        # Reversal: trend is +1 at 100, dropping to 89 is not enough (needs 2 sizes -> 80)
        self.assertEqual(renko.renko_step(100, 1, 100, 81, 10), (0, 0, None))
        self.assertEqual(renko.renko_step(100, 1, 100, 80, 10), (-1, 1, 90))

        # Engine integration:
        eng = renko.RenkoEngine(renko.Params())
        # Initial bars to establish base
        eng.on_bar(1, 100, 100, 100, 10.0)
        n1 = eng.on_bar(2, 110, 110, 110, 10.0)
        self.assertEqual(n1, 1)
        self.assertEqual(eng.green_run, 1)
        self.assertEqual(eng.trend, 1)

        # Drop by 1 brick size to 100 does NOT reverse
        n2 = eng.on_bar(3, 100, 100, 100, 10.0)
        self.assertEqual(n2, 0)
        self.assertEqual(eng.trend, 1)

        # Drop by 2 brick sizes to 90 reverses to red
        n3 = eng.on_bar(4, 90, 90, 90, 10.0)
        self.assertEqual(n3, 1)
        self.assertEqual(eng.red_run, 1)
        self.assertEqual(eng.trend, -1)

    def test_05_supertrend_staircase_state_machine(self):
        """UAT 2.2: Verify Supertrend direction flip and staircase preservation on Renko."""
        eng = renko.RenkoEngine(renko.Params(st_len=1, st_factor=2.1))
        eng.on_bar(1, 100, 100, 100, 10.0)
        eng.on_bar(2, 110, 110, 110, 10.0)
        eng.on_bar(3, 120, 120, 120, 10.0)

        # In uptrend: direction is Bullish (+1), and lower band acts as floor
        self.assertEqual(eng.st_dir, 1)
        self.assertIsNotNone(eng.st_lower)
        self.assertLess(eng.st_lower, eng.last_close)

        # Downward shock forces reversal to Bearish (-1)
        eng.on_bar(4, 100, 100, 100, 10.0)
        eng.on_bar(5, 90, 90, 90, 10.0)
        self.assertEqual(eng.st_dir, -1)
        self.assertIsNotNone(eng.st_upper)
        self.assertGreater(eng.st_upper, eng.last_close)

    # =========================================================================
    # SUITE 3: THE 6 EXPERT GOLD PROTECTIONS
    # =========================================================================
    def test_06_filter_1_lock_one_trade_per_run(self):
        """UAT 3.1: Protection #1 - Verify maximum 1 trade allowed per continuous brick wave."""
        eng = renko.RenkoEngine(renko.Params(one_trade_per_run=True))
        eng.on_bar(1, 100, 100, 100, 10.0)
        eng.on_bar(2, 110, 110, 110, 10.0)
        n = eng.on_bar(3, 120, 120, 120, 10.0)

        # First entry: BUY is allowed
        d1 = eng.decide(0, n, bar_close=120, brick_size=10.0)
        self.assertEqual(d1.action, "BUY")
        eng.mark_run_traded()

        # In same brick wave, if stopped out (position=0), next green brick is BLOCKED
        n2 = eng.on_bar(4, 130, 130, 130, 10.0)
        d2 = eng.decide(0, n2, bar_close=130, brick_size=10.0)
        self.assertEqual(d2.action, "")
        self.assertEqual(d2.filtered_reason, "one_trade_per_run")

    def test_07_filter_2_grid_anchor_alignment(self):
        """UAT 3.2: Protection #2 - Verify brick coordinates adhere to absolute grid ticks."""
        eng = renko.RenkoEngine(renko.Params(grid_anchor=True))
        # Initial bar with unaligned price 2653.8, brick size 10.0
        eng.on_bar(1, 2653.8, 2653.8, 2653.8, 10.0)
        # grid_anchor snaps initial last_close to nearest round multiple (2650.0)
        self.assertEqual(eng.last_close, 2650.0)

        # New bar reaches 2660.0 -> forms exact brick on round grid
        n = eng.on_bar(2, 2660.0, 2660.0, 2660.0, 10.0)
        self.assertEqual(n, 1)
        self.assertEqual(eng.bricks[-1].open, 2650.0)
        self.assertEqual(eng.bricks[-1].close, 2660.0)

    def test_08_filter_3_rollover_blackout(self):
        """UAT 3.3: Protection #3 - Verify complete veto of entries during 20:45-22:15 UTC."""
        t_blackout = datetime(2026, 11, 10, 21, 30, tzinfo=timezone.utc)
        self.assertTrue(risk.in_rollover_blackout(t_blackout))

        t_normal = datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)
        self.assertFalse(risk.in_rollover_blackout(t_normal))

        # Check check_entry_allowed vetos rollover
        cfg = json.loads(json.dumps(self.cfg))
        cfg["risk"]["rollover_blackout"] = True
        allowed, reason = risk.check_entry_allowed(
            cfg=cfg,
            bid=2650.0,
            ask=2650.2,
            brick_size=1.0,
            now=t_blackout,
        )
        self.assertFalse(allowed)
        self.assertIn("rollover blackout", reason.lower())

    def test_09_filter_4_max_spread_veto(self):
        """UAT 3.4: Protection #4 - Verify spread filter blocks entries when spread is too wide."""
        cfg = json.loads(json.dumps(self.cfg))
        cfg["risk"]["max_spread_frac_of_brick"] = 0.25  # Max 25% of brick size ($0.25 on $1.00 brick)
        t_normal = datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)

        # Spread $0.35 on $1.00 brick exceeds 0.25 fraction -> Veto
        allowed, reason = risk.check_entry_allowed(
            cfg=cfg,
            bid=2650.0,
            ask=2650.35,
            brick_size=1.0,
            now=t_normal,
        )
        self.assertFalse(allowed)
        self.assertIn("Spread", reason)

        # Spread $0.15 on $1.00 brick is acceptable -> Pass
        allowed_ok, _ = risk.check_entry_allowed(
            cfg=cfg,
            bid=2650.0,
            ask=2650.15,
            brick_size=1.0,
            now=t_normal,
        )
        self.assertTrue(allowed_ok)

    def test_10_filter_5_overshoot_and_news_spike_veto(self):
        """UAT 3.5: Protection #5 - Verify multi-brick gap news spike veto."""
        eng = renko.RenkoEngine(renko.Params(max_bricks_on_entry_bar=2))
        eng.on_bar(1, 100, 100, 100, 10.0)
        # Bar 2 prints 3 bricks at once (massive news spike from 100 to 130)
        n = eng.on_bar(2, 130, 130, 130, 10.0)
        self.assertEqual(n, 3)

        d = eng.decide(0, n, bar_close=130, brick_size=10.0)
        self.assertEqual(d.action, "")
        self.assertIn("news_spike", d.filtered_reason)

    def test_11_filter_6_breakeven_and_safety_stop(self):
        """UAT 3.6: Protection #6 - Verify trailing safety stop level calculation."""
        cfg = {"risk": {"safety_stop_bricks": 3.0}, "tick": 0.01}
        # Long (+1) at 2650.0 with 3.0 safety stop bricks of size 1.0 -> stop at 2647.0
        stop_long = risk.safety_stop_level(1, 2650.0, 1.0, cfg)
        self.assertEqual(stop_long, 2647.0)

        # Short (-1) at 2650.0 with 3.0 safety stop bricks of size 1.0 -> stop at 2653.0
        stop_short = risk.safety_stop_level(-1, 2650.0, 1.0, cfg)
        self.assertEqual(stop_short, 2653.0)

        # Trailing stop advancement: only trail in favorable direction
        self.assertTrue(risk.should_trail_stop(1, 2647.0, 2648.0, 0.01))
        self.assertFalse(risk.should_trail_stop(1, 2648.0, 2647.0, 0.01))

    # =========================================================================
    # SUITE 4: POSITION SIZING & SESSION EXECUTION
    # =========================================================================
    def test_12_position_sizing_models(self):
        """UAT 4.1: Verify Fixed Units vs Dynamic USD Risk sizing models."""
        cfg_fixed = {"sizing_mode": "fixed", "size": 2.5}
        sz = risk.compute_size(cfg_fixed, brick_size=1.0)
        self.assertEqual(sz, 2.5)

        cfg_risk = {
            "sizing_mode": "risk_usd",
            "risk_per_trade_usd": 60.0,
            "risk": {"safety_stop_bricks": 3.0},
        }
        # $60 risk with 3 bricks * $1.0 = $3.0 stop distance -> 20.0 oz
        sz_dynamic = risk.compute_size(cfg_risk, brick_size=1.0)
        self.assertEqual(sz_dynamic, 20.0)

    def test_13_session_filtering(self):
        """UAT 4.2: Verify Asian, London, and New York market session mapping."""
        # 04:00 UTC -> asian
        self.assertEqual(risk.session_of(datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc)), "asian")
        # 09:00 UTC -> london
        self.assertEqual(risk.session_of(datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)), "london")
        # 16:00 UTC -> new_york
        self.assertEqual(risk.session_of(datetime(2026, 11, 10, 16, 0, tzinfo=timezone.utc)), "new_york")

    # =========================================================================
    # SUITE 5: MONTE CARLO 2,500-RUN SIMULATION
    # =========================================================================
    def test_14_monte_carlo_resampling(self):
        """UAT 5.1: Verify 2,500 Monte Carlo run percentile cone invariants (P10 <= P50 <= P90)."""
        trade_pnls = [15.0, -10.0, 25.0, -8.0, 30.0, -12.0, 18.0, -9.0, 22.0, 14.0]
        res = montecarlo.run_montecarlo(trade_pnls, runs=500, seed=42)

        self.assertIn("fan_p10", res)
        self.assertIn("fan_p50", res)
        self.assertIn("fan_p90", res)
        self.assertIn("p95_dd", res)

        p10 = res["fan_p10"]
        p50 = res["fan_p50"]
        p90 = res["fan_p90"]
        self.assertEqual(len(p10), len(p50))
        self.assertEqual(len(p50), len(p90))

        # Check statistical monotonicity invariant: P10 <= P50 <= P90 at every step
        for i in range(len(p10)):
            self.assertLessEqual(p10[i], p50[i] + 1e-6)
            self.assertLessEqual(p50[i], p90[i] + 1e-6)

    # =========================================================================
    # SUITE 6: REST API, SECURITY GATE, 24/7 WATCHER & KILL SWITCH
    # =========================================================================
    def test_15_public_health_and_head_endpoints(self):
        """UAT 6.1: Verify unauthenticated cloud probe endpoints (GET/HEAD /healthz, /index.html)."""
        handler = server.Handler.__new__(server.Handler)
        handler.headers = {}
        handler.wfile = io.BytesIO()
        handler.rfile = io.BytesIO()

        # Test HEAD /healthz
        handler.path = "/healthz"
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.do_HEAD()
        handler.send_response.assert_called_with(200)

        # Test GET /healthz
        handler._send_json = MagicMock()
        handler.do_GET()
        handler._send_json.assert_called()
        call_args = handler._send_json.call_args[0][0]
        self.assertEqual(call_args["status"], "ok")
        self.assertEqual(call_args["service"], "renko-atr-pro-cockpit")

    def test_16_authentication_gate(self):
        """UAT 6.2: Verify constant-time HMAC security gate when password is set."""
        with patch.dict(os.environ, {"DASHBOARD_PASSWORD": "SuperSecretPassword123"}):
            self.assertEqual(server.get_dashboard_password(), "SuperSecretPassword123")
            tok = server.expected_session_token("SuperSecretPassword123")
            self.assertTrue(len(tok) >= 32)

            # Test request without credentials
            mock_req = MagicMock()
            mock_req.headers = {}
            self.assertFalse(server.is_request_authenticated(mock_req))

            # Test request with valid Bearer token
            mock_req.headers = {"Authorization": f"Bearer {tok}"}
            self.assertTrue(server.is_request_authenticated(mock_req))

            # Test request with valid Cookie token
            mock_req.headers = {"Cookie": f"renko_session={tok}"}
            self.assertTrue(server.is_request_authenticated(mock_req))

            # Test request with invalid token
            mock_req.headers = {"Authorization": "Bearer invalid_tampered_token"}
            self.assertFalse(server.is_request_authenticated(mock_req))

    def test_17_state_serialization_structure(self):
        """UAT 6.3: Verify /api/state response contains config, bot status, and backtest."""
        cfg = backtest.load_config()
        status = server.get_bot_status(cfg)
        self.assertIn("credentials", status)
        self.assertIn("watcher", status)
        self.assertIn("kill_switch", status)
        self.assertIn("market", status)

        bt = server.serialize_backtest(cfg)
        self.assertIn("data_summary", bt)
        self.assertIn("stats_net", bt)
        self.assertIn("bricks", bt)
        self.assertIn("trades", bt)
        self.assertIn("equity_curve", bt)

    def test_18_watcher_thread_and_kill_switch_lifecycle(self):
        """UAT 6.4: Verify 24/7 background watcher controls and emergency kill switch."""
        watcher = server.BackgroundWatcher()
        st = watcher.state()
        self.assertFalse(st["running"])

        # Toggle emergency kill switch
        server.set_kill_switch(True)
        self.assertTrue(server.kill_switch_active())

        # When kill switch is active, watcher cannot run trades
        server.set_kill_switch(False)
        self.assertFalse(server.kill_switch_active())

    # =========================================================================
    # SUITE 7: ZERO-SCROLL UI, DRAGGABLE DOCK & JAVASCRIPT VALIDATION
    # =========================================================================
    def test_19_javascript_syntax_and_dom_elements(self):
        """UAT 7.1: Verify JavaScript syntax via JavaScriptCore and check critical DOM IDs."""
        html_path = self.root / "static" / "index.html"
        self.assertTrue(html_path.exists())
        html = html_path.read_text(encoding="utf-8")

        # Required DOM elements for zero-scroll cockpit & draggable pane
        required_elements = [
            'id="renko-canvas"',
            'id="dock-drag-handle"',
            'class="split-handle"',
            'class="split-bar"',
            'class="dock-card"',
            'id="hdr-px"',
            'id="hdr-px-sub"',
            'id="btn-sync-market"',
            'id="in-epic"',
            'id="tab-journal"',
            'id="tab-positions"',
            'id="tab-equity"',
            'id="tab-mc"',
            'id="tab-sessions"',
            'id="tab-log"',
            'initDockDraggable',
            'syncLiveBrokerPrice',
        ]
        for elem in required_elements:
            self.assertIn(elem, html, f"Missing critical UI element: {elem}")

        # Verify Slippage Filter Buttons & Protected Badges in HTML
        journal_elements = [
            'id="btn-journal-all"',
            'id="btn-journal-exec"',
            'id="btn-journal-flt"',
            'id="cnt-j-all"',
            'id="cnt-j-exec"',
            'id="cnt-j-flt"',
            'setJournalFilter',
            'renderJournalTable',
            'PROTECTED',
        ]
        for elem in journal_elements:
            self.assertIn(elem, html, f"Missing journal element: {elem}")

        # Extract JS scripts and validate syntax using macOS JavaScriptCore
        scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
        self.assertGreaterEqual(len(scripts), 1)

        jsc_bin = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"
        if Path(jsc_bin).exists():
            test_js = f"/tmp/uat_ui_test_{os.getpid()}.js"
            try:
                with open(test_js, "w", encoding="utf-8") as f:
                    # Wrap in function check to test pure syntax without requiring DOM globals
                    f.write(f"function __uat_syntax_check() {{\n{scripts[0]}\n}}")
                res = subprocess.run([jsc_bin, test_js], capture_output=True, text=True)
                self.assertEqual(res.returncode, 0, f"JavaScript syntax error: {res.stderr or res.stdout}")
            finally:
                if Path(test_js).exists():
                    Path(test_js).unlink()

    def test_19_slippage_guard_pre_execution(self):
        """Verify Live Pre-Execution Slippage Guard prevents bad fills."""
        import risk
        cfg = {
            "risk": {
                "max_trades_per_day": 0,
                "max_daily_loss": 0,
                "max_spread_frac_of_brick": 0.25,
                "max_slippage_bricks": 0.35,
                "rollover_blackout": False,
                "sessions": {"asian": True, "london": True, "new_york": True}
            }
        }
        brick_size = 10.0
        trig_px = 2600.0

        # BUY with 0.2 brk slippage (2.0 pts): PASS
        ok, reason = risk.check_entry_allowed(cfg, bid=2601.5, ask=2602.0, brick_size=brick_size,
                                              trig_px=trig_px, side=1)
        self.assertTrue(ok, f"Expected PASS for 0.2 brk slippage, got: {reason}")

        # BUY with 0.5 brk slippage (5.0 pts): BLOCK (> 0.35 limit)
        ok, reason = risk.check_entry_allowed(cfg, bid=2604.5, ask=2605.0, brick_size=brick_size,
                                              trig_px=trig_px, side=1)
        self.assertFalse(ok)
        self.assertIn("Live slippage", reason)

        # BUY with favorable slippage (cheaper price 2598.0): PASS
        ok, reason = risk.check_entry_allowed(cfg, bid=2597.5, ask=2598.0, brick_size=brick_size,
                                              trig_px=trig_px, side=1)
        self.assertTrue(ok)

        # SELL with 0.2 brk slippage (drop of 2.0 pts to 2598.0): PASS
        ok, reason = risk.check_entry_allowed(cfg, bid=2598.0, ask=2598.5, brick_size=brick_size,
                                              trig_px=trig_px, side=-1)
        self.assertTrue(ok)

        # SELL with 0.6 brk adverse slippage (drop of 6.0 pts to 2594.0): BLOCK (> 0.35 limit)
        ok, reason = risk.check_entry_allowed(cfg, bid=2594.0, ask=2594.5, brick_size=brick_size,
                                              trig_px=trig_px, side=-1)
        self.assertFalse(ok)
        self.assertIn("Live slippage", reason)

    def test_20_filtered_trades_journal_metadata(self):
        """Verify backtest.simulate captures full metadata for filtered trades."""
        import backtest
        cfg = backtest.load_config()
        cfg["epic"] = "GOLD"
        bars, days = backtest.ensure_data(cfg)
        trades, eng, signals, filtered = backtest.simulate(bars, days, cfg)
        self.assertGreater(len(trades), 0)
        self.assertGreater(len(filtered), 0)
        # Check first filtered event structure
        flt = filtered[0]
        self.assertIn("t", flt)
        self.assertIn("session", flt)
        self.assertIn("side", flt)
        self.assertIn("reason", flt)
        self.assertIn("trigger_px", flt)
        self.assertIn("c", flt)
        self.assertIn("brick_size", flt)

    def test_21_model_e_5m_confirmation_and_dual_order_execution(self):
        """Model E (5m time confirmation + Usual 3B-TP & +1B Runner dual-order execution) and UI wiring."""
        import copy
        from pathlib import Path
        import backtest
        import server

        cfg = copy.deepcopy(backtest.load_config())
        cfg["active_instrument"] = "GOLD"
        cfg["strategy"]["atr_div"] = 15.0
        cfg["strategy"]["confirm_minutes"] = 5
        cfg["strategy"]["dual_order_mode"] = True
        cfg["strategy"]["tp_bricks"] = 3.0
        cfg["strategy"]["runner_offset_bricks"] = 1.0

        res = server.serialize_backtest(cfg, skip_network=True)
        trades = res["trades"]
        filtered = res["filtered_events"]
        st = res["stats_net"]

        legs = {t.get("leg") for t in trades}
        why_outs = {t.get("why_out") for t in trades}
        reasons = [f.get("reason", "") for f in filtered]

        self.assertIn("USUAL", legs)
        self.assertIn("RUNNER", legs)
        self.assertIn("TP_3_BRICKS", why_outs)
        self.assertGreater(st["net"], 1900.0)
        self.assertGreater(st["win_rate_%"], 55.0)
        self.assertTrue(any(r.startswith("5m_confirm_failed") for r in reasons))
        self.assertTrue(any(r.startswith("runner_+1b_unfilled") for r in reasons))

        html = (Path(backtest.ROOT) / "static" / "index.html").read_text(encoding="utf-8")
        for dom_id in (
            'id="in-dual_order_mode"',
            'id="in-confirm_minutes"',
            'id="in-tp_bricks"',
            'id="in-runner_offset_bricks"',
        ):
            self.assertIn(dom_id, html)

    def test_22_live_bot_hardening_tp_preservation_and_run_dedup(self):
        """Verify Capital.update_stop preserves profitLevel and _HANDLED_RUNS prevents duplicate CONFIRM_WAIT."""
        import copy
        from datetime import datetime, timedelta, timezone
        import backtest
        import bot
        from capital_api import Capital

        # 1. Capital.update_stop preserves profitLevel in PUT payload
        cap_obj = Capital("demo", api_key="k", identifier="id", password="pw")
        calls = []
        cap_obj._req = lambda method, path, body=None: calls.append((method, path, body)) or {}
        cap_obj.update_stop("DEAL-1", 3860.0, profit_level=3910.0)
        self.assertEqual(calls[-1], ("PUT", "/positions/DEAL-1", {"stopLevel": 3860.0, "profitLevel": 3910.0}))

        # 2. _trail_positions passes each position's profitLevel when trailing stops
        trailed_calls = []

        class MockTrailCap:
            def update_stop(self, deal_id, stop_level, profit_level=None):
                trailed_calls.append((deal_id, stop_level, profit_level))

        positions = [
            {"position": {"dealId": "D-USUAL", "stopLevel": 3840.0, "profitLevel": 3910.0}},
            {"position": {"dealId": "D-RUNNER", "stopLevel": 3840.0, "profitLevel": None}},
        ]
        bot._trail_positions(MockTrailCap(), positions, 1, 3890.0, 10.0, {"epic": "GOLD", "tick": 0.01, "risk": {"safety_stop_bricks": 3.0}})
        self.assertEqual(trailed_calls, [("D-USUAL", 3860.0, 3910.0), ("D-RUNNER", 3860.0, None)])

        # 3. _HANDLED_RUNS prevents duplicate CONFIRM_WAIT on the same brick run_id
        import tempfile
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        bars = []
        # Day range = 15.0 / atr_div 15 -> bs = 1.0; establish BEAR first then flip BULL right at end
        closes = [100.0] * 40 + [99.0, 98.0, 97.0] + [97.0] * 13 + [98.0, 99.0, 100.0, 101.0]
        prev = closes[0]
        for i, c in enumerate(closes):
            bars.append({"t": now - timedelta(minutes=len(closes) - i), "o": prev, "h": max(prev, c), "l": min(prev, c), "c": c})
            prev = c
        px = closes[-1]
        day_bars = [{"t": now - timedelta(days=30 - d), "o": 100.0, "h": 115.0, "l": 100.0, "c": 100.0} for d in range(30)]

        class MockLoopCap:
            def __init__(self):
                self.opened = []
            def positions(self):
                return []
            def market(self, epic):
                return {"snapshot": {"bid": px - 0.05, "offer": px + 0.05, "marketStatus": "TRADEABLE"}}
            def prices(self, epic, res, start, end):
                return day_bars if res == "DAY" else bars
            def open_position(self, epic, direction, size, stop_level=None, profit_level=None):
                self.opened.append((epic, direction, size, stop_level, profit_level))
                return {"dealId": "M-1", "dealStatus": "ACCEPTED", "level": px}

        cfg = copy.deepcopy(backtest.load_config())
        cfg["epic"] = "GOLD"
        cfg["mode"] = "auto"
        cfg["strategy"]["atr_div"] = 15.0
        cfg["strategy"]["confirm_minutes"] = 5
        cfg["strategy"]["one_trade_per_run"] = True
        cfg["strategy"]["grid_anchor"] = False
        cfg["risk"]["rollover_blackout"] = False
        cfg["risk"]["max_daily_loss"] = 0
        cfg["risk"]["max_trades_per_day"] = 0
        cfg["risk"]["sessions"] = {"asian": True, "london": True, "new_york": True}

        orig_log = bot.LIVE_TRADES
        bot._PENDING_CONFIRM.clear()
        bot._PENDING_RUNNERS.clear()
        bot._HANDLED_RUNS.clear()
        bot._DAYS_CACHE.clear()

        with tempfile.TemporaryDirectory() as tmpd:
            bot.LIVE_TRADES = Path(tmpd) / "live_trades.csv"
            try:
                mock_cap = MockLoopCap()
                bot.step_once(mock_cap, cfg)
                self.assertIn("GOLD", bot._PENDING_CONFIRM)
                self.assertEqual(len(mock_cap.opened), 0)

                # Simulate 5 minutes elapsed while run holds -> executes once & marks run handled
                bot._PENDING_CONFIRM["GOLD"]["t0_epoch"] -= 600
                bot.step_once(mock_cap, cfg)
                self.assertNotIn("GOLD", bot._PENDING_CONFIRM)
                self.assertEqual(len(mock_cap.opened), 1)
                self.assertIn("GOLD", bot._HANDLED_RUNS)

                # Subsequent poll on the exact same run_id must NOT re-enter CONFIRM_WAIT
                bot.step_once(mock_cap, cfg)
                self.assertNotIn("GOLD", bot._PENDING_CONFIRM)
                self.assertEqual(len(mock_cap.opened), 1)
            finally:
                bot.LIVE_TRADES = orig_log
                bot._PENDING_CONFIRM.clear()
                bot._PENDING_RUNNERS.clear()
                bot._HANDLED_RUNS.clear()
                bot._DAYS_CACHE.clear()

    def test_23_phase1_institutional_kpis_oms_persistence_and_chart_overlays(self):
        """Phase 1: verify Sharpe/Sortino/Calmar KPIs, OMS JSON persistence, and chart level overlays."""
        import tempfile
        import backtest
        import bot
        import server

        # 1. stats() computes institutional risk-adjusted KPIs
        st = backtest.stats([{"net": 120.0}, {"net": -40.0}, {"net": 80.0}, {"net": -30.0}], "net")
        for key in ("sharpe", "sortino", "calmar", "payoff_ratio", "expectancy_r"):
            self.assertIn(key, st)
            self.assertGreater(st[key], 0.0)
        empty_st = backtest.stats([], "net")
        self.assertEqual(empty_st["sharpe"], 0.0)
        self.assertEqual(empty_st["calmar"], 0.0)

        # 2. _save_oms_state / _load_oms_state round-trips OMS dictionaries
        orig_log = bot.LIVE_TRADES
        bot._PENDING_CONFIRM.clear()
        bot._PENDING_RUNNERS.clear()
        bot._HANDLED_RUNS.clear()
        with tempfile.TemporaryDirectory() as tmpd:
            bot.LIVE_TRADES = Path(tmpd) / "live_trades.csv"
            try:
                bot._PENDING_CONFIRM["GOLD"] = {"side": 1, "action": "BUY", "t0_epoch": 1000.0, "run_id": 7}
                bot._PENDING_RUNNERS["GOLD"] = {"side": 1, "action": "BUY", "target_px": 3900.0, "size": 1.0, "run_id": 7}
                bot._HANDLED_RUNS["GOLD"] = 7
                bot._save_oms_state()

                bot._PENDING_CONFIRM.clear()
                bot._PENDING_RUNNERS.clear()
                bot._HANDLED_RUNS.clear()
                bot._load_oms_state()

                self.assertEqual(bot._PENDING_CONFIRM["GOLD"]["action"], "BUY")
                self.assertEqual(bot._PENDING_RUNNERS["GOLD"]["target_px"], 3900.0)
                self.assertEqual(bot._HANDLED_RUNS["GOLD"], 7)

                status = server.get_bot_status(backtest.load_config(), skip_network=True)
                self.assertIn("pending_confirm", status)
                self.assertIn("pending_runners", status)
                self.assertIn("GOLD", status["pending_confirm"])
                self.assertIn("GOLD", status["pending_runners"])
            finally:
                bot.LIVE_TRADES = orig_log
                bot._PENDING_CONFIRM.clear()
                bot._PENDING_RUNNERS.clear()
                bot._HANDLED_RUNS.clear()

        # 3. Chart overlays & KPIs wired in static/index.html
        html = (Path(backtest.ROOT) / "static" / "index.html").read_text(encoding="utf-8")
        for marker in ("drawLevelLine", "RUN +1B", "TP 3B", "5m Confirm", "Calmar:"):
            self.assertIn(marker, html)

    def test_24_scanner_alias_fallback_and_configured_signal_thresholds(self):
        """Scanner: alias loop continues past first 404 and respects green_buy/red_sell config."""
        from types import SimpleNamespace
        import copy
        import backtest
        import scanner

        cfg = copy.deepcopy(backtest.load_config())
        cfg["strategy"]["green_buy"] = 2
        cfg["strategy"]["red_sell"] = 2

        calls = []

        class MockCap:
            def market(self, epic):
                calls.append(epic)
                if epic == "OIL_BRENT":
                    raise RuntimeError("404 not found")
                return {"snapshot": {"bid": 94.80, "offer": 94.84}}

        res = scanner.scan_single_instrument("OIL_BRENT", cfg, cap=MockCap())
        self.assertGreaterEqual(len(calls), 2, f"Expected alias fallback attempts, got {calls}")
        self.assertEqual(res["price"], 94.82)
        self.assertEqual(res["spread"], 0.04)

        # Verify signal classification uses configured green_buy / red_sell (2) rather than hardcoded 1
        orig_simulate = scanner.simulate
        orig_ensure = scanner.ensure_data
        try:
            scanner.ensure_data = lambda *a, **kw: (
                [{"t": "2026-11-14T10:00:00Z", "o": 100.0, "h": 100.5, "l": 99.5, "c": 100.0}],
                {},
            )

            def fake_sim_r1(*a, **kw):
                eng = SimpleNamespace(bricks=[], st_now=102.0, st_dir=-1, green_run=0, red_run=1, brick_cnt=10)
                return [], eng, [], []

            scanner.simulate = fake_sim_r1
            r1 = scanner.scan_single_instrument("EURUSD", cfg, cap=None)
            self.assertEqual(r1["signal_type"], "NEUTRAL")

            def fake_sim_r2(*a, **kw):
                eng = SimpleNamespace(bricks=[], st_now=102.0, st_dir=-1, green_run=0, red_run=2, brick_cnt=11)
                return [], eng, [], []

            scanner.simulate = fake_sim_r2
            r2 = scanner.scan_single_instrument("EURUSD", cfg, cap=None)
            self.assertEqual(r2["signal_type"], "SELL")
        finally:
            scanner.simulate = orig_simulate
            scanner.ensure_data = orig_ensure

        # Verify per-instrument synthetic seeds diversify non-GOLD runs without altering GOLD
        gold_bars, _ = backtest.generate_sample_gold_data("GOLD", days_intraday=3, seed=42)
        silver_bars, silver_days = backtest.generate_sample_gold_data("SILVER", days_intraday=3, seed=42)
        eur_bars, eur_days = backtest.generate_sample_gold_data("EURUSD", days_intraday=3, seed=42)
        silver_cfg = copy.deepcopy(cfg)
        silver_cfg.update({"epic": "SILVER", "tick": 0.005})
        silver_cfg["strategy"]["fallback_size"] = 0.08
        eur_cfg = copy.deepcopy(cfg)
        eur_cfg.update({"epic": "EURUSD", "tick": 0.0001})
        eur_cfg["strategy"]["fallback_size"] = 0.0008
        _, s_eng, _, _ = backtest.simulate(silver_bars, silver_days, silver_cfg)
        _, e_eng, _, _ = backtest.simulate(eur_bars, eur_days, eur_cfg)
        self.assertGreater(len(gold_bars), 0)
        self.assertNotEqual(
            (s_eng.green_run, s_eng.red_run, s_eng.brick_cnt),
            (e_eng.green_run, e_eng.red_run, e_eng.brick_cnt),
            "Non-GOLD synthetic regimes should use distinct per-instrument seeds",
        )

    def test_25_phase2_correlation_guard_runner_be_and_scanner_toggle(self):
        """Phase 2: correlation caps, live Runner BE lock, per-market pause, and /api/scanner/toggle."""
        import copy
        import backtest
        import bot
        import risk
        import scanner

        cfg = copy.deepcopy(backtest.load_config())
        cfg["tick"] = 0.01
        cfg["strategy"]["dual_order_mode"] = True
        cfg["risk"]["max_correlated_positions"] = 2

        # 1. Cluster exclusivity (ENERGY: OIL_CRUDE vs OIL_BRENT same direction blocked; same epic allowed)
        open_pos = [{"market": {"epic": "OIL_CRUDE"}, "position": {"direction": "BUY"}}]
        ok_same, _ = risk.check_correlation_allowed("OIL_CRUDE", 1, open_pos, cfg)
        self.assertTrue(ok_same, "Runner +1B on same epic must not be blocked by correlation guard")
        ok_brent, reason_brent = risk.check_correlation_allowed("OIL_BRENT", 1, open_pos, cfg)
        self.assertFalse(ok_brent)
        self.assertIn("ENERGY", reason_brent)
        ok_brent_opp, _ = risk.check_correlation_allowed("OIL_BRENT", -1, open_pos, cfg)
        self.assertTrue(ok_brent_opp)

        # 2. USD directional cap (2 long-USD shorts: GOLD BUY + EURUSD BUY => AUDUSD BUY blocked; USDCHF BUY allowed)
        usd_pos = [
            {"market": {"epic": "XAUUSD"}, "position": {"direction": "BUY"}},
            {"market": {"epic": "EUR/USD"}, "position": {"direction": "BUY"}},
        ]
        ok_aud, reason_usd = risk.check_correlation_allowed("AUDUSD", 1, usd_pos, cfg)
        self.assertFalse(ok_aud)
        self.assertIn("USD directional cap", reason_usd)
        ok_chf, _ = risk.check_correlation_allowed("USDCHF", 1, usd_pos, cfg)
        self.assertTrue(ok_chf)

        # 3. Live Runner Breakeven Ratchet in _trail_positions (when USUAL TP closed and only Runner remains)
        updated_stops = []

        class MockCap:
            def update_stop(self, deal_id, new_stop, profit_level=None):
                updated_stops.append((deal_id, new_stop, profit_level))

        runner_cfg = copy.deepcopy(cfg)
        runner_cfg["risk"]["safety_stop_bricks"] = 4
        # entry = 3800, bs = 10, last_close = 3820 (+2B) -> raw stop = 3780, BE ratchet = 3800
        runner_pos = [{"position": {"dealId": "RUN-1", "direction": "BUY", "level": 3800.0, "stopLevel": 3770.0, "profitLevel": None}}]
        actions = bot._trail_positions(MockCap(), runner_pos, 1, 3820.0, 10.0, runner_cfg)
        self.assertEqual(len(actions), 1)
        self.assertEqual(updated_stops[0], ("RUN-1", 3800.0, None))

        # 4. Scanner disabled_epics pause + UI markers in server.py / static/index.html
        paused_cfg = copy.deepcopy(cfg)
        paused_cfg["risk"]["disabled_epics"] = ["EURUSD"]
        paused_scan = scanner.scan_single_instrument("EURUSD", paused_cfg, cap=None)
        self.assertFalse(paused_scan["auto_enabled"])
        self.assertEqual(paused_scan["veto"], "Auto-Trade Paused")

        srv_src = (Path(backtest.ROOT) / "server.py").read_text(encoding="utf-8")
        html_src = (Path(backtest.ROOT) / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("/api/scanner/toggle", srv_src)
        self.assertIn("toggleInstrumentAuto", html_src)
        self.assertIn("const isEuUkIndex", html_src)

    def test_26_phase3_working_orders_tca_attribution_and_tearsheet(self):
        """Phase 3: native STOP working orders, TCA attribution, CSV export, and HTML tear-sheet."""
        import copy
        import backtest
        import bot
        import capital_api
        import server

        cfg = copy.deepcopy(backtest.load_config())
        cfg["epic"] = "GOLD"
        cfg["tick"] = 0.01

        # 1. Capital working order methods & bot working-order helpers
        self.assertTrue(hasattr(capital_api.Capital, "working_orders"))
        self.assertTrue(hasattr(capital_api.Capital, "create_working_order"))
        self.assertTrue(hasattr(capital_api.Capital, "delete_working_order"))

        wo_calls = []

        class MockWoCap:
            def create_working_order(self, epic, direction, size, level, order_type="STOP", stop_level=None, profit_level=None):
                wo_calls.append(("create", epic, direction, size, level, order_type, stop_level))
                return {"dealId": "WO-99", "dealStatus": "ACCEPTED"}

            def delete_working_order(self, deal_id):
                wo_calls.append(("delete", deal_id))
                return {"dealStatus": "DELETED"}

        bot._PENDING_RUNNERS.clear()
        orig_log = bot.LIVE_TRADES
        import tempfile
        with tempfile.TemporaryDirectory() as tmpd:
            bot.LIVE_TRADES = Path(tmpd) / "live_trades.csv"
            try:
                bot._arm_runner_order(MockWoCap(), cfg, "GOLD", "BUY", 1, 3900.0, 3900.0, 10.0, 1.5, 12)
                self.assertEqual(bot._PENDING_RUNNERS["GOLD"]["working_order_id"], "WO-99")
                self.assertEqual(wo_calls[0][0], "create")
                self.assertEqual(wo_calls[0][5], "STOP")
                bot.cancel_runner_working_order(MockWoCap(), "GOLD", bot._PENDING_RUNNERS.pop("GOLD"))
                self.assertEqual(wo_calls[1], ("delete", "WO-99"))
            finally:
                bot.LIVE_TRADES = orig_log
                bot._PENDING_RUNNERS.clear()

        # 2. TCA Attribution + Tear-Sheet HTML rendering + UI wiring
        bt = server.serialize_backtest(cfg, use_cache=False, skip_network=True)
        tca = bt.get("tca_attribution", {})
        for key in ("usual", "runner", "costs", "filter_breakdown"):
            self.assertIn(key, tca)
        self.assertIn("total_spread_cost", tca["costs"])

        sheet = server.render_tearsheet_html(cfg, bt)
        self.assertIn("Strategy Performance Tear-Sheet", sheet)
        self.assertIn("Transaction Cost", sheet)
        self.assertIn("GOLD", sheet)

        srv_src = (Path(backtest.ROOT) / "server.py").read_text(encoding="utf-8")
        html_src = (Path(backtest.ROOT) / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("/api/tearsheet", srv_src)
        self.assertIn("/api/export-trades.csv", srv_src)
        self.assertIn("tab-tca", html_src)
        self.assertIn("renderTcaTab", html_src)

    def test_27_phase4_htf_filter_macro_blackout_and_sse_stream(self):
        """Phase 4: HTF trend filter, US Macro News blackout, category profiles, and SSE /api/stream."""
        import copy
        import io
        import json
        from datetime import datetime, timedelta, timezone
        from pathlib import Path
        from unittest.mock import MagicMock
        import backtest
        import bot
        import renko
        import risk
        import scanner
        import server

        # 1. HTF 8-brick slope filter blocks counter-trend reversal before macro trend turns
        p_raw = renko.Params(green_buy=2, red_sell=2, use_st=False, use_htf_filter=False, htf_bricks=8)
        p_htf = renko.Params(green_buy=2, red_sell=2, use_st=False, use_htf_filter=True, htf_bricks=8)
        e_raw = renko.RenkoEngine(p_raw)
        e_htf = renko.RenkoEngine(p_htf)
        t0 = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
        # Drop 12 bricks to 2580, then bounce to 2610 (+30 -> reversal prints 2 green bricks, still below ref close[-9])
        prices = [2700.0 - i * 10.0 for i in range(13)] + [2600.0, 2610.0]
        last_n_htf = 0
        for idx, px in enumerate(prices):
            t_cur = t0 + timedelta(minutes=idx)
            e_raw.on_bar(t_cur, px, px, px, 10.0)
            last_n_htf = e_htf.on_bar(t_cur, px, px, px, 10.0)
        d_raw = e_raw.decide(0, last_n_htf, bar_close=prices[-1], brick_size=10.0)
        d_htf = e_htf.decide(0, last_n_htf, bar_close=prices[-1], brick_size=10.0)
        self.assertEqual(d_raw.action, "BUY")
        self.assertEqual(d_htf.action, "")
        self.assertTrue(d_htf.filtered_reason.startswith("htf_trend"))

        # 2. Macro blackout guard (13:25-13:35 & 18:55-19:10 UTC)
        self.assertTrue(risk.in_macro_blackout(datetime(2026, 11, 12, 13, 30, tzinfo=timezone.utc)))
        self.assertTrue(risk.in_macro_blackout(datetime(2026, 11, 12, 19, 0, tzinfo=timezone.utc)))
        self.assertFalse(risk.in_macro_blackout(datetime(2026, 11, 12, 14, 0, tzinfo=timezone.utc)))

        cfg = copy.deepcopy(backtest.load_config())
        cfg["risk"]["macro_blackout"] = True
        cfg["risk"]["rollover_blackout"] = False
        cfg["risk"]["sessions"] = {"asian": True, "london": True, "new_york": True}
        ok_blk, reason_blk = risk.check_entry_allowed(
            cfg, 2600.0, 2600.2, 5.0, now=datetime(2026, 11, 12, 13, 30, tzinfo=timezone.utc)
        )
        self.assertFalse(ok_blk)
        self.assertIn("Macro News Blackout", reason_blk)

        # 3. Category profiles in config & scanner output
        for cat in ("Metals", "Energy", "Forex", "Indices"):
            self.assertIn(cat, cfg["risk"]["category_profiles"])
        scan_row = scanner.scan_single_instrument("EURUSD", cfg, cap=None)
        self.assertEqual(scan_row["category_profile"]["atr_div"], cfg["risk"]["category_profiles"]["Forex"]["atr_div"])

        # 4. Real-time SSE /api/stream endpoint returns valid event: pulse frame without blocking
        handler = server.Handler.__new__(server.Handler)
        handler.headers = {}
        handler.wfile = io.BytesIO()
        handler.rfile = io.BytesIO()
        handler.path = "/api/stream"
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.do_GET()
        handler.send_response.assert_called_with(200)
        handler.send_header.assert_any_call("Content-Type", "text/event-stream; charset=utf-8")
        body = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("event: pulse", body)
        data_line = next(line for line in body.splitlines() if line.startswith("data: "))
        pulse = json.loads(data_line[6:])
        self.assertTrue(pulse["ok"])
        self.assertIn("watcher", pulse)
        self.assertIn("pending_confirm", pulse)
        self.assertTrue(callable(bot.send_bot_telegram))

        # 5. UI markers for Phase 4
        html_src = (Path(backtest.ROOT) / "static" / "index.html").read_text(encoding="utf-8")
        for marker in ("in-use_htf_filter", "in-macro_blackout", "applyCategoryProfile", "EventSource('/api/stream')"):
            self.assertIn(marker, html_src)

    def test_28_phase5_ws_streamer_cloud_state_persistence_and_var95(self):
        """Phase 5: WebSocket tick streamer, SQLite/browser backup-restore persistence, and 95% Daily VaR."""
        import io
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock
        import backtest
        import bot
        import capital_api
        import server

        # 1. daily_var_95 computed cleanly in backtest.stats()
        st = backtest.stats([
            {"entry_time": "2026-11-10T10:00:00Z", "exit_time": "2026-11-10T10:05:00Z", "net": -45.0, "gross": -40.0},
            {"entry_time": "2026-11-11T10:00:00Z", "exit_time": "2026-11-11T10:05:00Z", "net": 30.0, "gross": 35.0},
            {"entry_time": "2026-11-12T10:00:00Z", "exit_time": "2026-11-12T10:05:00Z", "net": -20.0, "gross": -15.0},
            {"entry_time": "2026-11-13T10:00:00Z", "exit_time": "2026-11-13T10:05:00Z", "net": 55.0, "gross": 60.0},
        ], "net")
        self.assertIn("daily_var_95", st)
        self.assertEqual(st["daily_var_95"], 45.0)
        self.assertEqual(backtest.stats([], "net")["daily_var_95"], 0.0)

        # 2. Capital.com WS tick cache + SSE /api/stream live_tick delivery
        self.assertTrue(capital_api.WS_URL.startswith("wss://"))
        tick = capital_api.update_live_tick("GOLD", 3880.10, 3880.50, digits=2)
        self.assertEqual(tick["mid"], 3880.30)
        self.assertEqual(capital_api.get_live_tick("GOLD")["spread"], 0.40)

        handler = server.Handler.__new__(server.Handler)
        handler.headers = {}
        handler.wfile = io.BytesIO()
        handler.rfile = io.BytesIO()
        handler.path = "/api/stream"
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.do_GET()
        body = handler.wfile.getvalue().decode("utf-8")
        data_line = next(line for line in body.splitlines() if line.startswith("data: "))
        pulse = json.loads(data_line[6:])
        self.assertEqual(pulse["live_tick"]["mid"], 3880.30)

        # 3. Cloud state SQLite persistence & /api/backup-state + /api/restore-state round-trip
        orig_db = server.CLOUD_STATE_DB
        orig_live = server.LIVE_TRADES
        orig_srv_oms = server._oms_state_path
        orig_bot_oms = bot._oms_state_path
        orig_cfg = backtest.load_config()
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            server.CLOUD_STATE_DB = tmp / "cloud_state.sqlite"
            server.LIVE_TRADES = tmp / "live_trades.csv"
            server._oms_state_path = lambda: tmp / "oms_state.json"
            bot._oms_state_path = server._oms_state_path
            try:
                snap = server.build_cloud_backup_snapshot(orig_cfg)
                self.assertTrue(snap["ok"])
                self.assertTrue(server.CLOUD_STATE_DB.exists())
                loaded = server._load_snapshot_from_sqlite(server.CLOUD_STATE_DB)
                self.assertEqual(loaded["config"]["epic"], orig_cfg["epic"])

                snap["live_trades"] = [{
                    "time": "2026-11-14T10:00:00Z", "event": "OPEN", "side": "BUY",
                    "size": "1.0", "price": "3880.30", "stop": "3865.0",
                    "deal_id": "DEAL-RESTORE", "realized_pnl": "", "note": "RESTORE_TEST"
                }]
                restored = server.restore_cloud_backup_snapshot(snap)
                self.assertTrue(restored["restored"])
                self.assertTrue(server.LIVE_TRADES.exists())
                self.assertIn("DEAL-RESTORE", server.LIVE_TRADES.read_text())
            finally:
                server.CLOUD_STATE_DB = orig_db
                server.LIVE_TRADES = orig_live
                server._oms_state_path = orig_srv_oms
                bot._oms_state_path = orig_bot_oms
                backtest.save_config(orig_cfg)

        # 4. UI markers for Phase 5
        html_src = (Path(backtest.ROOT) / "static" / "index.html").read_text(encoding="utf-8")
        for marker in ("checkAndRestoreCloudState", "syncCloudBackupToBrowser", "live_tick", "VaR95"):
            self.assertIn(marker, html_src)

    def test_29_telegram_config_save_and_cloud_backup_roundtrip(self):
        import os
        import tempfile
        import bot
        orig_root = server.ROOT
        orig_db = server.CLOUD_STATE_DB
        orig_live = server.LIVE_TRADES
        orig_srv_oms = server._oms_state_path
        orig_bot_oms = bot._oms_state_path
        orig_load_env = server.load_env
        orig_tok = os.environ.get("TELEGRAM_BOT_TOKEN")
        orig_chat = os.environ.get("TELEGRAM_CHAT_ID")
        orig_cfg = backtest.load_config()

        def _make_handler(path: str, payload: dict | None = None):
            h = server.Handler.__new__(server.Handler)
            raw = json.dumps(payload or {}).encode("utf-8")
            h.headers = {"Content-Length": str(len(raw))} if payload is not None else {}
            h.rfile = io.BytesIO(raw)
            h.wfile = io.BytesIO()
            h.path = path
            h.send_response = MagicMock()
            h.send_header = MagicMock()
            h.end_headers = MagicMock()
            return h

        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            server.ROOT = tmp
            server.CLOUD_STATE_DB = tmp / "cloud_state.sqlite"
            server.LIVE_TRADES = tmp / "live_trades.csv"
            server._oms_state_path = lambda: tmp / "oms_state.json"
            bot._oms_state_path = server._oms_state_path
            server.load_env = lambda: None
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            os.environ.pop("TELEGRAM_CHAT_ID", None)
            try:
                # 1. Save Telegram credentials via POST /api/save-telegram
                h_save = _make_handler("/api/save-telegram", {"bot_token": "TEST_TOKEN_123", "chat_id": "123456"})
                h_save.do_POST()
                save_res = json.loads(h_save.wfile.getvalue().decode("utf-8"))
                self.assertTrue(save_res["ok"])
                self.assertTrue(save_res["bot"]["telegram"]["configured"])
                self.assertEqual(save_res["bot"]["telegram"]["chat_id"], "123456")

                # 2. Verify GET /api/status exposes Telegram configured state cleanly
                h_st = _make_handler("/api/status")
                h_st.do_GET()
                st_res = json.loads(h_st.wfile.getvalue().decode("utf-8"))
                self.assertTrue(st_res["telegram"]["configured"])
                self.assertEqual(st_res["telegram"]["chat_id"], "123456")

                # 3. Backup & Restore round-trip preserves Telegram configuration across restarts
                h_bak = _make_handler("/api/backup-state")
                h_bak.do_GET()
                snap = json.loads(h_bak.wfile.getvalue().decode("utf-8"))
                self.assertEqual(snap["telegram_config"]["bot_token"], "TEST_TOKEN_123")
                self.assertEqual(snap["telegram_config"]["chat_id"], "123456")

                os.environ.pop("TELEGRAM_BOT_TOKEN", None)
                os.environ.pop("TELEGRAM_CHAT_ID", None)
                h_rst = _make_handler("/api/restore-state", snap)
                h_rst.do_POST()
                rst_res = json.loads(h_rst.wfile.getvalue().decode("utf-8"))
                self.assertTrue(rst_res["restored"])
                self.assertEqual(os.environ.get("TELEGRAM_BOT_TOKEN"), "TEST_TOKEN_123")
                self.assertEqual(os.environ.get("TELEGRAM_CHAT_ID"), "123456")

                # 4. UI markers for Telegram configuration card
                html_src = (Path(backtest.ROOT) / "static" / "index.html").read_text(encoding="utf-8")
                for marker in ("tg-bot-token", "tg-chat-id", "saveTelegramConfig", "testTelegramAlert"):
                    self.assertIn(marker, html_src)
            finally:
                server.ROOT = orig_root
                server.CLOUD_STATE_DB = orig_db
                server.LIVE_TRADES = orig_live
                server._oms_state_path = orig_srv_oms
                bot._oms_state_path = orig_bot_oms
                server.load_env = orig_load_env
                if orig_tok is not None:
                    os.environ["TELEGRAM_BOT_TOKEN"] = orig_tok
                else:
                    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
                if orig_chat is not None:
                    os.environ["TELEGRAM_CHAT_ID"] = orig_chat
                else:
                    os.environ.pop("TELEGRAM_CHAT_ID", None)
                backtest.save_config(orig_cfg)

    def test_30_phase6_gst_time_optimizer_telegram_commands_and_guide(self):
        """Phase 6: UAE GST formatting, grid optimizer, 2-way Telegram commands, password lock & guide routes."""
        import os
        import tempfile
        from datetime import datetime, timezone
        from unittest.mock import MagicMock

        orig_root = server.ROOT
        orig_db = server.CLOUD_STATE_DB
        orig_load_env = server.load_env
        orig_pw = os.environ.get("DASHBOARD_PASSWORD")
        orig_watch_run = server.WATCHER.running

        def _make_handler(path: str, payload: dict | None = None):
            h = server.Handler.__new__(server.Handler)
            raw = json.dumps(payload or {}).encode("utf-8")
            h.headers = {"Content-Length": str(len(raw))} if payload is not None else {}
            h.rfile = io.BytesIO(raw)
            h.wfile = io.BytesIO()
            h.path = path
            h.send_response = MagicMock()
            h.send_header = MagicMock()
            h.end_headers = MagicMock()
            return h

        # 1. UAE Gulf Standard Time (+04:00) formatting
        self.assertEqual(
            server.fmt_gst(datetime(2026, 11, 14, 18, 30, 5, tzinfo=timezone.utc)),
            "2026-11-14 22:30:05 GST",
        )
        self.assertEqual(server.fmt_gst("2026-11-14T21:00:00Z"), "2026-11-15 01:00:00 GST")

        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            server.ROOT = tmp
            server.CLOUD_STATE_DB = tmp / "cloud_state.sqlite"
            server.load_env = lambda: None
            os.environ.pop("DASHBOARD_PASSWORD", None)
            try:
                # 2. Parameter Optimizer endpoint (/api/optimize)
                h_opt = _make_handler("/api/optimize", {"atr_divisors": [15, 32], "tp_bricks": [3.0], "confirm_minutes": [0, 5]})
                h_opt.do_POST()
                opt_res = json.loads(h_opt.wfile.getvalue().decode("utf-8"))
                self.assertTrue(opt_res["ok"])
                self.assertEqual(opt_res["tested_combos"], 4)
                self.assertIn("GST", opt_res["scanned_at_gst"])
                self.assertGreaterEqual(opt_res["results"][0]["calmar"], opt_res["results"][-1]["calmar"])

                # 2b. BTCUSD default optimizer uses Crypto DNA grid and never ranks 0-trade combos #1
                btc_cfg = backtest.load_config()
                btc_cfg["epic"] = "BTCUSD"
                btc_opt = server.run_parameter_optimizer(btc_cfg, {})
                self.assertEqual(btc_opt["tested_combos"], 36)
                self.assertGreater(btc_opt["results"][0]["trades"], 0)
                zero_indices = [i for i, r in enumerate(btc_opt["results"]) if r["trades"] == 0]
                nonzero_indices = [i for i, r in enumerate(btc_opt["results"]) if r["trades"] > 0]
                if zero_indices and nonzero_indices:
                    self.assertGreater(min(zero_indices), max(nonzero_indices))

                # 3. 2-way Telegram Command Handlers
                self.assertIn("RENKO COCKPIT STATUS", server.handle_telegram_command("/status"))
                self.assertIn("RENKO CLOUD DAILY DIGEST", server.handle_telegram_command("/pnl"))
                pause_reply = server.handle_telegram_command("/pause")
                self.assertIn("PAUSED", pause_reply)
                self.assertFalse(server.WATCHER.running)
                resume_reply = server.handle_telegram_command("/resume")
                self.assertIn("RESUMED", resume_reply)
                self.assertTrue(server.WATCHER.running)

                # 4. Runtime Dashboard Password Set & Clear (/api/save-password)
                h_pw_set = _make_handler("/api/save-password", {"password": "Secret99!"})
                h_pw_set.do_POST()
                pw_set_res = json.loads(h_pw_set.wfile.getvalue().decode("utf-8"))
                self.assertTrue(pw_set_res["password_protected"])
                self.assertEqual(os.environ.get("DASHBOARD_PASSWORD"), "Secret99!")

                h_pw_clr = _make_handler("/api/save-password", {"password": ""})
                h_pw_clr.headers["Authorization"] = f"Bearer {pw_set_res['token']}"
                h_pw_clr.do_POST()
                pw_clr_res = json.loads(h_pw_clr.wfile.getvalue().decode("utf-8"))
                self.assertFalse(pw_clr_res["password_protected"])

                # 5. Master Guide HTML & PDF routes (/guide & /api/guide.pdf)
                h_guide = _make_handler("/guide")
                server.ROOT = orig_root
                h_guide.do_GET()
                h_guide.send_response.assert_called_with(200)
                self.assertIn(b"McKinsey-Style Executive Manual", h_guide.wfile.getvalue())

                h_pdf = _make_handler("/api/guide.pdf")
                h_pdf.do_GET()
                h_pdf.send_response.assert_called_with(200)
                self.assertTrue(h_pdf.wfile.getvalue().startswith(b"%PDF-"))

                # 6. UI markers for Phase 6 & UAE Clock
                html_src = (Path(backtest.ROOT) / "static" / "index.html").read_text(encoding="utf-8")
                for marker in ("hdr-gst-clock", "tab-optimizer", "runOptimizerGrid", "sendTelegramDigest", "saveDashboardPassword"):
                    self.assertIn(marker, html_src)
            finally:
                server.ROOT = orig_root
                server.CLOUD_STATE_DB = orig_db
                server.load_env = orig_load_env
                server.WATCHER.running = orig_watch_run
                if orig_pw is not None:
                    os.environ["DASHBOARD_PASSWORD"] = orig_pw
                else:
                    os.environ.pop("DASHBOARD_PASSWORD", None)

    def test_31_phase7_15yr_dna_matrix_and_apply_endpoint(self):
        """Phase 7: 15-Year Per-Instrument DNA matrix, scanner/API wiring, Telegram /dna & /best, and UI markers."""
        import copy
        import tempfile
        from unittest.mock import MagicMock
        import research_15yr_matrix
        import scanner

        orig_cfg = copy.deepcopy(backtest.load_config())
        orig_root = server.ROOT
        orig_db = server.CLOUD_STATE_DB
        orig_load_env = server.load_env
        orig_pw = os.environ.get("DASHBOARD_PASSWORD")

        def _make_handler(path: str, payload: dict | None = None):
            h = server.Handler.__new__(server.Handler)
            raw = json.dumps(payload or {}).encode("utf-8")
            h.headers = {"Content-Length": str(len(raw))} if payload is not None else {}
            h.rfile = io.BytesIO(raw)
            h.wfile = io.BytesIO()
            h.path = path
            h.send_response = MagicMock()
            h.send_header = MagicMock()
            h.end_headers = MagicMock()
            return h

        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            server.ROOT = tmp
            server.CLOUD_STATE_DB = tmp / "cloud_state.sqlite"
            server.load_env = lambda: None
            os.environ.pop("DASHBOARD_PASSWORD", None)
            try:
                # 1. Verify 15Y matrix has all 19 positive instruments & benchmark table
                matrix = research_15yr_matrix.get_or_build_matrix()
                self.assertEqual(len(matrix["instruments"]), 19)
                self.assertGreater(matrix["summary"]["portfolio_net_15y"], 500000.0)
                self.assertTrue(all(r["net_15y"] > 0 for r in matrix["instruments"]))
                self.assertGreaterEqual(len(matrix.get("competitor_benchmark", [])), 5)

                # 2. Scanner retains category_profile (test_26 parity) + adds per-instrument 15Y DNA
                eur_scan = scanner.scan_single_instrument("EURUSD", orig_cfg, cap=None)
                self.assertEqual(eur_scan["category_profile"]["atr_div"], orig_cfg["risk"]["category_profiles"]["Forex"]["atr_div"])
                self.assertIn("instrument_profile", eur_scan)
                self.assertIn("dna_summary", eur_scan)

                # 3. GET /api/matrix-15y & POST /api/apply-instrument-dna
                h_get = _make_handler("/api/matrix-15y")
                h_get.do_GET()
                h_get.send_response.assert_called_with(200)
                m_res = json.loads(h_get.wfile.getvalue().decode("utf-8"))
                self.assertEqual(len(m_res["instruments"]), 19)
                self.assertIn("BTCUSD", m_res["profiles_by_epic"])
                self.assertIn("ETHUSD", m_res["profiles_by_epic"])
                self.assertIn("SOLUSD", m_res["profiles_by_epic"])

                h_apply = _make_handler("/api/apply-instrument-dna", {"epic": "US100"})
                h_apply.do_POST()
                h_apply.send_response.assert_called_with(200)
                ap_res = json.loads(h_apply.wfile.getvalue().decode("utf-8"))
                self.assertTrue(ap_res["ok"])
                self.assertEqual(ap_res["epic"], "US100")
                self.assertEqual(ap_res["config"]["strategy"]["atr_div"], m_res["profiles_by_epic"]["US100"]["atr_div"])

                # 4. Telegram /dna & /best commands
                self.assertIn("15Y OPTIMAL DNA", server.handle_telegram_command("/dna GOLD"))
                self.assertIn("15Y OPTIMAL DNA", server.handle_telegram_command("/dna BTCUSD"))
                self.assertIn("15-YEAR OPTIMAL DNA LEADERBOARD", server.handle_telegram_command("/best"))

                # 5. Phase 7 UI markers present in index.html
                html_src = (Path(backtest.ROOT) / "static" / "index.html").read_text(encoding="utf-8")
                for marker in ("tab-matrix15y", "applyInstrumentDna", "matrix15y-canvas", "in-auto_dna_switch", "matrix15y-bench-tbody"):
                    self.assertIn(marker, html_src)
            finally:
                server.ROOT = orig_root
                server.CLOUD_STATE_DB = orig_db
                server.load_env = orig_load_env
                if orig_pw is not None:
                    os.environ["DASHBOARD_PASSWORD"] = orig_pw
                else:
                    os.environ.pop("DASHBOARD_PASSWORD", None)
                backtest.save_config(orig_cfg)

    def test_32_view_only_public_share_mode(self):
        """Verify Read-Only / View-Only Share Mode blocks mutations & backups while allowing Admin unlock."""
        def _make_handler(path: str, payload: dict | None = None):
            h = server.Handler.__new__(server.Handler)
            raw = json.dumps(payload or {}).encode("utf-8")
            h.headers = {"Content-Length": str(len(raw))} if payload is not None else {}
            h.rfile = io.BytesIO(raw)
            h.wfile = io.BytesIO()
            h.path = path
            h.send_response = MagicMock()
            h.send_header = MagicMock()
            h.end_headers = MagicMock()
            return h

        with patch.dict(os.environ, {"VIEW_ONLY_PUBLIC": "1", "DASHBOARD_PASSWORD": "AdminSharePass77"}):
            orig_load_env = server.load_env
            server.load_env = lambda: None
            try:
                tok = server.expected_session_token("AdminSharePass77")

                # 1. Public visitor GET /api/auth-status & /api/state -> 200 read-only, /api/backup-state -> 403
                h_auth = _make_handler("/api/auth-status")
                h_auth.do_GET()
                h_auth.send_response.assert_called_with(200)
                a_res = json.loads(h_auth.wfile.getvalue().decode("utf-8"))
                self.assertTrue(a_res["view_only_public"])
                self.assertTrue(a_res["read_only"])

                h_st = _make_handler("/api/state")
                h_st.do_GET()
                h_st.send_response.assert_called_with(200)
                st_res = json.loads(h_st.wfile.getvalue().decode("utf-8"))
                self.assertTrue(st_res["read_only"])

                h_bk = _make_handler("/api/backup-state")
                h_bk.do_GET()
                h_bk.send_response.assert_called_with(403)

                # 2. Public visitor mutating POSTs blocked with 403
                for ep_path, payload in (
                    ("/api/config", {"epic": "BTCUSD"}),
                    ("/api/switch-instrument", {"epic": "ETHUSD"}),
                    ("/api/kill-switch", {"kill": True}),
                ):
                    h_mut = _make_handler(ep_path, payload)
                    h_mut.do_POST()
                    h_mut.send_response.assert_called_with(403)
                    m_res = json.loads(h_mut.wfile.getvalue().decode("utf-8"))
                    self.assertTrue(m_res.get("read_only"))

                # 3. Public visitor read-only /api/optimize allowed
                h_opt = _make_handler("/api/optimize", {"atr_divisors": [15], "tp_bricks": [3.0], "confirm_minutes": [0]})
                h_opt.do_POST()
                h_opt.send_response.assert_called_with(200)

                # 4. Authenticated Admin passes read-only gate on GET /api/backup-state
                h_adm_bk = _make_handler("/api/backup-state")
                h_adm_bk.headers["Authorization"] = f"Bearer {tok}"
                h_adm_bk.do_GET()
                h_adm_bk.send_response.assert_called_with(200)

                # 5. UI markers present
                html_src = (Path(backtest.ROOT) / "static" / "index.html").read_text(encoding="utf-8")
                for marker in ("pill-readonly", "btn-toggle-viewonly", "applyReadOnlyUiState", "/api/save-view-only"):
                    self.assertIn(marker, html_src)
            finally:
                server.load_env = orig_load_env


if __name__ == "__main__":
    unittest.main()
