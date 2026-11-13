import unittest
from backtest import INSTRUMENT_SPECS, generate_sample_gold_data
from bot import resolve_epic_alias, EPIC_ALIASES
from server import serialize_backtest, load_config


class TestMultiInstrument(unittest.TestCase):
    def test_instrument_specs_coverage(self):
        """Verify all requested asset classes (Oil, Forex, Indices) exist in INSTRUMENT_SPECS."""
        required = [
            # Metals
            "GOLD", "XAUUSD", "SILVER",
            # Energy & Oil
            "OIL_CRUDE", "OIL_BRENT",
            # Major Forex
            "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF",
            # Major Indices
            "US500", "US100", "US30", "GER40", "UK100", "JP225",
        ]
        for epic in required:
            self.assertIn(epic, INSTRUMENT_SPECS, f"Missing spec for {epic}")
            spec = INSTRUMENT_SPECS[epic]
            self.assertGreater(spec["base_px"], 0)
            self.assertGreater(spec["tick"], 0)
            self.assertGreater(spec["fallback_size"], 0)

    def test_alias_resolution(self):
        """Verify broker alias mapping works for Oil, Forex, and Indices."""
        # Crude oil aliases
        oil_aliases = resolve_epic_alias("OIL_CRUDE")
        self.assertIn("USCRUDE", oil_aliases)
        self.assertIn("WTI", oil_aliases)

        # Forex aliases
        eur_aliases = resolve_epic_alias("EURUSD")
        self.assertIn("EUR/USD", eur_aliases)

        # Index aliases
        us500_aliases = resolve_epic_alias("US500")
        self.assertIn("SP500", us500_aliases)

    def test_sample_candle_generation_across_classes(self):
        """Verify realistic candles are generated with correct scale & price levels."""
        # Oil
        oil_bars, _ = generate_sample_gold_data(epic="OIL_CRUDE", days_intraday=3, seed=42)
        self.assertTrue(len(oil_bars) > 0)
        self.assertTrue(40.0 < oil_bars[-1]["c"] < 120.0, f"Unexpected oil price: {oil_bars[-1]['c']}")

        # EURUSD
        eur_bars, _ = generate_sample_gold_data(epic="EURUSD", days_intraday=3, seed=42)
        self.assertTrue(len(eur_bars) > 0)
        self.assertTrue(0.80 < eur_bars[-1]["c"] < 1.40, f"Unexpected EURUSD price: {eur_bars[-1]['c']}")

        # US500
        sp_bars, _ = generate_sample_gold_data(epic="US500", days_intraday=3, seed=42)
        self.assertTrue(len(sp_bars) > 0)
        self.assertTrue(4000.0 < sp_bars[-1]["c"] < 8000.0, f"Unexpected US500 price: {sp_bars[-1]['c']}")

    def test_serialize_backtest_with_oil_and_forex(self):
        """Verify serialize_backtest returns correct digits and brick metrics for other instruments."""
        # Test Oil
        cfg_oil = load_config()
        cfg_oil["epic"] = "OIL_CRUDE"
        cfg_oil["tick"] = 0.01
        cfg_oil["strategy"]["fallback_size"] = 0.20
        res_oil = serialize_backtest(cfg_oil)
        self.assertEqual(res_oil["data_summary"]["epic"], "OIL_CRUDE")
        self.assertEqual(res_oil["data_summary"]["digits"], 2)
        self.assertGreater(len(res_oil["bricks"]), 0)

        # Test EURUSD
        cfg_eur = load_config()
        cfg_eur["epic"] = "EURUSD"
        cfg_eur["tick"] = 0.0001
        cfg_eur["strategy"]["fallback_size"] = 0.0008
        res_eur = serialize_backtest(cfg_eur)
        self.assertEqual(res_eur["data_summary"]["epic"], "EURUSD")
        self.assertEqual(res_eur["data_summary"]["digits"], 4)
        self.assertGreater(len(res_eur["bricks"]), 0)

    def test_forex_decimal_points_and_trade_rounding(self):
        """Verify Forex trades and bricks maintain full 4-5 decimal precision (not truncated to 2)."""
        from backtest import simulate
        cfg_gbp = load_config()
        cfg_gbp["epic"] = "GBPUSD"
        cfg_gbp["tick"] = 0.0001
        cfg_gbp["strategy"]["fallback_size"] = 0.0010
        bars, days = generate_sample_gold_data(epic="GBPUSD", days_intraday=12, seed=42)
        trades, eng, signals, filtered = simulate(bars, days, cfg_gbp)
        self.assertGreater(len(trades), 0, "Expected backtest trades on GBPUSD")
        for t in trades:
            # Entry and exit prices should have 4 decimal precision
            entry_str = f"{t['entry']:.4f}"
            self.assertEqual(t["entry"], round(t["entry"], 4))
            self.assertGreater(t["brick_size"], 0.0)
            self.assertLess(t["brick_size"], 0.05, f"Brick size too large for Forex: {t['brick_size']}")

    def test_scanner_engine_all_instruments(self):
        """Verify multi-instrument scanner evaluates all 16 instruments with correct metadata."""
        from scanner import scan_all_instruments, get_cached_or_scan_all
        cfg = load_config()
        res = scan_all_instruments(cfg)
        self.assertIn("items", res)
        self.assertEqual(len(res["items"]), 16)
        
        epics = {it["epic"] for it in res["items"]}
        expected_epics = {"GOLD", "SILVER", "OIL_CRUDE", "OIL_BRENT", "EURUSD", "GBPUSD", "USDJPY",
                          "AUDUSD", "USDCAD", "USDCHF", "US500", "US100", "US30", "GER40", "UK100", "JP225"}
        self.assertEqual(epics, expected_epics)

        for it in res["items"]:
            self.assertIn("price", it)
            self.assertIn("spread", it)
            self.assertIn("brick_size", it)
            self.assertIn("st_dir", it)
            self.assertIn("signal", it)
            self.assertIn("pip_factor", it)
            if it["epic"] in ("EURUSD", "GBPUSD", "AUDUSD", "USDCAD", "USDCHF"):
                self.assertEqual(it["currency"], "")
                self.assertEqual(it["pip_factor"], 10000)
                self.assertEqual(it["digits"], 4)
            elif it["epic"] == "USDJPY":
                self.assertEqual(it["currency"], "")
                self.assertEqual(it["pip_factor"], 100)
                self.assertEqual(it["digits"], 2)
            elif it["epic"] in ("GOLD", "SILVER", "OIL_CRUDE", "OIL_BRENT"):
                self.assertEqual(it["currency"], "$")

    def test_step_multi_market_execution(self):
        """Verify step_multi_market runs across all instruments independently without blocking."""
        from bot import step_multi_market

        class MockCapital:
            def __init__(self):
                self.env = "demo"
                self.opened_positions = []
                self.closed_positions = []
                self.updated_stops = []
            def positions(self):
                return []
            def market(self, epic):
                return {"snapshot": {"bid": 100.0, "offer": 100.1, "marketStatus": "TRADEABLE"}}
            def prices(self, epic, res, start, end):
                return []
            def open_position(self, epic, direction, size, stop_level=None):
                deal = {"dealId": f"mock-{epic}", "dealStatus": "ACCEPTED", "level": 100.0}
                self.opened_positions.append((epic, direction, size, stop_level))
                return deal
            def close_position(self, deal_id):
                self.closed_positions.append(deal_id)
                return {"dealId": deal_id, "dealStatus": "CLOSED", "profit": 10.0, "level": 100.0}
            def update_stop(self, deal_id, stop_level):
                self.updated_stops.append((deal_id, stop_level))
                return {"dealId": deal_id, "status": "ACCEPTED"}

        mock_cap = MockCapital()
        cfg = load_config()
        test_epics = ["GOLD", "OIL_CRUDE", "EURUSD"]
        summary = step_multi_market(mock_cap, cfg, epics=test_epics)
        self.assertIn("monitored", summary)
        self.assertEqual(summary["monitored"], 3)


if __name__ == "__main__":
    unittest.main()
