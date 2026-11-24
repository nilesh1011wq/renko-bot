"""
History Analytics Engine for Renko ATR Pro Cockpit.

Supports multi-account, multi-broker historical trade synchronization, 
custom date-range filtering (day-to-day, 24h, custom windows), 
realized risk-to-reward / win rate / profit factor calculations, 
and currency/asset-wise performance attribution.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.resolve()
JOURNAL = ROOT / "journal"
LIVE_TRADES_CSV = JOURNAL / "live_trades.csv"
CLOUD_DB = Path(os.environ.get("RENDER_DISK_PATH", str(JOURNAL))) / "cloud_state.sqlite"

GST_TZ = timezone(timedelta(hours=4), name="GST")
_ANALYTICS_LOCK = threading.Lock()


@dataclass
class TradeRecord:
    deal_id: str
    broker: str
    account_id: str
    account_name: str
    epic: str
    direction: str  # BUY or SELL
    size: float
    open_price: float
    close_price: float
    stop_level: float | None
    pnl: float
    currency: str
    opened_at_utc: str
    closed_at_utc: str
    exit_type: str = "CLOSED"  # SL, TP, STRATEGY_EXIT, OPPOSITE_SIGNAL, MANUAL
    note: str = ""

    @property
    def opened_at_gst(self) -> str:
        return _utc_to_gst_str(self.opened_at_utc)

    @property
    def closed_at_gst(self) -> str:
        return _utc_to_gst_str(self.closed_at_utc)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["opened_at_gst"] = self.opened_at_gst
        d["closed_at_gst"] = self.closed_at_gst
        return d


# Pre-seeded verified trades for 2026-11-23 to ensure fresh cloud instances display instant history
SEED_TRADES_23RD = [
    TradeRecord('00000000-75bb-c3c9-049d-4da50055311e', 'CAPITAL', 'capital_demo', 'Capital.com Demo (#332507)', 'BTCUSD', 'BUY', 0.034, 60974.85, 60624.35, 60625.0, -11.92, 'USD', '2026-11-23T01:02:33Z', '2026-11-23T02:08:52Z', 'SL_HIT', 'Safety stop hit'),
    TradeRecord('00000000-75be-b472-049d-4da50055311e', 'CAPITAL', 'capital_demo', 'Capital.com Demo (#332507)', 'SILVER', 'BUY', 52.0, 42.199, 41.573, 41.575, -32.55, 'USD', '2026-11-23T00:51:03Z', '2026-11-23T02:30:54Z', 'SL_HIT', 'Safety stop hit'),
    TradeRecord('00000000-75bf-08d4-049d-4da50055311e', 'CAPITAL', 'capital_demo', 'Capital.com Demo (#332507)', 'US30', 'BUY', 0.26, 48618.90, 48875.60, 48878.0, 66.74, 'USD', '2026-11-23T00:31:08Z', '2026-11-23T02:37:58Z', 'TRAILING_STOP_LOCK', 'Trailing stop ratchet'),
    TradeRecord('00000000-75c2-29c6-049d-4da50055311e', 'CAPITAL', 'capital_demo', 'Capital.com Demo (#332507)', 'SILVER', 'BUY', 57.1, 42.775, 42.633, 42.44, -8.11, 'USD', '2026-11-23T05:44:04Z', '2026-11-23T07:51:31Z', 'DYNAMIC_STOP_CUT', 'Tightened stop cut'),
    TradeRecord('00000000-75c4-51e9-049d-4da50055311e', 'CAPITAL', 'capital_demo', 'Capital.com Demo (#332507)', 'OIL_CRUDE', 'SELL', 52.6, 82.787, 83.147, 83.33, -18.94, 'USD', '2026-11-23T09:45:32Z', '2026-11-23T09:50:03Z', 'REVERSAL_CUT', 'Quick reversal cut'),
    TradeRecord('00000000-75c2-c18c-049d-4da50055311e', 'CAPITAL', 'capital_demo', 'Capital.com Demo (#332507)', 'ETHUSD', 'BUY', 0.684, 1724.18, 1718.44, 1680.0, -3.93, 'USD', '2026-11-23T07:03:14Z', '2026-11-23T09:55:33Z', 'OPPOSITE_SIGNAL', 'Counter signal cut'),
    TradeRecord('00000000-75c2-c169-049d-4da50055311e', 'CAPITAL', 'capital_demo', 'Capital.com Demo (#332507)', 'US100', 'BUY', 0.08, 25773.10, 25947.00, 25862.5, 13.91, 'USD', '2026-11-23T07:03:09Z', '2026-11-23T11:54:31Z', 'TRAILING_STOP_LOCK', 'Trailing stop lock'),
    TradeRecord('00000000-75c5-a9a5-049d-4da50055311e', 'CAPITAL', 'capital_demo', 'Capital.com Demo (#332507)', 'US30', 'BUY', 0.25, 49047.80, 48991.70, 48946.0, -14.03, 'USD', '2026-11-23T12:22:33Z', '2026-11-23T12:41:09Z', 'OPPOSITE_BRICK_EXIT', 'Opposite brick exit'),
    TradeRecord('00000000-75c3-3e6d-049d-4da50055311e', 'CAPITAL', 'capital_demo', 'Capital.com Demo (#332507)', 'SILVER', 'BUY', 57.1, 42.675, 42.162, 42.04, -29.29, 'USD', '2026-11-23T07:55:33Z', '2026-11-23T12:52:31Z', 'OPPOSITE_BRICK_EXIT', 'Opposite brick exit'),
    TradeRecord('00000000-75c6-30a1-049d-4da50055311e', 'CAPITAL', 'capital_demo', 'Capital.com Demo (#332507)', 'SILVER', 'BUY', 57.1, 42.459, 41.998, 41.853, -26.32, 'USD', '2026-11-23T13:15:04Z', '2026-11-23T13:53:03Z', 'SAFETY_STOP_HIT', 'Safety stop cut'),
]


def _utc_to_gst_str(utc_iso: str) -> str:
    if not utc_iso:
        return ""
    try:
        clean = utc_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(GST_TZ).strftime("%Y-%m-%d %H:%M:%S GST")
    except Exception:
        return utc_iso


def init_trade_history_table(db_path: Path | None = None):
    p = db_path or CLOUD_DB
    with _ANALYTICS_LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p))
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS unified_trade_history (
                    deal_id TEXT PRIMARY KEY,
                    broker TEXT,
                    account_id TEXT,
                    account_name TEXT,
                    epic TEXT,
                    direction TEXT,
                    size REAL,
                    open_price REAL,
                    close_price REAL,
                    stop_level REAL,
                    pnl REAL,
                    currency TEXT,
                    opened_at_utc TEXT,
                    closed_at_utc TEXT,
                    exit_type TEXT,
                    note TEXT,
                    updated_at TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_uth_acct ON unified_trade_history (broker, account_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_uth_closed ON unified_trade_history (closed_at_utc)")

            # Auto-seed default cloud DB if table is empty
            if (db_path is None or db_path == CLOUD_DB):
                cur = conn.execute("SELECT COUNT(*) FROM unified_trade_history")
                if cur.fetchone()[0] == 0:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    for t in SEED_TRADES_23RD:
                        conn.execute("""
                            INSERT OR REPLACE INTO unified_trade_history (
                                deal_id, broker, account_id, account_name, epic,
                                direction, size, open_price, close_price, stop_level,
                                pnl, currency, opened_at_utc, closed_at_utc, exit_type, note, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            t.deal_id, t.broker, t.account_id, t.account_name, t.epic,
                            t.direction, t.size, t.open_price, t.close_price, t.stop_level,
                            t.pnl, t.currency, t.opened_at_utc, t.closed_at_utc, t.exit_type, t.note, now_iso
                        ))
            conn.commit()
        finally:
            conn.close()


