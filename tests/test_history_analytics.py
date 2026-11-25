"""Unit tests for History Analytics and Multi-Account Trade History."""
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from unittest.mock import patch

from history_analytics import (
    TradeRecord,
    save_trades_to_history,
    query_raw_trades,
    compute_performance_metrics,
    get_filtered_trade_analytics,
    get_accounts_registry,
    sync_capital_transactions,
    GST_TZ,
)


class TestHistoryAnalytics(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = Path(self.tmpdir.name) / "test_state.sqlite"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_trade_record_to_dict(self):
        t = TradeRecord(
            deal_id="deal-1",
            broker="CAPITAL",
            account_id="capital_demo",
            account_name="Capital Demo",
            epic="US30",
            direction="BUY",
            size=0.25,
            open_price=48000.0,
            close_price=48200.0,
            stop_level=47850.0,
            pnl=50.0,
            currency="USD",
            opened_at_utc="2026-11-23T05:00:00Z",
            closed_at_utc="2026-11-23T07:00:00Z",
            exit_type="TRAILING_STOP",
            note="Test deal",
        )
        d = t.to_dict()
        self.assertEqual(d["deal_id"], "deal-1")
        self.assertEqual(d["pnl"], 50.0)
        self.assertIn("GST", d["opened_at_gst"])
        self.assertIn("GST", d["closed_at_gst"])

    def test_save_and_query_trades(self):
        t1 = TradeRecord(
            deal_id="deal-1",
            broker="CAPITAL",
            account_id="capital_demo",
            account_name="Capital Demo",
            epic="US30",
            direction="BUY",
            size=0.25,
            open_price=48000.0,
            close_price=48200.0,
            stop_level=47850.0,
            pnl=66.74,
            currency="USD",
            opened_at_utc="2026-11-23T00:31:08Z",
            closed_at_utc="2026-11-23T02:37:58Z",
            exit_type="TRAILING_STOP",
        )
        t2 = TradeRecord(
            deal_id="deal-2",
            broker="CAPITAL",
            account_id="capital_demo",
            account_name="Capital Demo",
            epic="SILVER",
            direction="BUY",
            size=52.0,
            open_price=42.199,
            close_price=41.573,
            stop_level=41.575,
            pnl=-32.55,
            currency="USD",
            opened_at_utc="2026-11-23T00:51:03Z",
            closed_at_utc="2026-11-23T02:30:54Z",
            exit_type="SL_HIT",
        )
        t3 = TradeRecord(
            deal_id="deal-3",
            broker="MT5",
            account_id="alpha_direct_mt5",
            account_name="Alpha Direct Prop",
            epic="EURUSD",
            direction="BUY",
            size=1.0,
            open_price=1.0850,
            close_price=1.0880,
            stop_level=1.0820,
            pnl=30.0,
            currency="USD",
            opened_at_utc="2026-11-23T10:00:00Z",
            closed_at_utc="2026-11-23T12:00:00Z",
            exit_type="TP_HIT",
        )
        save_trades_to_history([t1, t2, t3], db_path=self.db)

        # Query Capital demo
        cap_trades = query_raw_trades(broker="CAPITAL", account_id="capital_demo", db_path=self.db)
        self.assertEqual(len(cap_trades), 2)

        # Query MT5 account
        mt5_trades = query_raw_trades(broker="MT5", account_id="alpha_direct_mt5", db_path=self.db)
        self.assertEqual(len(mt5_trades), 1)
        self.assertEqual(mt5_trades[0].epic, "EURUSD")

        # Query all combined
        all_trades = query_raw_trades(broker="ALL", account_id="ALL", db_path=self.db)
        self.assertEqual(len(all_trades), 3)

    def test_metrics_and_asset_breakdown(self):
        trades = [
            TradeRecord("d1", "CAPITAL", "demo", "Demo", "US30", "BUY", 0.25, 48000, 48200, None, 100.0, "USD", "2026-11-23T01:00:00Z", "2026-11-23T02:00:00Z"),
            TradeRecord("d2", "CAPITAL", "demo", "Demo", "US30", "BUY", 0.25, 48200, 48100, None, -50.0, "USD", "2026-11-23T03:00:00Z", "2026-11-23T04:00:00Z"),
            TradeRecord("d3", "CAPITAL", "demo", "Demo", "SILVER", "BUY", 50, 42.0, 41.5, None, -25.0, "USD", "2026-11-23T05:00:00Z", "2026-11-23T06:00:00Z"),
        ]
        metrics = compute_performance_metrics(trades)

        self.assertEqual(metrics["total_trades"], 3)
        self.assertEqual(metrics["wins"], 1)
        self.assertEqual(metrics["losses"], 2)
        self.assertAlmostEqual(metrics["win_rate_%"], 33.3, places=1)
        self.assertEqual(metrics["gross_profit"], 100.0)
        self.assertEqual(metrics["gross_loss"], 75.0)
        self.assertEqual(metrics["net_pnl"], 25.0)
        self.assertAlmostEqual(metrics["profit_factor"], 1.33, places=2)
        self.assertEqual(metrics["avg_win"], 100.0)
        self.assertEqual(metrics["avg_loss"], 37.5)
        self.assertAlmostEqual(metrics["risk_reward_ratio"], 2.67, places=2)

        # Asset breakdown check
        breakdown = metrics["asset_breakdown"]
        self.assertEqual(len(breakdown), 2)
        us30_stats = next(a for a in breakdown if a["epic"] == "US30")
        self.assertEqual(us30_stats["total_trades"], 2)
        self.assertEqual(us30_stats["net_pnl"], 50.0)
        self.assertEqual(us30_stats["win_rate_%"], 50.0)

    def test_filtered_presets(self):
        now = datetime.now(timezone.utc)
        t_today = TradeRecord("d1", "CAPITAL", "capital_demo", "Demo", "GOLD", "BUY", 1.0, 2600, 2610, None, 10.0, "USD", (now - timedelta(hours=2)).isoformat(), now.isoformat())
        save_trades_to_history([t_today], db_path=self.db)

        res = get_filtered_trade_analytics(account_id="capital_demo", preset="today", db_path=self.db)
        self.assertIn("total_trades", res)
        self.assertIn("asset_breakdown", res)
        self.assertIn("account", res)

    def test_sync_capital_transactions_recent_and_deal_fallback(self):
        now_utc = datetime.now(timezone.utc)
        recent_close = (now_utc - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        old_close = "2026-11-10T15:05:31.948"

        class FakeCap:
            def _req(self, method, path):
                if "/history/transactions" in path:
                    return {
                        "transactions": [
                            {
                                "dealId": "deal-recent",
                                "instrumentName": "US30",
                                "size": "-18.76",
                                "currency": "USD",
                                "dateUtc": recent_close,
                                "note": "close",
                            },
                            {
                                "dealId": "deal-fallback",
                                "instrumentName": "DE40",
                                "size": "-6.75",
                                "currency": "USD",
                                "dateUtc": old_close,
                                "note": "Trade closed",
                            },
                        ]
                    }
                if "/history/activity?" in path and "2026-11-10T" in path:
                    # Tight (from, to) fallback window for older deal outside 48h
                    return {
                        "activities": [
                            {
                                "dealId": "deal-fallback",
                                "type": "POSITION",
                                "status": "ACCEPTED",
                                "source": "SL",
                                "dateUTC": old_close,
                                "details": {
                                    "direction": "BUY",
                                    "size": 0.11,
                                    "level": 24921.4,
                                    "openPrice": 24865.3,
                                },
                            }
                        ]
                    }
                if "/history/activity?" in path and "&to=" in path:
                    return {
                        "activities": [
                            {
                                "dealId": "deal-recent",
                                "type": "POSITION",
                                "status": "ACCEPTED",
                                "source": "SYSTEM",
                                "dateUTC": recent_close,
                                "details": {
                                    "direction": "SELL",
                                    "size": 0.25,
                                    "level": 48100.0,
                                    "openPrice": 48175.04,
                                },
                            },
                            {
                                "dealId": "deal-recent",
                                "type": "POSITION",
                                "status": "ACCEPTED",
                                "source": "USER",
                                "dateUTC": (now_utc - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "details": {
                                    "direction": "BUY",
                                    "size": 0.25,
                                    "level": 48175.04,
                                    "stopLevel": 48050.0,
                                },
                            },
                        ]
                    }
                return {"activities": []}

        with patch("history_analytics.CLOUD_DB", self.db):
            synced = sync_capital_transactions(FakeCap())

        self.assertEqual(len(synced), 2)
        by_id = {t.deal_id: t for t in synced}

        self.assertEqual(by_id["deal-recent"].direction, "BUY")
        self.assertEqual(by_id["deal-recent"].size, 0.25)
        self.assertEqual(by_id["deal-recent"].open_price, 48175.04)
        self.assertEqual(by_id["deal-recent"].close_price, 48100.0)
        self.assertEqual(by_id["deal-recent"].exit_type, "STRATEGY_EXIT")

        self.assertEqual(by_id["deal-fallback"].direction, "SELL")
        self.assertEqual(by_id["deal-fallback"].size, 0.11)
        self.assertEqual(by_id["deal-fallback"].open_price, 24865.3)
        self.assertEqual(by_id["deal-fallback"].close_price, 24921.4)
        self.assertEqual(by_id["deal-fallback"].exit_type, "SL_HIT")


if __name__ == "__main__":
    unittest.main()
