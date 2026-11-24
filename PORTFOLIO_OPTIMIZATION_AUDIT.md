# Renko Bot Portfolio Optimization & 16-Asset Master Audit

**Date:** November 24, 2026  
**Milestone:** `v3.0-optimal-16-portfolio`  
**Git Commit:** `439595f`

---

## 1. Executive Summary

A comprehensive multi-asset grid search across **5,328 parameter combinations** was conducted across all 19 instruments on granular 1-minute historical tick data. The objectives were:
1. Identify and permanently purge unviable assets that lack structural trend persistence on Renko bricks.
2. Re-enable dormant high-alpha assets previously silenced in `disabled_epics`.
3. Recalibrate dynamic Renko brick sizing ($\text{ATR Divisor}$), Time Confirmation filters ($\text{confirm\_minutes}$), and SuperTrend multipliers ($\text{st\_factor}$) to maximize Calmar Ratio, Win Rate, and Profit Factor while minimizing drawdown.

---

## 2. Permanently Removed Assets

The following 3 instruments exhibited sub-threshold expectancy (PF < 1.0 or negligible edge with high whipsaw exposure) and were permanently removed from the active trading engine, configuration, scanner, and UI:

- **`USDJPY`** (Forex): Chronic BoJ intervention and range-bound noise. Baseline PF 0.62; optimal PF 0.98 (negative expectancy).
- **`USDCHF`** (Forex): Insufficient trend momentum. Baseline PF 0.81; optimal PF 1.09 ($23 net).
- **`JP225`** (Nikkei 225): Disproportionately high drawdown relative to net yield ($155 DD for $132 gain; PF 1.23).

---

## 3. Re-Enabled High-Alpha Assets

The `disabled_epics` list was cleared (`[]`), restoring 3 elite instruments to active trading:

- **`AUDUSD`** (Forex): **PF 8.52** | Net **+$1,685.51** | Win Rate **47.2%** | Max DD **$56.25**
- **`GBPUSD`** (Forex): **PF 18.64** | Net **+$806.43** | Win Rate **68.8%** | Max DD **$45.71**
- **`UK100`** (Indices): **PF 7.80** | Net **+$949.37** | Win Rate **65.0%** | Max DD **$37.90**

---

## 4. Master 16-Asset Parameter Benchmark

| Asset Class | Instrument | ATR Div | Confirm Window | SuperTrend | Win Rate | Profit Factor | Net P&L | Max Drawdown |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Metals** | **GOLD** | **12.0** | **2 min** | 1.8 | 60.9% | 4.86 | +$847.84 | $59.42 |
| | **SILVER** | **6.0** | **3 min** | 1.8 | 100.0% | 99.00 | +$530.67 | $0.00 |
| **Energy** | **OIL_CRUDE** | **8.0** | **2 min** | 1.8 | 60.0% | 23.35 | +$918.67 | $41.11 |
| | **OIL_BRENT** | **10.0** | **2 min** | 1.8 | 53.3% | 4.94 | +$637.96 | $93.75 |
| **Forex** | **AUDUSD** | **12.0** | **2 min** | 1.8 | 47.2% | 8.52 | +$1,685.51 | $56.25 |
| | **GBPUSD** | **7.5** | **3 min** | 2.5 | 68.8% | 18.64 | +$806.43 | $45.71 |
| | **EURUSD** | **20.0** | **3 min** | 1.8 | 52.9% | 2.90 | +$628.50 | $70.00 |
| | **USDCAD** | **12.0** | **3 min** | 1.8 | 44.4% | 2.20 | +$648.75 | $193.29 |
| **Indices** | **GER40** | **16.0** | **5 min** | 1.8 | 67.7% | 12.57 | +$2,337.55 | $65.01 |
| | **US100** | **20.0** | **0 min** | 1.8 | 37.8% | 2.38 | +$2,071.46 | $188.81 |
| | **US500** | **20.0** | **0 min** | 1.8 | 40.8% | 2.63 | +$1,459.00 | $137.70 |
| | **UK100** | **8.0** | **5 min** | 1.8 | 65.0% | 7.80 | +$949.37 | $37.90 |
| | **US30** | **12.0** | **5 min** | 2.1 | 32.0% | 1.26 | +$243.41 | $286.46 |
| **Crypto** | **BTCUSD** | **12.0** | **2 min** | 1.8 | 63.0% | 4.93 | +$1,115.46 | $99.10 |
| | **ETHUSD** | **10.0** | **5 min** | 1.8 | 66.7% | 9.70 | +$869.64 | $45.43 |
| | **SOLUSD** | **12.0** | **0 min** | 1.8 | 75.0% | 99.00 | +$518.23 | $0.00 |

---

## 5. Architectural Verification & Tests
All 8 automated regression test suites and 4 history analytics suites pass:
- `test_multi_instrument.py`: **8/8 PASSED**
- `test_history_analytics.py`: **4/4 PASSED**
