import csv
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import backtest
import bot
import risk
import server
from risk import check_entry_allowed, daily_loss_bypass_active


def _journal(tmp: Path, pnl: float, day: str) -> Path:
    p = tmp / "live_trades.csv"
    with p.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["time", "event", "side", "size", "price", "stop",
                                          "deal_id", "realized_pnl", "note"])
        w.writeheader()
        w.writerow({"time": f"{day}T10:00:00Z", "event": "CLOSE", "side": "EXIT", "size": "1",
                    "price": "1", "stop": "", "deal_id": "x", "realized_pnl": str(pnl), "note": ""})
    return p


def _make_handler(path: str, payload: dict | None = None, headers: dict | None = None):
    h = server.Handler.__new__(server.Handler)
    raw = json.dumps(payload or {}).encode("utf-8")
    hdrs = {"Content-Length": str(len(raw))} if payload is not None else {}
    if headers:
        hdrs.update(headers)
    h.headers = hdrs
    h.rfile = io.BytesIO(raw)
    h.wfile = io.BytesIO()
    h.path = path
    h.send_response = MagicMock()
    h.send_header = MagicMock()
    h.end_headers = MagicMock()
    return h


class TestDailyLossBypass(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
        self.day = "2026-11-19"

    def _cfg(self, **risk_overrides):
        base = {"max_daily_loss": 250.0, "max_trades_per_day": 0, "sessions": {}}
        base.update(risk_overrides)
        return {"risk": base}

    def test_blocks_without_bypass(self):
        with tempfile.TemporaryDirectory() as t:
            csv_p = _journal(Path(t), -300.0, self.day)
            ok, why = check_entry_allowed(self._cfg(), None, None, 1.0, csv_p, self.now)
            self.assertFalse(ok)
            self.assertIn("Daily loss limit", why)

    def test_bypass_today_allows(self):
        with tempfile.TemporaryDirectory() as t:
            csv_p = _journal(Path(t), -300.0, self.day)
            cfg = self._cfg(daily_loss_bypass_date=self.day)
            ok, _ = check_entry_allowed(cfg, None, None, 1.0, csv_p, self.now)
            self.assertTrue(ok)

    def test_bypass_expires_next_day(self):
        cfg = self._cfg(daily_loss_bypass_date="2026-11-18")
        self.assertFalse(daily_loss_bypass_active(cfg["risk"], self.now))

    def test_endpoint_auth_toggle_and_stream_pulse(self):
        orig_env = dict(os.environ)
        orig_bt_root = backtest.ROOT
        orig_srv_root = server.ROOT
        orig_db = server.CLOUD_STATE_DB
        orig_srv_trades = server.LIVE_TRADES
        orig_bot_trades = bot.LIVE_TRADES
        orig_risk_journal = risk.JOURNAL
        orig_pending = server.PENDING_FILE
        orig_oms = server._oms_state_path
        orig_bot_oms = bot._oms_state_path
        orig_load_env = server.load_env

        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            shutil.copy(orig_bt_root / "config.json", tmp / "config.json")
            backtest.ROOT = tmp
            server.ROOT = tmp
            server.CLOUD_STATE_DB = tmp / "cloud_state.sqlite"
            server.LIVE_TRADES = tmp / "live_trades.csv"
            bot.LIVE_TRADES = tmp / "live_trades.csv"
            risk.JOURNAL = tmp
            server.PENDING_FILE = tmp / "pending_ticket.json"
            server._oms_state_path = lambda: tmp / "oms_state.json"
            bot._oms_state_path = server._oms_state_path
            server.load_env = lambda: None

            env_patch = {
                "RENKO_TESTING": "1",
                "VIEW_ONLY_PUBLIC": "1",
                "DASHBOARD_PASSWORD": "AdminTestPass1978",
                "CAPITAL_API_KEY": "",
                "CAPITAL_IDENTIFIER": "",
                "CAPITAL_PASSWORD": "",
            }
            try:
                with patch.dict(os.environ, env_patch):
                    tok = server.expected_session_token("AdminTestPass1978")
                    auth_hdr = {"Authorization": f"Bearer {tok}"}

                    # 1. Public visitor blocked with 403
                    h_pub = _make_handler("/api/daily-loss-bypass", {"active": True})
                    h_pub.do_POST()
                    h_pub.send_response.assert_called_with(403)
                    pub_res = json.loads(h_pub.wfile.getvalue().decode("utf-8"))
                    self.assertTrue(pub_res.get("read_only"))

                    # 2. Admin enables bypass -> 200 and persisted
                    h_on = _make_handler("/api/daily-loss-bypass", {"active": True}, headers=auth_hdr)
                    h_on.do_POST()
                    h_on.send_response.assert_called_with(200)
                    on_res = json.loads(h_on.wfile.getvalue().decode("utf-8"))
                    self.assertTrue(on_res["ok"])
                    self.assertTrue(on_res["bot"]["daily_loss_bypass"])
                    self.assertGreater(on_res["bot"]["max_daily_loss"], 0)

                    # 3. /api/stream SSE pulse reflects daily_loss_bypass = True
                    h_sse = _make_handler("/api/stream")
                    h_sse.do_GET()
                    h_sse.send_response.assert_called_with(200)
                    sse_raw = h_sse.wfile.getvalue().decode("utf-8")
                    data_line = [ln for ln in sse_raw.splitlines() if ln.startswith("data: ")][0]
                    pulse = json.loads(data_line[len("data: "):])
                    self.assertTrue(pulse["daily_loss_bypass"])
                    self.assertEqual(pulse["max_daily_loss"], on_res["bot"]["max_daily_loss"])

                    # 4. Admin disables bypass -> 200 and daily_loss_bypass = False
                    h_off = _make_handler("/api/daily-loss-bypass", {"active": False}, headers=auth_hdr)
                    h_off.do_POST()
                    h_off.send_response.assert_called_with(200)
                    off_res = json.loads(h_off.wfile.getvalue().decode("utf-8"))
                    self.assertTrue(off_res["ok"])
                    self.assertFalse(off_res["bot"]["daily_loss_bypass"])

                    # 5. Audit events logged to live_trades.csv
                    with server.LIVE_TRADES.open() as f:
                        rows = list(csv.DictReader(f))
                    events = [(r["event"], r["side"]) for r in rows]
                    self.assertIn(("LOSS_BYPASS", "ON"), events)
                    self.assertIn(("LOSS_BYPASS", "OFF"), events)
            finally:
                backtest.ROOT = orig_bt_root
                server.ROOT = orig_srv_root
                server.CLOUD_STATE_DB = orig_db
                server.LIVE_TRADES = orig_srv_trades
                bot.LIVE_TRADES = orig_bot_trades
                risk.JOURNAL = orig_risk_journal
                server.PENDING_FILE = orig_pending
                server._oms_state_path = orig_oms
                bot._oms_state_path = orig_bot_oms
                server.load_env = orig_load_env
                os.environ.clear()
                os.environ.update(orig_env)

    def test_backtest_live_parity_6_fixes(self):
        # Fix 1: _trail_positions locks Usual leg to breakeven when breakeven_after_bricks is reached or 2 legs open
        updated = []

        class MockTrailCap:
            def update_stop(self, deal_id, new_stop, profit_level=None):
                updated.append((deal_id, new_stop, profit_level))

        dual_positions = [
            {"position": {"dealId": "U-1", "direction": "BUY", "level": 3800.0, "stopLevel": 3770.0, "profitLevel": 3830.0}},
            {"position": {"dealId": "R-1", "direction": "BUY", "level": 3810.0, "stopLevel": 3770.0, "profitLevel": None}},
        ]
        t_cfg = {
            "epic": "GOLD",
            "tick": 0.01,
            "strategy": {"dual_order_mode": True},
            "risk": {"safety_stop_bricks": 3.0, "breakeven_after_bricks": 1.5},
        }
        # At last_close = 3815 (+1.5B for U-1, +0.5B for R-1), U-1 ratchets to breakeven 3800.0 while R-1 trails to 3785.0
        bot._trail_positions(MockTrailCap(), dual_positions, 1, 3815.0, 10.0, t_cfg, bar_close=3815.0)
        self.assertEqual(updated, [("U-1", 3800.0, 3830.0), ("R-1", 3785.0, None)])

        # Fix 4: _process_pending_runner detects already-filled 2nd leg and does not open a 3rd duplicate leg
        opened_calls = []

        class MockRunCap:
            def open_position(self, *a, **kw):
                opened_calls.append((a, kw))
                return {"dealId": "DUP-3", "dealStatus": "ACCEPTED", "level": 3810.0}

        bot._PENDING_RUNNERS["GOLD"] = {
            "side": 1,
            "action": "BUY",
            "target_px": 3810.0,
            "size": 1.0,
            "run_id": 5,
            "working_order_id": "WO-1",
        }
        dummy_eng = MagicMock(red_run=0, green_run=2, st_dir=1, last_close=3815.0)
        orig_bt = bot.LIVE_TRADES
        with tempfile.TemporaryDirectory() as tmpd:
            bot.LIVE_TRADES = Path(tmpd) / "live_trades.csv"
            try:
                filled_new = bot._process_pending_runner(
                    MockRunCap(), t_cfg, "GOLD", dummy_eng, 3815.0, 10.0, None, True, existing_leg_count=2
                )
                self.assertFalse(filled_new)
                self.assertNotIn("GOLD", bot._PENDING_RUNNERS)
                self.assertEqual(len(opened_calls), 0)
            finally:
                bot.LIVE_TRADES = orig_bt
                bot._PENDING_RUNNERS.clear()

        # Fix 6: Post-NY close overnight dead zone (21:00-24:00 UTC) blocked when asian session is disabled
        late_ny = datetime(2026, 11, 19, 22, 30, tzinfo=timezone.utc)
        lon_ny_cfg = {
            "risk": {
                "rollover_blackout": False,
                "sessions": {"asian": False, "london": True, "new_york": True},
            }
        }
        ok_late, why_late = check_entry_allowed(lon_ny_cfg, 100.0, 100.1, 10.0, now=late_ny)
        self.assertFalse(ok_late)
        self.assertIn("Post-NY close", why_late)


if __name__ == "__main__":
    unittest.main()
