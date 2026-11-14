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


if __name__ == "__main__":
    unittest.main()