def save_trades_to_history(trades: list[TradeRecord], db_path: Path | None = None):
    if not trades:
        return
    p = db_path or CLOUD_DB
    init_trade_history_table(p)
    now_iso = datetime.now(timezone.utc).isoformat()
    with _ANALYTICS_LOCK:
        conn = sqlite3.connect(str(p))
        try:
            for t in trades:
                conn.execute("""
                    INSERT OR REPLACE INTO unified_trade_history (
                        deal_id, broker, account_id, account_name, epic,
                        direction, size, open_price, close_price, stop_level,
                        pnl, currency, opened_at_utc, closed_at_utc, exit_type, note, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    t.deal_id, t.broker, t.account_id, t.account_name, t.epic,
                    t.direction, t.size, t.open_price, t.close_price, t.stop_level,
                    t.pnl, t.currency, t.opened_at_utc, t.closed_at_utc, t.exit_type, t.note, now_iso
                ))
            conn.commit()
        finally:
            conn.close()


def query_raw_trades(
    broker: str | None = None,
    account_id: str | None = None,
    from_utc: datetime | None = None,
    to_utc: datetime | None = None,
    db_path: Path | None = None,
) -> list[TradeRecord]:
    p = db_path or CLOUD_DB
    init_trade_history_table(p)
    query = "SELECT deal_id, broker, account_id, account_name, epic, direction, size, open_price, close_price, stop_level, pnl, currency, opened_at_utc, closed_at_utc, exit_type, note FROM unified_trade_history WHERE 1=1"
    params: list[Any] = []

    if broker and broker != "ALL":
        query += " AND broker = ?"
        params.append(broker)
    if account_id and account_id != "ALL":
        query += " AND account_id = ?"
        params.append(account_id)
    if from_utc:
        query += " AND closed_at_utc >= ?"
        params.append(from_utc.strftime("%Y-%m-%dT%H:%M:%SZ"))
    if to_utc:
        query += " AND closed_at_utc <= ?"
        params.append(to_utc.strftime("%Y-%m-%dT%H:%M:%SZ"))

    query += " ORDER BY closed_at_utc DESC"

    records = []
    with _ANALYTICS_LOCK:
        conn = sqlite3.connect(str(p))
        try:
            cur = conn.execute(query, params)
            for r in cur.fetchall():
                records.append(TradeRecord(
                    deal_id=r[0], broker=r[1], account_id=r[2], account_name=r[3],
                    epic=r[4], direction=r[5], size=r[6], open_price=r[7], close_price=r[8],
                    stop_level=r[9], pnl=r[10], currency=r[11], opened_at_utc=r[12],
                    closed_at_utc=r[13], exit_type=r[14], note=r[15]
                ))
        finally:
            conn.close()
    return records


def sync_capital_transactions(cap, account_id: str = "default_demo", account_name: str = "Capital.com Demo") -> list[TradeRecord]:
    """Fetch completed trades directly from Capital.com API and save them into the unified history database."""
    try:
        from_str = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT00:00:00")
        tx_res = cap._req("GET", f"/history/transactions?from={from_str}&type=TRADE")
        tx_list = tx_res.get("transactions", [])
        if not tx_list:
            return []

        act_res = cap._req("GET", f"/history/activity?from={from_str}&detailed=true")
        activities = act_res.get("activities", [])

        # Map activities by dealId
        deal_acts: dict[str, list[dict]] = {}
        for a in activities:
            d_id = a.get("dealId")
            if d_id:
                deal_acts.setdefault(d_id, []).append(a)

        synced = []
        for t in tx_list:
            deal_id = t.get("dealId")
            if not deal_id:
                continue

            inst = t.get("instrumentName", "UNKNOWN")
            pnl = float(t.get("size", 0.0))
            curr = t.get("currency", "USD")
            close_utc = t.get("dateUtc", "")
            note = t.get("note", "")

            # Extract open info from activities
            acts = deal_acts.get(deal_id, [])
            direction = "BUY"
            size = 0.0
            open_price = 0.0
            close_price = 0.0
            stop_level = None
            open_utc = close_utc
            exit_type = "STRATEGY_EXIT"

            for a in reversed(acts):
                det = a.get("details", {})
                a_type = a.get("type")
                source = a.get("source")
                status = a.get("status")

                if a_type == "POSITION" and status == "ACCEPTED" and det.get("openPrice") is None:
                    # Opening event
                    direction = det.get("direction", "BUY")
                    size = float(det.get("size", 0.0))
                    open_price = float(det.get("level", 0.0))
                    stop_level = float(det.get("stopLevel")) if det.get("stopLevel") is not None else None
                    open_utc = a.get("dateUTC", close_utc)

                if a_type == "POSITION" and status == "ACCEPTED" and det.get("openPrice") is not None:
                    # Closing event
                    close_price = float(det.get("level", 0.0))
                    if source == "SL":
                        exit_type = "SL_HIT"
                    elif "EDIT" in str(acts):
                        exit_type = "TRAILING_STOP_OR_RATCHET"
                    else:
                        exit_type = "STRATEGY_EXIT"

            synced.append(TradeRecord(
                deal_id=deal_id,
                broker="CAPITAL",
                account_id=account_id,
                account_name=account_name,
                epic=inst,
                direction=direction,
                size=size,
                open_price=open_price,
                close_price=close_price,
                stop_level=stop_level,
                pnl=pnl,
                currency=curr,
                opened_at_utc=open_utc,
                closed_at_utc=close_utc,
                exit_type=exit_type,
                note=note
            ))

        save_trades_to_history(synced)
        return synced
    except Exception as e:
        return []


def compute_performance_metrics(trades: list[TradeRecord]) -> dict[str, Any]:
    """Calculate institutional-grade metrics: win rate, profit factor, risk-to-reward, and asset breakdown."""
    total = len(trades)
    if total == 0:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_%": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "net_pnl": 0.0,
            "profit_factor": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "risk_reward_ratio": 0.0,
            "largest_win": 0.0,
            "largest_loss": 0.0,
            "asset_breakdown": [],
            "trades": [],
        }

    wins_list = [t for t in trades if t.pnl > 0]
    losses_list = [t for t in trades if t.pnl <= 0]

    wins = len(wins_list)
    losses = len(losses_list)
    win_rate = round((wins / total) * 100, 1)

    gross_profit = round(sum(t.pnl for t in wins_list), 2)
    gross_loss = round(abs(sum(t.pnl for t in losses_list)), 2)
    net_pnl = round(gross_profit - gross_loss, 2)

    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    avg_win = round(gross_profit / wins, 2) if wins > 0 else 0.0
    avg_loss = round(gross_loss / losses, 2) if losses > 0 else 0.0
    rr_ratio = round(avg_win / avg_loss, 2) if avg_loss > 0 else 0.0

    largest_win = round(max((t.pnl for t in trades), default=0.0), 2)
    largest_loss = round(min((t.pnl for t in trades), default=0.0), 2)

    # Asset / Currency breakdown
    by_epic: dict[str, list[TradeRecord]] = {}
    for t in trades:
        by_epic.setdefault(t.epic, []).append(t)

    asset_breakdown = []
    for epic, e_trades in by_epic.items():
        e_total = len(e_trades)
        e_wins_list = [x for x in e_trades if x.pnl > 0]
        e_loss_list = [x for x in e_trades if x.pnl <= 0]
        e_w = len(e_wins_list)
        e_l = len(e_loss_list)
        e_wr = round((e_w / e_total) * 100, 1)
        e_gp = round(sum(x.pnl for x in e_wins_list), 2)
        e_gl = round(abs(sum(x.pnl for x in e_loss_list)), 2)
        e_net = round(e_gp - e_gl, 2)
        e_pf = round(e_gp / e_gl, 2) if e_gl > 0 else (999.0 if e_gp > 0 else 0.0)
        e_aw = round(e_gp / e_w, 2) if e_w > 0 else 0.0
        e_al = round(e_gl / e_l, 2) if e_l > 0 else 0.0
        e_rr = round(e_aw / e_al, 2) if e_al > 0 else 0.0

        asset_breakdown.append({
            "epic": epic,
            "total_trades": e_total,
            "wins": e_w,
            "losses": e_l,
            "win_rate_%": e_wr,
            "net_pnl": e_net,
            "gross_profit": e_gp,
            "gross_loss": e_gl,
            "profit_factor": e_pf,
            "avg_win": e_aw,
            "avg_loss": e_al,
            "risk_reward_ratio": e_rr,
        })

    # Sort breakdown by net_pnl descending
    asset_breakdown.sort(key=lambda x: x["net_pnl"], reverse=True)

    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate_%": win_rate,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_pnl": net_pnl,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "risk_reward_ratio": rr_ratio,
        "largest_win": largest_win,
        "largest_loss": largest_loss,
        "asset_breakdown": asset_breakdown,
        "trades": [t.to_dict() for t in trades],
    }


def get_filtered_trade_analytics(
    account_id: str = "capital_demo",
    preset: str = "today",
    custom_from: str | None = None,
    custom_to: str | None = None,
    sync: bool = False,
    cap: Any = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Calculate and return full performance analytics for a specific account and date window."""
    reg = get_accounts_registry()
    acct = next((a for a in reg if a["id"] == account_id), reg[0])

    broker_target = acct["broker"]
    acct_target = acct["id"] if acct["id"] != "all_accounts" else "ALL"

    # Sync live Capital transactions if requested
    if sync and cap and broker_target == "CAPITAL":
        try:
            sync_capital_transactions(cap, account_id=acct["id"], account_name=acct["name"])
        except Exception:
            pass

    # Determine date range
    now_gst = datetime.now(GST_TZ)
    from_utc = None
    to_utc = None
    from_date_str = ""
    to_date_str = ""
    label = preset.upper()

    if custom_from:
        try:
            if len(custom_from) == 10:
                dt_from = datetime.strptime(custom_from, "%Y-%m-%d").replace(hour=0, minute=0, second=0, tzinfo=GST_TZ)
            else:
                dt_from = datetime.fromisoformat(custom_from)
                if dt_from.tzinfo is None:
                    dt_from = dt_from.replace(tzinfo=GST_TZ)
            from_utc = dt_from.astimezone(timezone.utc)
            from_date_str = custom_from
            label = f"From {custom_from}"
        except Exception:
            pass

    if custom_to:
        try:
            if len(custom_to) == 10:
                dt_to = datetime.strptime(custom_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=GST_TZ)
            else:
                dt_to = datetime.fromisoformat(custom_to)
                if dt_to.tzinfo is None:
                    dt_to = dt_to.replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=GST_TZ)
            to_utc = dt_to.astimezone(timezone.utc)
            to_date_str = custom_to
            label = f"{label} to {custom_to}" if custom_from else f"Up to {custom_to}"
        except Exception:
            pass

    if not custom_from and not custom_to:
        if preset == "today":
            start_gst = now_gst.replace(hour=0, minute=0, second=0, microsecond=0)
            from_utc = start_gst.astimezone(timezone.utc)
            from_date_str = now_gst.strftime("%Y-%m-%d")
            to_date_str = now_gst.strftime("%Y-%m-%d")
            label = f"Today ({from_date_str} GST)"
        elif preset == "yesterday":
            end_gst = now_gst.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(microseconds=1)
            start_gst = end_gst.replace(hour=0, minute=0, second=0, microsecond=0)
            from_utc = start_gst.astimezone(timezone.utc)
            to_utc = end_gst.astimezone(timezone.utc)
            from_date_str = start_gst.strftime("%Y-%m-%d")
            to_date_str = start_gst.strftime("%Y-%m-%d")
            label = f"Yesterday ({from_date_str} GST)"
        elif preset == "7d":
            start_gst = (now_gst - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
            from_utc = start_gst.astimezone(timezone.utc)
            from_date_str = start_gst.strftime("%Y-%m-%d")
            to_date_str = now_gst.strftime("%Y-%m-%d")
            label = "Last 7 Days"
        elif preset == "30d":
            start_gst = (now_gst - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
            from_utc = start_gst.astimezone(timezone.utc)
            from_date_str = start_gst.strftime("%Y-%m-%d")
            to_date_str = now_gst.strftime("%Y-%m-%d")
            label = "Last 30 Days"
        elif preset == "all":
            from_utc = None
            to_utc = None
            from_date_str = ""
            to_date_str = now_gst.strftime("%Y-%m-%d")
            label = "All Time History"

    # Query trades
    trades = query_raw_trades(
        broker=broker_target,
        account_id=acct_target,
        from_utc=from_utc,
        to_utc=to_utc,
        db_path=db_path,
    )

    perf = compute_performance_metrics(trades)
    perf["account"] = acct
    perf["filter"] = {
        "preset": preset,
        "label": label,
        "from_date": from_date_str,
        "to_date": to_date_str,
        "from_utc": from_utc.isoformat() if from_utc else None,
        "to_utc": to_utc.isoformat() if to_utc else None,
        "from_gst": from_utc.astimezone(GST_TZ).strftime("%Y-%m-%d %H:%M:%S GST") if from_utc else "Start",
        "to_gst": to_utc.astimezone(GST_TZ).strftime("%Y-%m-%d %H:%M:%S GST") if to_utc else "Now",
    }
    return perf


def get_accounts_registry() -> list[dict[str, Any]]:
    """Return all configured accounts for the multi-account cockpit."""
    accounts = [
        {
            "id": "capital_demo",
            "name": "Capital.com Demo (#332507)",
            "broker": "CAPITAL",
            "env": "demo",
            "type": "CFD Demo",
            "is_active": True,
        },
        {
            "id": "capital_live",
            "name": "Capital.com Live Account",
            "broker": "CAPITAL",
            "env": "live",
            "type": "CFD Live",
            "is_active": False,
        },
        {
            "id": "alpha_direct_mt5",
            "name": "Alpha Direct Prop ($10,000 MT5)",
            "broker": "MT5",
            "env": "demo",
            "type": "Prop Direct",
            "is_active": False,
        },
        {
            "id": "all_accounts",
            "name": "🌐 All Combined Accounts",
            "broker": "ALL",
            "env": "all",
            "type": "Aggregated View",
            "is_active": False,
        }
    ]
    return accounts
