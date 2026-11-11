"""Deep profile of top candidate settings across 45 days and 3 regimes."""
from backtest import generate_sample_gold_data, simulate, load_config, stats, session_breakdown
import copy

regimes = [
    ("Regime 1 (Trend/Mean-revert)", generate_sample_gold_data(days_intraday=15, seed=42)),
    ("Regime 2 (Chop/High-vol)", generate_sample_gold_data(days_intraday=15, seed=99)),
    ("Regime 3 (Breakout/Whipsaw)", generate_sample_gold_data(days_intraday=15, seed=77)),
]

candidates = [
    {
        "name": "Setting 1: Institutional Balanced (Golden Middle)",
        "div": 15, "gb": 2, "st_fac": 1.5, "exit_st": False, "be": 0.0,
        "sessions": {"asian": True, "london": True, "new_york": True}
    },
    {
        "name": "Setting 2: Maximum Net Profit (Fast Confirmation)",
        "div": 15, "gb": 1, "st_fac": 2.1, "exit_st": False, "be": 0.0,
        "sessions": {"asian": True, "london": True, "new_york": True}
    },
    {
        "name": "Setting 3: London & New York Session Specialist",
        "div": 15, "gb": 2, "st_fac": 1.5, "exit_st": False, "be": 0.0,
        "sessions": {"asian": False, "london": True, "new_york": True}
    },
    {
        "name": "Setting 4: Medium Swing (Divisor 10 / ~$5.00 Bricks)",
        "div": 10, "gb": 2, "st_fac": 2.1, "exit_st": False, "be": 0.0,
        "sessions": {"asian": True, "london": True, "new_york": True}
    },
    {
        "name": "Setting 5: Conservative Multi-Day Swing (Divisor 5 / ~$10.00 Bricks)",
        "div": 5, "gb": 2, "st_fac": 2.1, "exit_st": False, "be": 0.0,
        "sessions": {"asian": True, "london": True, "new_york": True}
    }
]

base_cfg = load_config()

for cand in candidates:
    cname = cand["name"]
    print(f"\n==========================================")
    print(f" {cname} ")
    print(f"==========================================")
    cfg = copy.deepcopy(base_cfg)
    cfg["strategy"]["atr_div"] = cand["div"]
    cfg["strategy"]["green_buy"] = cand["gb"]
    cfg["strategy"]["red_sell"] = cand["gb"]
    cfg["strategy"]["st_factor"] = cand["st_fac"]
    cfg["strategy"]["exit_on_st"] = cand["exit_st"]
    cfg["risk"]["breakeven_after_bricks"] = cand["be"]
    cfg["risk"]["sessions"] = cand["sessions"]
    
    all_trades = []
    for r_name, (bars, days) in regimes:
        trades, _, _, _ = simulate(bars, days, cfg, use_stop=True)
        all_trades.extend(trades)
        st_reg = stats(trades, "net")
        net_val = st_reg["net"]
        wr_val = st_reg["win_rate_%"]
        pf_val = st_reg["profit_factor"]
        dd_val = st_reg["max_drawdown"]
        print(f"  {r_name}: Net=${net_val:+.2f} | WR={wr_val}% | PF={pf_val} | MaxDD=${dd_val}")
        
    tot = stats(all_trades, "net")
    sess = session_breakdown(all_trades, "net")
    tot_net = tot["net"]
    tot_wr = tot["win_rate_%"]
    tot_pf = tot["profit_factor"]
    tot_dd = tot["max_drawdown"]
    tot_tr = tot["trades"]
    avg_w = tot["avg_win"]
    avg_l = tot["avg_loss"]
    wl_ratio = round(abs(avg_w / avg_l), 2) if avg_l else 0
    print(f"  --> TOTAL (45 Days): Net=${tot_net:+.2f} | WR={tot_wr}% | PF={tot_pf} | MaxDD=${tot_dd} | Trades={tot_tr}")
    print(f"      Avg Win=${avg_w} | Avg Loss=${avg_l} | Win/Loss Ratio={wl_ratio}")
    print(f"      Asian:  ${sess['asian']['net']:+.2f} (WR: {sess['asian']['win_rate_%']}%, PF: {sess['asian']['profit_factor']})")
    print(f"      London: ${sess['london']['net']:+.2f} (WR: {sess['london']['win_rate_%']}%, PF: {sess['london']['profit_factor']})")
    print(f"      NY:     ${sess['new_york']['net']:+.2f} (WR: {sess['new_york']['win_rate_%']}%, PF: {sess['new_york']['profit_factor']})")
