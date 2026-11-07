"""Monte Carlo 'time machine' on the backtest's trades.

  python3 montecarlo.py                 # 10,000 reshuffles of journal/backtest_trades.csv (net P&L)
  python3 montecarlo.py --key gross --runs 20000 --seed 7
"""
import argparse
import csv
import random
from pathlib import Path


def pct(xs, q):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q / 100 * len(xs)))]


def run_montecarlo(pnl_list: list[float], runs: int = 2000, seed: int = 19) -> dict:
    if len(pnl_list) < 3:
        return {
            "runs": 0, "trades": len(pnl_list),
            "p5_final": 0.0, "p50_final": 0.0, "p95_final": 0.0,
            "p50_dd": 0.0, "p95_dd": 0.0, "p99_dd": 0.0,
            "p50_streak": 0, "p95_streak": 0, "loss_prob_%": 0.0,
            "fan_p10": [], "fan_p50": [], "fan_p90": [], "sample_paths": []
        }
    rng = random.Random(seed)
    n = len(pnl_list)
    finals, dds, streaks = [], [], []
    step_values = [[] for _ in range(n + 1)]
    sample_paths = []

    for r_idx in range(runs):
        eq = peak = dd = 0.0
        s = worst = 0
        path = [0.0]
        step_values[0].append(0.0)
        for step in range(1, n + 1):
            x = rng.choice(pnl_list)
            eq += x
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
            s = s + 1 if x <= 0 else 0
            worst = max(worst, s)
            step_values[step].append(round(eq, 2))
            if r_idx < 25:
                path.append(round(eq, 2))
        if r_idx < 25:
            sample_paths.append(path)
        finals.append(eq)
        dds.append(dd)
        streaks.append(worst)

    fan_p10 = [round(pct(step_values[i], 10), 2) for i in range(n + 1)]
    fan_p50 = [round(pct(step_values[i], 50), 2) for i in range(n + 1)]
    fan_p90 = [round(pct(step_values[i], 90), 2) for i in range(n + 1)]

    return {
        "runs": runs,
        "trades": n,
        "p5_final": round(pct(finals, 5), 2),
        "p50_final": round(pct(finals, 50), 2),
        "p95_final": round(pct(finals, 95), 2),
        "p50_dd": round(pct(dds, 50), 2),
        "p95_dd": round(pct(dds, 95), 2),
        "p99_dd": round(pct(dds, 99), 2),
        "p50_streak": int(pct(streaks, 50)),
        "p95_streak": int(pct(streaks, 95)),
        "loss_prob_%": round(100 * sum(1 for f in finals if f < 0) / len(finals), 1),
        "fan_p10": fan_p10,
        "fan_p50": fan_p50,
        "fan_p90": fan_p90,
        "sample_paths": sample_paths,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(Path(__file__).parent / "journal" / "backtest_trades.csv"))
    ap.add_argument("--key", default="net", choices=["net", "gross"])
    ap.add_argument("--runs", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=19)
    a = ap.parse_args()
    pnl = [float(r[a.key]) for r in csv.DictReader(open(a.file))]
    if len(pnl) < 3:
        print("Only %d trades: too few for a meaningful Monte Carlo." % len(pnl))
        return
    res = run_montecarlo(pnl, runs=a.runs, seed=a.seed)
    print("%d runs x %d trades (%s P&L, seed %d)" % (res["runs"], res["trades"], a.key, a.seed))
    print("  final P&L      5%%: %8.2f   median: %8.2f   95%%: %8.2f" % (res["p5_final"], res["p50_final"], res["p95_final"]))
    print("  max drawdown  median: %8.2f   95%%: %8.2f   99%%: %8.2f" % (res["p50_dd"], res["p95_dd"], res["p99_dd"]))
    print("  losing streak median: %d   95%%: %d" % (res["p50_streak"], res["p95_streak"]))
    print("  runs that ended below zero: %.1f%%" % res["loss_prob_%"])


if __name__ == "__main__":
    main()
