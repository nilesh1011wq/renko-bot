"""Generate the McKinsey-Level Master Guide HTML & print-ready executive manual
for the Renko ATR Pro & Capital.com Cloud Cockpit (UAE GST / UTC+4).
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import html
from pathlib import Path

from backtest import INSTRUMENT_SPECS, load_config
import server

ROOT = Path(__file__).parent
OUT_HTML = ROOT / "static" / "guide.html"

SCREENSHOTS = {
    "overview": Path(
        "/Users/imac/.gemini/antigravity/brain/9aa51ede-d907-4d63-9d9c-d7f4b2f4510a/"
        ".user_uploaded/media_1794678420135_404d7e3d.jpg"
    ),
    "journal": Path(
        "/Users/imac/.gemini/antigravity/brain/9aa51ede-d907-4d63-9d9c-d7f4b2f4510a/"
        ".user_uploaded/media_1794666173745_9fc51a70.jpg"
    ),
    "scanner": Path(
        "/Users/imac/.gemini/antigravity/brain/9aa51ede-d907-4d63-9d9c-d7f4b2f4510a/"
        ".user_uploaded/media_1794664889633_afb4a287.jpg"
    ),
}

GST_TZ = timezone(timedelta(hours=4), name="GST")


def _b64_img(p: Path) -> str:
    if not p.exists():
        return ""
    raw = p.read_bytes()
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


def _svg_renko_vs_candle() -> str:
    return """
    <svg viewBox="0 0 820 230" width="100%" style="background:#f8fafc;border:1px solid #cbd5e1;border-radius:8px;">
      <text x="20" y="24" font-size="12" font-weight="700" fill="#0b1f3a">Time-Based Candlestick Chart (Noisy)</text>
      <text x="430" y="24" font-size="12" font-weight="700" fill="#0284c7">Renko ATR Brick Engine (Pure Price Action)</text>
      <line x1="410" y1="12" x2="410" y2="215" stroke="#cbd5e1" stroke-dasharray="4 4"/>
      <!-- Left: noisy candlesticks -->
      <line x1="45" y1="70" x2="45" y2="170" stroke="#64748b" stroke-width="2"/><rect x="38" y="95" width="14" height="45" fill="#ef4444"/>
      <line x1="78" y1="85" x2="78" y2="180" stroke="#64748b" stroke-width="2"/><rect x="71" y="105" width="14" height="40" fill="#10b981"/>
      <line x1="111" y1="60" x2="111" y2="165" stroke="#64748b" stroke-width="2"/><rect x="104" y="88" width="14" height="52" fill="#ef4444"/>
      <line x1="144" y1="90" x2="144" y2="155" stroke="#64748b" stroke-width="2"/><rect x="137" y="102" width="14" height="25" fill="#10b981"/>
      <line x1="177" y1="80" x2="177" y2="160" stroke="#64748b" stroke-width="2"/><rect x="170" y="96" width="14" height="35" fill="#ef4444"/>
      <line x1="210" y1="75" x2="210" y2="145" stroke="#64748b" stroke-width="2"/><rect x="203" y="90" width="14" height="30" fill="#10b981"/>
      <line x1="243" y1="68" x2="243" y2="152" stroke="#64748b" stroke-width="2"/><rect x="236" y="85" width="14" height="42" fill="#ef4444"/>
      <line x1="276" y1="55" x2="276" y2="128" stroke="#64748b" stroke-width="2"/><rect x="269" y="70" width="14" height="40" fill="#10b981"/>
      <line x1="309" y1="45" x2="309" y2="115" stroke="#64748b" stroke-width="2"/><rect x="302" y="55" width="14" height="38" fill="#10b981"/>
      <line x1="342" y1="40" x2="342" y2="105" stroke="#64748b" stroke-width="2"/><rect x="335" y="50" width="14" height="28" fill="#ef4444"/>
      <text x="20" y="205" font-size="10.5" fill="#64748b">Every 1 min prints a candle even if price goes sideways (whipsaws).</text>
      <!-- Right: clean Renko + Supertrend -->
      <rect x="435" y="140" width="34" height="26" fill="#ef4444" stroke="#b91c1c" rx="2"/>
      <rect x="473" y="140" width="34" height="26" fill="#10b981" stroke="#047857" rx="2"/>
      <rect x="511" y="114" width="34" height="26" fill="#10b981" stroke="#047857" rx="2"/>
      <rect x="549" y="88" width="34" height="26" fill="#10b981" stroke="#047857" rx="2"/>
      <rect x="587" y="62" width="34" height="26" fill="#10b981" stroke="#047857" rx="2"/>
      <rect x="625" y="62" width="34" height="26" fill="#10b981" stroke="#047857" opacity="0.25" stroke-dasharray="3 3"/>
      <rect x="663" y="88" width="34" height="26" fill="#ef4444" stroke="#b91c1c" rx="2"/>
      <rect x="701" y="114" width="34" height="26" fill="#ef4444" stroke="#b91c1c" rx="2"/>
      <!-- Supertrend staircase -->
      <polyline points="435,134 473,134 473,174 511,174 511,150 587,150 587,124 663,124" fill="none" stroke="#059669" stroke-width="2.5"/>
      <polyline points="663,56 750,56" fill="none" stroke="#e11d48" stroke-width="2.5"/>
      <text x="485" y="192" font-size="10" font-weight="700" fill="#047857">+1 Brick Continuation</text>
      <text x="645" y="48" font-size="10" font-weight="700" fill="#b91c1c">2-Brick Reversal Rule</text>
      <text x="430" y="205" font-size="10.5" fill="#0b1f3a">New brick ONLY prints when price moves ±1 Brick Size (ATR / Divisor).</text>
    </svg>
    """


def _svg_model_e_lifecycle() -> str:
    return """
    <svg viewBox="0 0 820 220" width="100%" style="background:#f8fafc;border:1px solid #cbd5e1;border-radius:8px;">
      <text x="20" y="24" font-size="12.5" font-weight="800" fill="#0b1f3a">Model E Dual-Order Trade Lifecycle (Usual 3B-TP + Runner +1B)</text>
      <!-- Steps -->
      <rect x="20" y="45" width="135" height="72" rx="6" fill="#e0f2fe" stroke="#0284c7" stroke-width="1.5"/>
      <text x="87" y="66" text-anchor="middle" font-size="11" font-weight="700" fill="#0369a1">1. Signal Fires</text>
      <text x="87" y="83" text-anchor="middle" font-size="9.5" fill="#0f172a">2 Green Bricks +</text>
      <text x="87" y="96" text-anchor="middle" font-size="9.5" fill="#0f172a">Supertrend BULL</text>
      <text x="87" y="109" text-anchor="middle" font-size="9.5" fill="#0369a1">All 9 Guards Pass</text>

      <line x1="155" y1="81" x2="178" y2="81" stroke="#475569" stroke-width="2" marker-end="url(#arr)"/>

      <rect x="180" y="45" width="140" height="72" rx="6" fill="#fef3c7" stroke="#d97706" stroke-width="1.5"/>
      <text x="250" y="66" text-anchor="middle" font-size="11" font-weight="700" fill="#b45309">2. 5m Confirm Gate</text>
      <text x="250" y="83" text-anchor="middle" font-size="9.5" fill="#0f172a">Holds 5 minutes</text>
      <text x="250" y="96" text-anchor="middle" font-size="9.5" fill="#0f172a">Anti-fakeout shield</text>
      <text x="250" y="109" text-anchor="middle" font-size="9.5" fill="#b45309">Slippage ≤ 0.35 Brk</text>

      <line x1="320" y1="81" x2="343" y2="81" stroke="#475569" stroke-width="2"/>

      <rect x="345" y="42" width="215" height="36" rx="6" fill="#dcfce7" stroke="#16a34a" stroke-width="1.5"/>
      <text x="452" y="59" text-anchor="middle" font-size="10.5" font-weight="700" fill="#166534">Leg 1: USUAL (Market Order)</text>
      <text x="452" y="72" text-anchor="middle" font-size="9.5" fill="#14532d">Fixed Take Profit = +3 Bricks | Stop = -3B</text>

      <rect x="345" y="86" width="215" height="36" rx="6" fill="#f3e8ff" stroke="#9333ea" stroke-width="1.5"/>
      <text x="452" y="103" text-anchor="middle" font-size="10.5" font-weight="700" fill="#6b21a8">Leg 2: RUNNER (+1B Native STOP)</text>
      <text x="452" y="116" text-anchor="middle" font-size="9.5" fill="#581c87">Fills ONLY if trend moves +1B higher</text>

      <line x1="560" y1="81" x2="583" y2="81" stroke="#475569" stroke-width="2"/>

      <rect x="585" y="45" width="215" height="72" rx="6" fill="#0b1f3a" stroke="#0284c7" stroke-width="1.5"/>
      <text x="692" y="66" text-anchor="middle" font-size="11" font-weight="700" fill="#38bdf8">3. Breakeven Ratchet</text>
      <text x="692" y="83" text-anchor="middle" font-size="9.5" fill="#f8fafc">When Usual +3B TP banks profit:</text>
      <text x="692" y="96" text-anchor="middle" font-size="9.5" fill="#34d399">Runner SL locks at Entry (Risk-Free!)</text>
      <text x="692" y="109" text-anchor="middle" font-size="9.5" fill="#cbd5e1">Rides big trend until opposite brick</text>

      <!-- Bottom payoff bar -->
      <rect x="20" y="138" width="780" height="62" rx="6" fill="#ffffff" stroke="#cbd5e1"/>
      <text x="35" y="158" font-size="11" font-weight="700" fill="#0b1f3a">Why Two Legs Beat One Order ("Have Your Cake &amp; Eat It Too"):</text>
      <text x="35" y="175" font-size="10" fill="#334155">• Sideways / chop reversal? Only Leg 1 entered (Runner STOP order never triggered &amp; auto-cancels = HALF loss!).</text>
      <text x="35" y="191" font-size="10" fill="#334155">• Normal +3B swing? Leg 1 banks guaranteed cash at TP. Huge +12B runaway rally? Leg 2 rides the entire wave risk-free!</text>
    </svg>
    """


def _svg_equity_chart(bt: dict) -> str:
    curve = bt.get("equity_curve", [])
    raw_curve = bt.get("raw_curve", [])
    if len(curve) < 2:
        return ""
    w, h = 760, 210
    pad_l, pad_r, pad_t, pad_b = 56, 20, 22, 26
    net_vals = [float(p.get("net", 0.0)) for p in curve]
    gross_vals = [float(p.get("gross", 0.0)) for p in curve]
    raw_vals = [float(x) for x in raw_curve] if len(raw_curve) >= 2 else [0.0]
    all_v = net_vals + gross_vals + raw_vals + [0.0]
    min_v, max_v = min(all_v), max(all_v)
    if max_v <= min_v:
        max_v = min_v + 10.0
    pad = (max_v - min_v) * 0.12
    min_v -= pad
    max_v += pad
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b

    def py(v: float) -> float:
        return pad_t + plot_h - ((v - min_v) / (max_v - min_v)) * plot_h

    def pts(vals: list[float]) -> str:
        n = max(1, len(vals) - 1)
        return " ".join(f"{pad_l + (i / n) * plot_w:.1f},{py(v):.1f}" for i, v in enumerate(vals))

    y0 = py(0.0)
    grid_lines = []
    for k in range(5):
        val = min_v + (max_v - min_v) * (k / 4)
        yk = py(val)
        grid_lines.append(
            f'<line x1="{pad_l}" y1="{yk:.1f}" x2="{w - pad_r}" y2="{yk:.1f}" stroke="#e2e8f0"/>'
            f'<text x="6" y="{yk + 3:.1f}" font-size="9.5" fill="#64748b">${val:.0f}</text>'
        )

    return f"""
    <svg viewBox="0 0 {w} {h}" width="100%" style="background:#ffffff;border:1px solid #cbd5e1;border-radius:8px;">
      <text x="{pad_l}" y="15" font-size="11.5" font-weight="700" fill="#0b1f3a">Gold Benchmark Equity Curve — Pro Model E (Green/Blue) vs Unfiltered Raw Strategy (Amber Dashed)</text>
      {''.join(grid_lines)}
      <line x1="{pad_l}" y1="{y0:.1f}" x2="{w - pad_r}" y2="{y0:.1f}" stroke="#94a3b8" stroke-dasharray="4 4"/>
      <polyline points="{pts(raw_vals)}" fill="none" stroke="#d97706" stroke-width="1.8" stroke-dasharray="5 3"/>
      <polyline points="{pts(gross_vals)}" fill="none" stroke="#0284c7" stroke-width="1.6" stroke-dasharray="3 3"/>
      <polyline points="{pts(net_vals)}" fill="none" stroke="#059669" stroke-width="2.6"/>
      <rect x="{w - 230}" y="22" width="205" height="46" rx="4" fill="#f8fafc" stroke="#cbd5e1"/>
      <line x1="{w - 220}" y1="34" x2="{w - 198}" y2="34" stroke="#059669" stroke-width="2.6"/>
      <text x="{w - 192}" y="37" font-size="9.5" fill="#0b1f3a">Pro Filtered Net ({net_vals[-1]:+.2f}$)</text>
      <line x1="{w - 220}" y1="48" x2="{w - 198}" y2="48" stroke="#0284c7" stroke-width="1.6" stroke-dasharray="3 3"/>
      <text x="{w - 192}" y="51" font-size="9.5" fill="#0369a1">Pro Gross Before Spread</text>
      <line x1="{w - 220}" y1="62" x2="{w - 198}" y2="62" stroke="#d97706" stroke-width="1.8" stroke-dasharray="5 3"/>
      <text x="{w - 192}" y="65" font-size="9.5" fill="#b45309">Raw Unfiltered ({raw_vals[-1]:+.2f}$)</text>
    </svg>
    """


def _svg_tca_bars(tca: dict) -> str:
    items = tca.get("filter_breakdown", [])[:6]
    if not items:
        return ""
    w, h = 760, 175
    max_c = max(int(x.get("count", 1)) for x in items) or 1
    bars_svg = []
    y = 38
    for it in items:
        cnt = int(it.get("count", 0))
        bw = (cnt / max_c) * 430.0
        lbl = html.escape(str(it.get("reason", "")))
        bars_svg.append(
            f'<text x="16" y="{y + 13}" font-size="10.5" fill="#1e293b" font-weight="600">{lbl}</text>'
            f'<rect x="235" y="{y}" width="{bw:.1f}" height="17" rx="3" fill="#0284c7"/>'
            f'<text x="{243 + bw:.1f}" y="{y + 12.5}" font-size="10" font-weight="700" fill="#0b1f3a">{cnt} blocked signals</text>'
        )
        y += 22
    return f"""
    <svg viewBox="0 0 {w} {max(h, y + 12)}" width="100%" style="background:#f8fafc;border:1px solid #cbd5e1;border-radius:8px;">
      <text x="16" y="21" font-size="11.5" font-weight="700" fill="#0b1f3a">Institutional Guard Shield Breakdown — Bad / High-Risk Signals Automatically Vetoed</text>
      {''.join(bars_svg)}
    </svg>
    """


def _svg_uae_gst_clock() -> str:
    return """
    <svg viewBox="0 0 820 155" width="100%" style="background:#ffffff;border:1px solid #cbd5e1;border-radius:8px;">
      <text x="16" y="22" font-size="12" font-weight="800" fill="#0b1f3a">24-Hour Global Trading Clock — UAE Gulf Standard Time (GST = UTC+4)</text>
      <!-- Base timeline 00 to 24 GST -> x = 30 to 790 (width=760 -> 31.66 px/hr) -->
      <rect x="30" y="50" width="31.7" height="34" fill="#1e3a8a" opacity="0.88"/>
      <text x="45" y="71" text-anchor="middle" font-size="9" fill="#fff" font-weight="700">NY</text>
      <!-- Rollover Blackout 00:45 - 02:15 GST -->
      <rect x="53.7" y="42" width="47.5" height="48" fill="#dc2626" rx="3"/>
      <text x="77" y="64" text-anchor="middle" font-size="8.5" fill="#fff" font-weight="800">BLACKOUT</text>
      <text x="77" y="76" text-anchor="middle" font-size="8" fill="#fee2e2">00:45–02:15</text>
      <!-- Asian 04:00 - 11:00 GST (x = 156.6 to 378.3) -->
      <rect x="156.6" y="50" width="221.7" height="34" fill="#0ea5e9" opacity="0.85" rx="3"/>
      <text x="267" y="71" text-anchor="middle" font-size="10.5" fill="#fff" font-weight="700">ASIAN SESSION (04:00 – 11:00 GST)</text>
      <!-- London 11:00 - 17:00 GST (x = 378.3 to 568.3) -->
      <rect x="378.3" y="50" width="190" height="34" fill="#059669" opacity="0.9" rx="3"/>
      <text x="473" y="71" text-anchor="middle" font-size="10.5" fill="#fff" font-weight="700">LONDON SESSION (11:00 – 17:00 GST)</text>
      <!-- New York 17:00 - 01:00 GST (x = 568.3 to 790) -->
      <rect x="568.3" y="50" width="221.7" height="34" fill="#1d4ed8" opacity="0.9" rx="3"/>
      <text x="679" y="71" text-anchor="middle" font-size="10.5" fill="#fff" font-weight="700">NEW YORK (17:00 – 01:00 GST)</text>
      <!-- Macro markers -->
      <line x1="584" y1="36" x2="584" y2="96" stroke="#f59e0b" stroke-width="2.5"/>
      <text x="584" y="108" text-anchor="middle" font-size="8.5" font-weight="700" fill="#b45309">17:30 GST (CPI/NFP)</text>
      <line x1="758" y1="36" x2="758" y2="96" stroke="#f59e0b" stroke-width="2.5"/>
      <text x="742" y="108" text-anchor="middle" font-size="8.5" font-weight="700" fill="#b45309">23:00 GST (FOMC)</text>
      <line x1="93" y1="36" x2="93" y2="96" stroke="#7c3aed" stroke-width="2" stroke-dasharray="3 2"/>
      <text x="112" y="108" text-anchor="middle" font-size="8.5" font-weight="700" fill="#6d28d9">02:00 GST Digest</text>
      <!-- Hour axis -->
      <text x="30" y="135" font-size="9" fill="#64748b">00:00</text>
      <text x="156" y="135" font-size="9" fill="#64748b">04:00</text>
      <text x="283" y="135" font-size="9" fill="#64748b">08:00</text>
      <text x="378" y="135" font-size="9" fill="#64748b">11:00</text>
      <text x="568" y="135" font-size="9" fill="#64748b">17:00</text>
      <text x="726" y="135" font-size="9" fill="#64748b">22:00</text>
      <text x="772" y="135" font-size="9" fill="#64748b">24:00</text>
    </svg>
    """


def build_guide() -> Path:
    cfg = load_config()
    bt = server.serialize_backtest(cfg, use_cache=False, skip_network=True)
    sn = bt.get("stats_net", {})
    sg = bt.get("stats_gross", {})
    raw_sn = bt.get("raw_pine_stats_net", {})
    tca = bt.get("tca_attribution", {})
    ds = bt.get("data_summary", {})
    usual = tca.get("usual", {})
    runner = tca.get("runner", {})
    costs = tca.get("costs", {})
    edge = round(float(sn.get("net", 0.0)) - float(raw_sn.get("net", 0.0)), 2)
    gen_gst = datetime.now(GST_TZ).strftime("%d %b %Y · %H:%M GST (UTC+4)")

    img_overview = _b64_img(SCREENSHOTS["overview"])
    img_scanner = _b64_img(SCREENSHOTS["scanner"])
    img_journal = _b64_img(SCREENSHOTS["journal"])

    inst_rows = []
    corr_map = {
        "GOLD": "Precious Metals / USD Inverse", "SILVER": "Precious Metals / USD Inverse",
        "OIL_CRUDE": "ENERGY (Max 2)", "OIL_BRENT": "ENERGY (Max 2)",
        "EURUSD": "USD_BLOC (Max 2)", "GBPUSD": "USD_BLOC (Max 2)", "USDJPY": "USD_BLOC (Max 2)",
        "AUDUSD": "USD_BLOC (Max 2)", "USDCAD": "USD_BLOC (Max 2)", "USDCHF": "USD_BLOC (Max 2)",
        "US500": "US_INDICES (Max 2)", "US100": "US_INDICES (Max 2)", "US30": "US_INDICES (Max 2)",
        "GER40": "EU_INDICES (Max 2)", "UK100": "EU_INDICES (Max 2)", "JP225": "Asia Index",
    }
    for epic, sp in INSTRUMENT_SPECS.items():
        inst_rows.append(
            f"<tr><td><b>{epic}</b></td><td>{sp.get('name')}</td><td>{sp.get('category')}</td>"
            f"<td>{sp.get('currency')}</td><td>{sp.get('digits')}</td><td>{sp.get('fallback_size')}</td>"
            f"<td>{corr_map.get(epic, 'Independent')}</td></tr>"
        )

    glossary = [
        ("ATR (Average True Range)", "Average size of recent daily price swings using Wilder's RMA smoothing.", "Thermometer of market volatility: bigger swings = bigger Renko bricks."),
        ("Brick Size", "Exact price height of one Renko block: round_to_tick(Daily ATR(5) / ATR Divisor).", "Height of each Lego brick on the chart."),
        ("BE (Breakeven Lock)", "Moving the Runner's Stop-Loss to its entry price once Leg 1 (Usual TP) banks profit.", "Locking your seat-belt so a winning trade can NEVER turn into a loser."),
        ("Calmar Ratio", "Net P&L divided by Maximum Drawdown. Measures return per unit of worst pain.", "How many dollars you made for every $1 of deepest temporary drop."),
        ("5m Confirm (Time Confirmation)", "Waiting 5 minutes after a new signal brick prints to make sure Supertrend doesn't flip back.", "The 'count to 5' rule before jumping in — skips fakeouts."),
        ("CST / X-SECURITY-TOKEN", "Short-lived encrypted session headers issued by Capital.com on login.", "Digital security badge allowing our bot to talk to your broker."),
        ("Dual-Order (Model E)", "Splitting every confirmed entry into two legs: Usual (3B Fixed TP) + Runner (+1B STOP).", "Taking steady pocket cash early while keeping a second ticket for huge moves."),
        ("EPIC", "Capital.com's unique ticker symbol for a market (e.g. GOLD, OIL_CRUDE, US100, EURUSD).", "Market nickname code inside Capital.com."),
        ("Expectancy (R)", "(Win% × AvgWin) − (Loss% × AvgLoss) normalized per trade risk unit (R).", "Average R-multiple earned every single time you click trade."),
        ("G:x / R:y (Brick Run)", "Count of consecutive same-color Renko bricks printed (G=Green Bull, R=Red Bear).", "Winning streak counter of current bricks in one direction."),
        ("Grid-Anchor Origin", "Snapping brick boundaries cleanly to multiples of brick_size (2600, 2610, 2620).", "Lining up Lego bricks on neat graph-paper lines so everyone sees identical levels."),
        ("GST (UAE Time / UTC+4)", "Gulf Standard Time (Dubai / Abu Dhabi), 4 hours ahead of UTC.", "All clocks, logs, blackouts, and Telegram alerts now use UAE local time."),
        ("HTF Filter (8-Brick)", "8-brick Higher-Timeframe slope check requiring macro momentum alignment.", "Checking if the bigger river is flowing the same way before swimming."),
        ("MAE (Max Adverse Excursion)", "Worst unrealized dip (in bricks) a trade suffered before closing.", "Scariest red floating number you had to sit through."),
        ("MFE (Max Favorable Excursion)", "Best unrealized peak (in bricks) reached while a trade was open.", "Highest green profit peak the trade touched."),
        ("Monte Carlo Cone", "2,500 random reshufflings of your trade sequence showing P10 / P50 / P90 outcomes.", "Rolling the same dice 2,500 times to see lucky vs unlucky streaks."),
        ("OMS (Order Management System)", "Persistent JSON/SQLite state machine tracking pending confirms and Runner STOP orders.", "Bot's notebook so it never forgets active orders even if the server restarts."),
        ("Overshoot", "Extra distance beyond brick close where a volatile 1-minute candle actually finished.", "Slippage gap caused by a fast candle shooting past the brick line."),
        ("PF (Profit Factor)", "Total Gross Wins divided by Total Gross Losses (above 1.50 is strong).", "Dollars won per $1 lost."),
        ("Pip", "Standard smallest price quote increment in Forex (e.g. 0.0001 on EURUSD, 0.01 on USDJPY).", "A cent of movement for currency pairs."),
        ("Payoff Ratio (R)", "Average winning trade divided by average losing trade (Reward ÷ Risk).", "How big your average win is compared to your average loss."),
        ("Runner (+1B STOP)", "Capital.com native STOP working order placed 1 brick ahead of entry in Model E.", "Trailing hitchhiker order that ONLY boards the train if it keeps moving!"),
        ("Sharpe / Sortino Ratio", "Risk-adjusted return vs total volatility (Sharpe) or downside-only volatility (Sortino).", "Smoothness score of your equity curve: higher means steadier growth."),
        ("Spread Drag", "Cost paid to the broker between Bid (sell) and Ask (buy) on every entry and exit.", "Small toll fee paid at the bridge on every round-trip."),
        ("SSE (/api/stream)", "Server-Sent Events real-time push stream updating WebSocket prices and watcher status.", "Live scoreboard feed without pressing Refresh."),
        ("Supertrend (ST)", "Volatility staircase trend indicator computed on Renko bricks (green=Bull, red=Bear).", "Traffic light line that sits below green trends and above red trends."),
        ("TCA (Transaction Cost Analysis)", "Full attribution of Gross P&L vs Spread Drag vs Leg 1/Leg 2 vs Guard Vetoes.", "Receipt showing where every dollar was earned or saved."),
        ("UPL", "Unrealized Profit or Loss on currently open broker positions.", "Floating live profit/loss right now before closing."),
        ("VaR95 (95% Daily Value at Risk)", "5th-percentile worst daily loss across your trading distribution.", "Realistic 'bad day' loss bound 95% of days stay safer than."),
        ("WSS (WebSocket Stream)", "Persistent wss:// feed streaming millisecond Bid/Offer ticks from Capital.com.", "Live firehose of real-time market prices."),
    ]
    glossary_rows = "".join(
        f"<tr><td><b>{html.escape(term)}</b></td><td>{html.escape(pro)}</td><td>{html.escape(teen)}</td></tr>"
        for term, pro, teen in glossary
    )

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Renko ATR Cloud Cockpit — McKinsey Master Guide &amp; Playbook (UAE GST)</title>
<style>
  @page {{
    size: A4 portrait;
    margin: 14mm 14mm 16mm 14mm;
    @bottom-left {{ content: "RENKO ATR PRO CLOUD COCKPIT · UAE GST (UTC+4)"; font-size: 8pt; color: #64748b; font-family: -apple-system, sans-serif; }}
    @bottom-right {{ content: "Page " counter(page) " of " counter(pages); font-size: 8pt; color: #0b1f3a; font-weight: 700; font-family: -apple-system, sans-serif; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    color: #0f172a;
    background: #ffffff;
    margin: 0 auto;
    max-width: 960px;
    padding: 18px 24px 48px;
    line-height: 1.52;
    font-size: 10.5pt;
  }}
  .print-bar {{
    position: sticky; top: 0; z-index: 50; background: #0b1f3a; color: #fff;
    padding: 10px 16px; border-radius: 8px; margin-bottom: 18px;
    display: flex; justify-content: space-between; align-items: center;
    box-shadow: 0 4px 16px rgba(11,31,58,0.18);
  }}
  .print-btn {{
    background: #0284c7; color: #fff; border: none; border-radius: 5px;
    padding: 8px 16px; font-weight: 700; cursor: pointer; font-size: 10pt; text-decoration: none;
  }}
  @media print {{
    body {{ max-width: none; padding: 0; font-size: 9.8pt; }}
    .no-print {{ display: none !important; }}
    .page-break {{ page-break-before: always; }}
  }}
  .cover {{
    background: linear-gradient(135deg, #0b1f3a 0%, #0f294d 62%, #0284c7 100%);
    color: #ffffff; padding: 38px 34px; border-radius: 10px; margin-bottom: 24px;
  }}
  .cover .tag {{
    display: inline-block; font-size: 8.5pt; letter-spacing: 0.12em; text-transform: uppercase;
    font-weight: 800; color: #7dd3fc; border: 1px solid rgba(125,211,252,0.45);
    padding: 4px 10px; border-radius: 4px; margin-bottom: 14px;
  }}
  .cover h1 {{ margin: 0 0 10px; font-size: 25pt; line-height: 1.15; letter-spacing: -0.02em; }}
  .cover p {{ color: #e2e8f0; font-size: 11.2pt; margin: 0 0 18px; max-width: 720px; }}
  .kpi-strip {{
    display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin-top: 18px;
  }}
  .kpi-card {{
    background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.2);
    border-radius: 6px; padding: 10px 12px;
  }}
  .kpi-card .k-lbl {{ font-size: 8pt; text-transform: uppercase; color: #bae6fd; font-weight: 700; }}
  .kpi-card .k-val {{ font-size: 14pt; font-weight: 800; color: #ffffff; margin-top: 2px; font-family: ui-monospace, monospace; }}
  .sec-hdr {{
    margin: 26px 0 12px; padding-bottom: 6px; border-bottom: 2.5px solid #0b1f3a;
    display: flex; align-items: baseline; justify-content: space-between;
  }}
  .sec-num {{ color: #0284c7; font-size: 9.5pt; font-weight: 800; letter-spacing: 0.08em; text-transform: uppercase; }}
  .sec-hdr h2 {{ margin: 2px 0 0; font-size: 15.5pt; color: #0b1f3a; letter-spacing: -0.01em; }}
  .insight-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 12px 0 16px; break-inside: avoid; }}
  .box-teen {{
    background: #f0f9ff; border-left: 4px solid #0284c7; padding: 11px 14px; border-radius: 0 6px 6px 0;
  }}
  .box-quant {{
    background: #f8fafc; border-left: 4px solid #0b1f3a; padding: 11px 14px; border-radius: 0 6px 6px 0;
  }}
  .box-title {{ font-size: 8.5pt; text-transform: uppercase; font-weight: 800; letter-spacing: 0.05em; margin-bottom: 4px; color: #0369a1; }}
  .box-quant .box-title {{ color: #0b1f3a; }}
  table {{ width: 100%; border-collapse: collapse; margin: 10px 0 16px; font-size: 9.2pt; break-inside: auto; }}
  tr {{ break-inside: avoid; }}
  th {{ background: #0b1f3a; color: #ffffff; text-align: left; padding: 7px 9px; font-size: 8.5pt; text-transform: uppercase; letter-spacing: 0.04em; }}
  td {{ padding: 6.5px 9px; border-bottom: 1px solid #e2e8f0; vertical-align: top; }}
  tr:nth-child(even) td {{ background: #f8fafc; }}
  .shot-wrap {{
    border: 1.5px solid #cbd5e1; border-radius: 8px; overflow: hidden; margin: 12px 0;
    background: #0b1f3a; break-inside: avoid;
  }}
  .shot-wrap img {{ width: 100%; display: block; }}
  .shot-cap {{ background: #0b1f3a; color: #e2e8f0; padding: 7px 12px; font-size: 9pt; font-weight: 600; }}
  .pill {{ display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 8.5pt; font-weight: 700; font-family: ui-monospace, monospace; background: #e2e8f0; color: #0f172a; }}
  .pos {{ color: #059669; font-weight: 700; }}
  .neg {{ color: #dc2626; font-weight: 700; }}
  .card-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin: 12px 0; break-inside: avoid; }}
  .mini-card {{ border: 1px solid #cbd5e1; border-radius: 6px; padding: 10px 12px; background: #ffffff; }}
  .mini-card h4 {{ margin: 0 0 5px; font-size: 10pt; color: #0b1f3a; border-bottom: 1px solid #e2e8f0; padding-bottom: 4px; }}
  .mini-card p {{ margin: 0; font-size: 9pt; color: #334155; }}
</style>
</head>
<body>

<div class="print-bar no-print">
  <div><b>📘 McKinsey Master Playbook &amp; Technical Manual</b> · Renko ATR Cloud Cockpit (UAE GST / UTC+4)</div>
  <div style="display:flex; gap:8px;">
    <a class="print-btn" href="/api/guide.pdf" target="_blank">📥 Download Official PDF</a>
    <button class="print-btn" onclick="window.print()" style="background:#059669;">🖨️ Print This Guide</button>
    <a class="print-btn" href="/" style="background:#334155;">← Back to Cockpit</a>
  </div>
</div>

<!-- SECTION 1: COVER & EXECUTIVE SUMMARY -->
<section class="cover">
  <div class="tag">McKinsey-Style Executive Manual &amp; Beginner Playbook · UAE GST (UTC+4)</div>
  <h1>Renko ATR Pro &amp; Capital.com<br/>Multi-Market Cloud Trading Cockpit</h1>
  <p>A complete end-to-end guide written so clearly that a 15-year-old can start trading confidently in 10 minutes — while packing every mathematical formula, architectural diagram, risk guard, live chart, and A–Z definition demanded by an institutional quant desk.</p>
  <div style="font-size:9.5pt; color:#bae6fd;">
    <b>Cloud URL:</b> https://renko-bot.onrender.com &nbsp;|&nbsp;
    <b>Remote Bot:</b> @RENKO2026BOT (Telegram 2-Way Control) &nbsp;|&nbsp;
    <b>Global Timezone:</b> UAE Gulf Standard Time (GST = UTC+4) &nbsp;|&nbsp;
    <b>Updated:</b> {gen_gst}
  </div>
  <div class="kpi-strip">
    <div class="kpi-card"><div class="k-lbl">Pro Net P&amp;L ({ds.get('epic','GOLD')})</div><div class="k-val">${float(sn.get('net',0)):+.2f}</div></div>
    <div class="kpi-card"><div class="k-lbl">Alpha vs Raw Strategy</div><div class="k-val">${edge:+.2f}</div></div>
    <div class="kpi-card"><div class="k-lbl">Win Rate / Profit Factor</div><div class="k-val">{sn.get('win_rate_%',0)}% / {sn.get('profit_factor',0)}</div></div>
    <div class="kpi-card"><div class="k-lbl">Sharpe / Calmar Ratio</div><div class="k-val">{sn.get('sharpe',0)} / {sn.get('calmar',0)}</div></div>
    <div class="kpi-card"><div class="k-lbl">Markets Monitored 24/7</div><div class="k-val">16 Global</div></div>
  </div>
</section>

<div class="insight-grid">
  <div class="box-teen">
    <div class="box-title">🧒 The 30-Second Story (If You Are New to Trading)</div>
    Most trading apps show messy time charts that trick beginners into over-trading. This cockpit strips out time noise using <b>Renko Lego Bricks</b>, waits 5 minutes to confirm trends aren't fakeouts, splits every trade into a <b>Quick Profit Cash Register (Usual 3B-TP)</b> plus a <b>Trend Runner (+1B)</b>, guards your account with 9 safety shields, and texts your phone on Telegram (<b>@RENKO2026BOT</b>) in UAE time!
  </div>
  <div class="box-quant">
    <div class="box-title">🏦 Institutional Resolution (McKinsey Situation → Complication → Answer)</div>
    <b>Situation:</b> Naive Renko + Supertrend strategies suffer spread erosion, news-spike whipsaws, and correlated blowups.<br/>
    <b>Complication:</b> Live execution differs from idealized TradingView backtests due to intrabar fills and rollover spreads.<br/>
    <b>Resolution:</b> Native Python Model E dual-order OMS + Capital.com WebSocket/REST + 9 pre-trade shields turning a raw loser into <span class="pos">${float(sn.get('net',0)):+.2f} Net</span> (<span class="pos">${edge:+.2f} Pro Edge</span>).
  </div>
</div>

<!-- SECTION 2: HOW RENKO, ATR & SUPERTREND WORK -->
<div class="sec-hdr"><div><div class="sec-num">Section 02</div><h2>How the Strategy Brain Works: Renko Bricks, Wilder ATR &amp; Supertrend</h2></div></div>
{_svg_renko_vs_candle()}
<div class="insight-grid">
  <div class="box-teen">
    <div class="box-title">🧒 Think of Renko Like Building with Lego Blocks</div>
    On a normal 1-minute chart, a candle spawns every 60 seconds even if Gold barely moved — creating lots of zig-zag confusion. A <b>Renko Chart ignores the clock</b> and ONLY adds a new brick when price actually travels a fixed step size (<b>Brick Size</b>).
    <br/>• <b>Going Up (Continuation):</b> Price rises +1 Brick Size → prints 1 new <b class="pos">Green Brick</b>.
    <br/>• <b>Turning Down (Reversal):</b> To flip direction from Green to Red, price must drop <b>2× Brick Sizes</b> (filtering out tiny wobbles!).
  </div>
  <div class="box-quant">
    <div class="box-title">📐 Exact Quantitative Formulas (No Black Boxes)</div>
    • <b>Daily Wilder ATR(5):</b> Uses completed prior daily candles only (zero lookahead bias) with Wilder RMA smoothing.<br/>
    • <b>Dynamic Brick Size:</b> <code>brick_size = round_to_tick(Daily ATR(5) / ATR_Divisor)</code>. Gold uses Divisor <code>15</code> (~$6–$7 bricks); Scalp/FX/Indices adapt via Category Profiles.<br/>
    • <b>Supertrend (Len 1, Factor 2.1):</b> Computed natively on completed Renko brick True Ranges (ATR = brick range), producing an institutional staircase Stop/Reverse regime line.
  </div>
</div>

<!-- SECTION 3: MODEL E DUAL-ORDER ARCHITECTURE -->
<div class="page-break"></div>
<div class="sec-hdr"><div><div class="sec-num">Section 03</div><h2>Model E Dual-Order Architecture &amp; 5-Minute Confirmation Gate</h2></div></div>
{_svg_model_e_lifecycle()}
<div class="card-grid">
  <div class="mini-card">
    <h4>1️⃣ 5-Minute Confirm Clock</h4>
    <p>When 2 consecutive Renko bricks align with Supertrend, the bot does NOT rush in blindly. It starts a <b>5-minute countdown</b> (<code>pending_confirm</code>). If momentum collapses during those 5 minutes, the trade is cancelled cleanly (saving a loss!).</p>
  </div>
  <div class="mini-card">
    <h4>2️⃣ Leg 1: Usual (3B-TP) + Leg 2: Runner (+1B)</h4>
    <p>Once 5 minutes hold, <b>Leg 1 (Usual)</b> enters at market with a fixed <b>+3 Bricks Take Profit</b> and <b>3-Brick Safety Stop</b>. Simultaneously, the bot places a <b>Capital.com Native STOP Working Order (+1B)</b> — Leg 2 only fills if price breaks even higher!</p>
  </div>
  <div class="mini-card">
    <h4>3️⃣ Automatic Breakeven (BE) Lock</h4>
    <p>The moment Leg 1 hits its +3B Take Profit and banks cash, the live watcher <b>automatically moves Leg 2's Stop-Loss to its entry price</b>. From that second forward, your Runner rides massive multi-hour trends with <b>zero downside risk</b>.</p>
  </div>
</div>

<!-- SECTION 4: ANNOTATED VISUAL COCKPIT TOUR -->
<div class="sec-hdr"><div><div class="sec-num">Section 04</div><h2>Visual Cockpit Anatomy: Live Screens &amp; Where Every Button Lives</h2></div></div>
<div class="shot-wrap">
  <img src="{img_overview}" alt="Cockpit Main Workspace"/>
  <div class="shot-cap">Screenshot A — Top Telemetry Bar, Left Strategy Control Room, Central Interactive Renko Chart &amp; Multi-Market Scanner</div>
</div>
<table>
  <thead><tr><th>Zone</th><th>Element</th><th>What It Shows &amp; How To Use It</th></tr></thead>
  <tbody>
    <tr><td><b>Top Header</b></td><td>Market Dropdown, 🇦🇪 UAE TIME, Bid/Ask, Brick Size, Runs (G/R), Supertrend</td><td>Shows live UAE clock (<code>GST</code>), active market price, current green/red brick streak, and bullish/bearish trend state. Click <b>MODE</b> pill to toggle <code>FULL AUTO</code> vs <code>SEMI-AUTO</code>.</td></tr>
    <tr><td><b>Top Actions</b></td><td>📘 Master Guide, Open Studio Lab, Manage Broker, Sync Price, Watcher, Kill Switch</td><td><b>Sync Broker Price</b> pulls fresh Capital.com candles; <b>▶ Start 24/7 Watcher</b> activates cloud trading; <b>FLATTEN / KILL SWITCH</b> immediately closes every open position &amp; pauses trading.</td></tr>
    <tr><td><b>KPI Strip</b></td><td>Net P&amp;L, Pro Edge vs Raw, Win Rate, Profit Factor, Max DD, Sharpe/Calmar/VaR95</td><td>Live scoreboard comparing the filtered Model E strategy against the raw unfiltered baseline strategy.</td></tr>
    <tr><td><b>Main Chart</b></td><td>Interactive Pan/Zoom Canvas + <code>50 / 80 / 120 / ALL</code></td><td>Hover any brick for exact Open/Close/Brick Move/Supertrend/Timestamp HUD. Drag left/right to inspect history; click <b>▶▶ Real-Time</b> to snap back. Overlay toggles hide/show ST/Signals/Vetoes.</td></tr>
  </tbody>
</table>

<div class="page-break"></div>
<div class="shot-wrap">
  <img src="{img_scanner}" alt="16 Market Scanner & Presets"/>
  <div class="shot-cap">Screenshot B — 16-Market Strategy Scanner with Per-Asset Category Profiles &amp; One-Click Auto/Pause Controls</div>
</div>
<div class="shot-wrap">
  <img src="{img_journal}" alt="Trade Journal & Brick Hover HUD"/>
  <div class="shot-cap">Screenshot C — Trade Journal with Executed Model E Legs (<code>USUAL 3B-TP</code> / <code>RUNNER +1B</code>), Amber <code>FLT-*</code> Protected Signals &amp; Hover HUD</div>
</div>

<!-- SECTION 5: COMPLETE GUIDE TO ALL 9 BOTTOM DOCK TABS -->
<div class="page-break"></div>
<div class="sec-hdr"><div><div class="sec-num">Section 05</div><h2>Complete Field Guide to All 9 Bottom Terminal Tabs</h2></div></div>
<p>Every operational tool sits inside the resizable bottom dock so you never need to scroll off the chart. Drag the horizontal divider bar to resize or double-click to collapse.</p>
<table>
  <thead><tr><th># &amp; Tab Name</th><th>Core Purpose</th><th>What You Inspect &amp; Key Actions Inside</th></tr></thead>
  <tbody>
    <tr><td><b>1. 📋 Trade Journal</b></td><td>Full audit ledger of Executed vs Protected/Filtered signals</td><td>Filter by <b>All</b>, <b>Executed</b>, or <b>⚠️ Slippage &amp; Filtered (<code>FLT-*</code>)</b>. See exact Trigger Price vs Fill Price, Exit Reason (<code>TP_3_BRICKS</code>, <code>SAFETY_STOP</code>), MFE/MAE in bricks, and Gross vs Net P&amp;L. Download <b>📄 Tear-Sheet</b> or <b>📥 CSV</b>.</td></tr>
    <tr><td><b>2. ⚡ Capital.com Live Orders</b></td><td>Real-time broker portfolio &amp; manual override</td><td>Lists every active Capital.com deal (<code>Deal ID</code>, <code>Epic</code>, <code>BUY/SELL</code>, <code>Size</code>, <code>Entry</code>, <code>SL/TP</code>, live floating <b>UPL</b>). Includes an instant per-deal <b>Close</b> button.</td></tr>
    <tr><td><b>3. 📊 Market Scanner (16 Mkts)</b></td><td>Radar across Metals, Energy, Forex &amp; Global Indices</td><td>Displays live Bid/Ask mid-price, Spread, Brick Size, Run streak, Supertrend, Signal (<code>BUY SIGNAL</code>, <code>SELL SIGNAL</code>, <code>HOLD</code>), and Filter Status (<code>READY</code> or Veto reason). Toggle <b>🟢 AUTO / ⏸ PAUSED</b> per asset or click <b>📊 Load Chart</b>.</td></tr>
    <tr><td><b>4. 📈 Equity Curve (vs Raw)</b></td><td>Visual proof of strategy edge over time</td><td>Plots <b>Pro Net P&amp;L</b> (green solid), <b>Pro Gross before spread</b> (blue dashed), and <b>Raw Unfiltered P&amp;L</b> (amber dashed). Instantly reveals whether protections are adding alpha.</td></tr>
    <tr><td><b>5. 🎲 Monte Carlo Cone (2,500 Runs)</b></td><td>Forward drawdown stress-testing</td><td>Reshuffles your trade sequence 2,500 times with replacement to map <b>P90 Optimistic</b>, <b>P50 Median</b>, and <b>P10 Worst-Case</b> futures so position size matches real risk tolerance.</td></tr>
    <tr><td><b>6. 🎯 TCA &amp; Attribution</b></td><td>Wall-Street Transaction Cost &amp; Leg Analytics</td><td>Compares <b>Leg 1 (Usual 3B-TP)</b> vs <b>Leg 2 (Runner +1B)</b> Win Rate, Gross P&amp;L, Spread Drag ($), and Avg Net/Trade, alongside a ranked count of every blocked trade by Protection Guard. Opens printable Institutional Tear-Sheet.</td></tr>
    <tr><td><b>7. 🔬 Optimizer</b></td><td>One-click 36-combo parameter grid search</td><td>Click <b>▶ Run Grid Search</b> to test <code>ATR Divisor (15/22/32/48) × Usual TP (2B/3B/4B) × Confirm (0m/3m/5m)</code> on the current market, ranked by <b>Calmar Ratio &amp; Net P&amp;L</b>. Click <b>⚡ Apply</b> to load any winning combo into your live strategy immediately.</td></tr>
    <tr><td><b>8. 🔑 Broker Env &amp; Sessions</b></td><td>Control vault for Capital.com, Telegram &amp; Security + Session stats</td><td>Manage Capital.com DEMO/LIVE keys, configure &amp; test <b>@RENKO2026BOT</b> Telegram alerts &amp; Daily Digest, set/clear <b>DASHBOARD_PASSWORD</b>, and analyze win rates across <b>Asian / London / New York (UAE GST)</b> sessions.</td></tr>
    <tr><td><b>9. 📜 Bot Activity Log</b></td><td>Chronological black-box recorder (UAE GST timestamps)</td><td>Every background scan, 5-minute confirmation countdown (<code>CONFIRM_WAIT</code>), Order Open, Runner Arming (<code>WORKING_ORDER</code>), Breakeven Lock (<code>BREAKEVEN_LOCK</code>), and Flatten event with Deal IDs.</td></tr>
  </tbody>
</table>

<!-- SECTION 6: LEFT SIDEBAR CONTROLS + SECTION 7: 9 RISK GUARDS -->
<div class="sec-hdr"><div><div class="sec-num">Section 06</div><h2>The 9 Institutional Capital Protection Shields</h2></div></div>
<p>Think of these 9 shields as a Formula 1 car's roll-cage, ABS brakes, and traction control. Together they generated <b class="pos">${edge:+.2f}</b> extra Net P&amp;L over the raw unfiltered strategy:</p>
<table>
  <thead><tr><th>Protection Shield</th><th>What Goes Wrong Without It</th><th>How This Cockpit Prevents It Automatically</th></tr></thead>
  <tbody>
    <tr><td><b>1. One-Trade-Per-Run Lock</b></td><td>Re-entering late inside the exact same brick trend after an exit.</td><td>Permits at most 1 entry per unbroken green/red brick run until a full reversal resets the counter.</td></tr>
    <tr><td><b>2. Grid-Anchor Brick Origin</b></td><td>Tiny offset shifts make backtests &amp; live charts disagree.</td><td>Snaps every brick to exact multiples of <code>brick_size</code> so live and backtest bricks match 1:1.</td></tr>
    <tr><td><b>3. UAE Rollover Blackout</b></td><td>At midnight NY close (<b>00:45–02:15 GST</b>), broker spreads spike 5×.</td><td>Automatically vetoes new entries during daily rollover so wide spreads never stop you out prematurely.</td></tr>
    <tr><td><b>4. US Macro News Blackout</b></td><td>CPI/NFP (<b>17:25–17:35 GST</b>) &amp; FOMC (<b>22:55–23:10 GST</b>) cause violent spikes.</td><td>Blocks fresh entries right across Tier-1 US economic releases.</td></tr>
    <tr><td><b>5. Dynamic Spread Veto</b></td><td>Thin liquidity makes spread eat >25% of brick profit.</td><td>Rejects any signal when live Bid/Ask spread exceeds <code>max_spread_frac_of_brick × brick_size</code>.</td></tr>
    <tr><td><b>6. News Spike &amp; Overshoot Guard</b></td><td>One huge 1m bar jumps 4+ bricks and leaves terrible fill prices.</td><td>Skips bars printing &gt;3 bricks at once or overshooting trigger level by &gt;1.5 bricks.</td></tr>
    <tr><td><b>7. Live Pre-Execution Slippage Guard</b></td><td>Price sprints away between brick close and broker API order.</td><td>Aborts live Capital.com order if live Ask/Bid drifted &gt;<code>0.35 Bricks</code> past trigger.</td></tr>
    <tr><td><b>8. Cross-Asset Correlation Guard</b></td><td>Buying Gold + EURUSD + GBPUSD + US500 bets 4× on one USD drop.</td><td>Enforces <code>Max Correlated Pos = 2</code> per cluster (<code>ENERGY</code>, <code>USD_BLOC</code>, <code>US_INDICES</code>, <code>EU_INDICES</code>).</td></tr>
    <tr><td><b>9. Emergency Kill Switch + Safe Trailer</b></td><td>Flash crash or broken trend without human supervision.</td><td>Trailing 3-brick broker safety stop + Instant <b>FLATTEN / KILL SWITCH</b> (or Telegram <code>/flatten</code>).</td></tr>
  </tbody>
</table>

<!-- SECTION 8: LIVE PERFORMANCE CHARTS & METRICS -->
<div class="page-break"></div>
<div class="sec-hdr"><div><div class="sec-num">Section 07</div><h2>Quantitative Performance Benchmarks &amp; Live Attribution Charts</h2></div></div>
{_svg_equity_chart(bt)}
<div style="height:10px;"></div>
{_svg_tca_bars(tca)}
<table>
  <thead><tr><th>Cohort / Leg ({ds.get('epic','GOLD')} Current Window)</th><th>Trades</th><th>Win Rate</th><th>Gross P&amp;L</th><th>Spread Drag</th><th>Net P&amp;L</th><th>Avg / Trade</th></tr></thead>
  <tbody>
    <tr><td><b>Leg 1: USUAL (3B Fixed Take-Profit)</b></td><td>{usual.get('trades',0)}</td><td>{usual.get('win_rate_%',0)}%</td><td>${float(usual.get('gross',0)):+.2f}</td><td class="neg">-${abs(float(usual.get('spread_cost',0))):.2f}</td><td class="pos">${float(usual.get('net',0)):+.2f}</td><td>${float(usual.get('avg_net',0)):+.2f}</td></tr>
    <tr><td><b>Leg 2: RUNNER (+1B Continuation)</b></td><td>{runner.get('trades',0)}</td><td>{runner.get('win_rate_%',0)}%</td><td>${float(runner.get('gross',0)):+.2f}</td><td class="neg">-${abs(float(runner.get('spread_cost',0))):.2f}</td><td>${float(runner.get('net',0)):+.2f}</td><td>${float(runner.get('avg_net',0)):+.2f}</td></tr>
    <tr><td><b>Combined Model E Pro Strategy</b></td><td>{sn.get('trades',0)}</td><td>{sn.get('win_rate_%',0)}%</td><td>${float(sg.get('net',0)):+.2f}</td><td class="neg">-${abs(float(costs.get('total_spread_cost',0))):.2f}</td><td class="pos">${float(sn.get('net',0)):+.2f}</td><td>${float(sn.get('avg_trade',0)):+.2f}</td></tr>
  </tbody>
</table>
<div class="box-teen">
  <div class="box-title">📏 How To Grade Any Strategy Setup in 10 Seconds (Target Benchmarks)</div>
  <b>1. Profit Factor ≥ 1.35</b> (Great ≥ 1.60) &nbsp;|&nbsp;
  <b>2. Sharpe ≥ 1.5 &amp; Calmar ≥ 1.5</b> (Means profits far outweigh drawdowns) &nbsp;|&nbsp;
  <b>3. Pro Edge vs Raw &gt; $0</b> (Proves our protections actually protect you) &nbsp;|&nbsp;
  <b>4. Expectancy &gt; +0.10R</b> (Positive expected payoff per trade).
</div>

<!-- SECTION 9: GLOBAL UAE GST SCHEDULE & 16 MARKETS -->
<div class="page-break"></div>
<div class="sec-hdr"><div><div class="sec-num">Section 08</div><h2>UAE Gulf Standard Time (GST / UTC+4) Market Schedule &amp; 16-Asset Matrix</h2></div></div>
{_svg_uae_gst_clock()}
<table>
  <thead><tr><th>Market / Epic</th><th>Asset Name</th><th>Category</th><th>Quote Unit</th><th>Decimals</th><th>Fallback Brick</th><th>Correlation Guard Cluster</th></tr></thead>
  <tbody>{''.join(inst_rows)}</tbody>
</table>
<p style="font-size:9pt; color:#475569;"><b>Tip:</b> Click any of the 4 sidebar Category Buttons (<b>Metals</b>, <b>Energy</b>, <b>Forex</b>, <b>Indices</b>) to instantly load tailored ATR Divisor, Stop, and Spread limits tuned specifically for that asset class!</p>

<!-- SECTION 10: TELEGRAM 2-WAY REMOTE COCKPIT -->
<div class="sec-hdr"><div><div class="sec-num">Section 09</div><h2>2-Way Telegram Mobile Cockpit (@RENKO2026BOT) &amp; Cloud Safeguards</h2></div></div>
<div class="insight-grid">
  <div class="box-teen">
    <div class="box-title">📲 Interactive Phone Commands (Send in @RENKO2026BOT)</div>
    • <code>/status</code> — Check active market, watcher state &amp; today's live P&amp;L.<br/>
    • <code>/positions</code> — Inspect every open Capital.com deal &amp; floating UPL.<br/>
    • <code>/pnl</code> or <code>/digest</code> — Instant Model E performance &amp; attribution summary.<br/>
    • <code>/scanner</code> — See which of the 16 global markets have BUY/SELL signals.<br/>
    • <code>/pause</code> | <code>/resume</code> | <code>/flatten</code> — Pause, resume, or trigger Emergency Kill Switch!
  </div>
  <div class="box-quant">
    <div class="box-title">🔔 Automatic Push Alerts &amp; Cloud State Persistence</div>
    • Push alerts fire automatically in UAE GST on: <code>OPEN</code>, <code>REVERSE_OPEN</code>, <code>WORKING_ORDER</code>, <code>RUNNER_FILLED</code>, <code>BREAKEVEN_LOCK</code>, <code>FLATTEN</code>, and <b>Daily Digest (02:00 GST)</b>.<br/>
    • Every strategy rule, OMS order, and trade log mirrors into <b>SQLite (<code>cloud_state.sqlite</code>) + Browser Backup</b>, auto-restoring seamlessly across Render cloud restarts.
  </div>
</div>

<!-- SECTION 11: MASTER A-Z GLOSSARY -->
<div class="page-break"></div>
<div class="sec-hdr"><div><div class="sec-num">Section 10</div><h2>Complete A–Z Glossary of Definitions &amp; Abbreviations</h2></div></div>
<table>
  <thead><tr><th style="width:22%;">Term / Abbr.</th><th style="width:40%;">Institutional Definition</th><th style="width:38%;">Simple Meaning (For Anyone / Teenagers)</th></tr></thead>
  <tbody>{glossary_rows}</tbody>
</table>

<!-- SECTION 12: DAILY 5-MINUTE OPERATING CHECKLIST -->
<div class="sec-hdr"><div><div class="sec-num">Section 11</div><h2>Daily 5-Minute Operating Playbook &amp; Safety Rules</h2></div></div>
<table>
  <thead><tr><th>When (UAE Time)</th><th>2-Minute Routine</th><th>What To Verify</th></tr></thead>
  <tbody>
    <tr><td><b>Morning (10:30 GST)</b><br/><i>Before London Open</i></td><td>Open Cockpit or type <code>/status</code> &amp; <code>/scanner</code> in Telegram.</td><td>Verify Top Header shows <code>DEMO READY</code> (or <code>LIVE</code>), 24/7 Bot is <b>RUNNING</b>, UAE Clock ticking, and scan which markets are <code>READY</code>.</td></tr>
    <tr><td><b>Afternoon (16:45 GST)</b><br/><i>Before NY &amp; US News</i></td><td>Check <b>Market Scanner</b> &amp; open <b>⚡ Capital.com Live Orders</b>.</td><td>Confirm <b>US Macro Blackout (17:30 GST)</b> is ticked before CPI/NFP days; verify any open <code>+1B Runner</code> has its Stop locked safely.</td></tr>
    <tr><td><b>Weekend Tune (1×/wk)</b></td><td>Open <b>🔬 Optimizer</b> tab &amp; click <b>Run Grid Search</b>.</td><td>Compare current settings vs Top-3 ranked combos by <b>Calmar &amp; Sharpe</b>; never chase tiny sample sizes (&lt;15 trades). Print weekly <b>📄 Tear-Sheet</b>.</td></tr>
    <tr><td><b>Emergency Anytime</b></td><td>Click red <b>FLATTEN / KILL SWITCH</b> or text <code>/flatten</code> on Telegram.</td><td>Immediately halts all new signals and closes every open deal on Capital.com until you deliberately click <b>RESUME</b> (or text <code>/resume</code>).</td></tr>
  </tbody>
</table>

<p style="text-align:center; font-size:8.5pt; color:#64748b; margin-top:26px; border-top:1px solid #e2e8f0; padding-top:10px;">
  Renko ATR Pro &amp; Capital.com Cloud Cockpit · Institutional Documentation &amp; User Playbook · Generated {gen_gst}
</p>
</body>
</html>"""

    OUT_HTML.write_text(doc, encoding="utf-8")
    return OUT_HTML


if __name__ == "__main__":
    p = build_guide()
    print(f"Generated McKinsey Master Guide HTML: {p} ({p.stat().st_size:,} bytes)")
