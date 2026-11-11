"""Grid Search & Parameter Optimization across multiple market regimes for Renko Gold Bot."""
import copy
import json
from backtest import generate_sample_gold_data, simulate, load_config, stats

def run_grid():
    base_cfg = load_config()
    
    # 3 diverse 15-day market regimes (45 days total intraday)
    print("Generating 3 distinct market regimes (45 days intraday total)...")
    regimes = [
        ("Regime 1 (Trend/Mean-revert)", generate_sample_gold_data(days_intraday=15, seed=42)),
        ("Regime 2 (Chop/High-vol)", generate_sample_gold_data(days_intraday=15, seed=99)),
        ("Regime 3 (Breakout/Whipsaw)", generate_sample_gold_data(days_intraday=15, seed=77)),
    ]

    # Parameter grid candidates
    atr_divs = [5, 8, 10, 15, 20, 25]
    green_buys = [1, 2] # 1 brick immediate vs 2 bricks confirmed
    st_factors = [1.5, 2.1, 2.5]
    exit_on_sts = [False, True]
    safety_stops = [2.0, 3.0]
    breakevens = [0.0, 1.5]
    session_modes = [
        ("All Sessions", {"asian": True, "london": True, "new_york": True}),
        ("London + NY Only", {"asian": False, "london": True, "new_york": True}),
        ("NY Only", {"asian": False, "london": False, "new_york": True}),
    ]

    results = []

    # Test focused combinations
    count = 0
    for sess_label, sess_dict in session_modes:
        for div in atr_divs:
            for gb in green_buys:
                for st_fac in [2.1, 1.5]:
                    for exit_st in [False, True]:
                        for be in [0.0, 1.5]:
                            cfg = copy.deepcopy(base_cfg)
                            cfg["risk"]["sessions"] = sess_dict
                            cfg["risk"]["breakeven_after_bricks"] = be
                            cfg["risk"]["safety_stop_bricks"] = 3.0
                            cfg["strategy"]["atr_div"] = div
                            cfg["strategy"]["green_buy"] = gb
                            cfg["strategy"]["red_sell"] = gb
                            cfg["strategy"]["st_factor"] = st_fac
                            cfg["strategy"]["exit_on_st"] = exit_st
                            cfg["strategy"]["one_trade_per_run"] = True

                            total_net = 0.0
                            total_trades = 0
                            total_wins = 0
                            regime_nets = []
                            max_dd_all = 0.0

                            for r_name, (bars, days) in regimes:
                                trades, _, _, _ = simulate(bars, days, cfg, use_stop=True)
                                st = stats(trades, "net")
                                total_net += st["net"]
                                total_trades += st["trades"]
                                total_wins += int(st["trades"] * st["win_rate_%"] / 100.0)
                                regime_nets.append(st["net"])
                                max_dd_all = max(max_dd_all, st["max_drawdown"])

                            if total_trades < 10:
                                continue

                            overall_wr = round(100.0 * total_wins / total_trades, 1) if total_trades else 0
                            # Calmar / Recovery ratio proxy
                            rec_ratio = round(total_net / max_dd_all, 2) if max_dd_all > 0 else 0
                            # Consistency: how many regimes were profitable?
                            profitable_regimes = sum(1 for n in regime_nets if n > 0)

                            results.append({
                                "div": div,
                                "gb": gb,
                                "st_fac": st_fac,
                                "exit_st": exit_st,
                                "be": be,
                                "session": sess_label,
                                "net": round(total_net, 2),
                                "trades": total_trades,
                                "win_rate": overall_wr,
                                "max_dd": round(max_dd_all, 2),
                                "recovery_ratio": rec_ratio,
                                "profitable_regimes": profitable_regimes,
                                "regime_nets": [round(n, 1) for n in regime_nets]
                            })
                            count += 1

    print(f"Tested {count} configurations across 3 regimes.")

    # Sort by recovery ratio (Net / Max DD) and total net profit
    results.sort(key=lambda x: (x["profitable_regimes"], x["recovery_ratio"], x["net"]), reverse=True)

    print("\n=== TOP 10 BEST PERFORMING SETTINGS (Cross-Regime Robustness) ===")
    for i, r in enumerate(results[:10], 1):
        print(f"#{i}: Net: ${r['net']:+8.2f} | MaxDD: ${r['max_dd']:6.2f} | Ratio: {r['recovery_ratio']:4.2f} | "
              f"WinRate: {r['win_rate']:4.1f}% | Trades: {r['trades']:3d} | Profitable Regimes: {r['profitable_regimes']}/3 | "
              f"Div: {r['div']:2d} | ConfBricks: {r['gb']} | ST: {r['st_fac']} | ExitST: {r['exit_st']} | BE: {r['be']} | {r['session']}")

    return results

if __name__ == "__main__":
    run_grid()
