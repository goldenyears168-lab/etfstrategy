#!/usr/bin/env python3
"""9217 松山在指定股票的買進金額門檻 sweep，找 L1H7 跟單訊號最純的門檻.

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_9217_6147_threshold_sweep.py --stock-id 6147
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_9217_6147_threshold_sweep.py --stock-id 2337
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
from scipy import stats

from market_benchmark import load_benchmark_close
from stock_db import DEFAULT_DB_PATH, connect

HOLD_DAYS = 7
COST_RT = 0.003
BETA = 1.15
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"


def load_buy_days(conn, trader_id: str, stock_id: str) -> pd.DataFrame:
    q = """
        SELECT b.trade_date, b.buy, p.close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id AND p.trade_date = b.trade_date AND p.source = 'finmind'
        WHERE b.securities_trader_id = ? AND b.stock_id = ? AND b.source = 'finmind' AND b.buy > 0
        ORDER BY b.trade_date
    """
    df = pd.read_sql_query(q, conn, params=(trader_id, stock_id))
    df["buy_amt"] = df["buy"] * df["close"]
    return df


def load_bars(conn, stock_id: str) -> pd.DataFrame:
    q = """
        SELECT trade_date, open, close FROM stock_daily_bars
        WHERE stock_id = ? AND source = 'finmind' ORDER BY trade_date
    """
    return pd.read_sql_query(q, conn, params=(stock_id,)).set_index("trade_date")


def l1h7_backtest(signal_dates: list[str], bars: pd.DataFrame, bench: pd.Series) -> pd.DataFrame:
    all_dates = bars.index.tolist()
    idx = {d: i for i, d in enumerate(all_dates)}
    bench_dates = bench.index.astype(str).tolist()
    bidx = {d: i for i, d in enumerate(bench_dates)}
    rows = []
    for sig in signal_dates:
        if sig not in idx:
            continue
        i_entry = idx[sig] + 1
        i_exit = i_entry + HOLD_DAYS - 1
        if i_entry >= len(all_dates) or i_exit >= len(all_dates):
            continue
        entry_date, exit_date = all_dates[i_entry], all_dates[i_exit]
        entry_px, exit_px = bars["open"].iloc[i_entry], bars["close"].iloc[i_exit]
        if pd.isna(entry_px) or pd.isna(exit_px) or entry_px <= 0:
            continue
        if entry_date not in bidx or exit_date not in bidx:
            continue
        b0, b1 = bench.iloc[bidx[entry_date]], bench.iloc[bidx[exit_date]]
        if pd.isna(b0) or pd.isna(b1) or b0 <= 0:
            continue
        net_ret = (exit_px / entry_px - 1.0) - COST_RT
        bench_ret = b1 / b0 - 1.0
        rows.append({"signal_date": sig, "excess_pct": (net_ret - BETA * bench_ret) * 100})
    return pd.DataFrame(rows)


def summarize(df: pd.DataFrame, label: str) -> dict:
    vals = df["excess_pct"].dropna() / 100.0
    n = len(vals)
    if n < 2:
        return {"門檻": label, "n": n, "mean_excess_pct": None, "win_rate_pct": None, "t": None, "p": None}
    t_stat, p_val = stats.ttest_1samp(vals, 0)
    return {
        "門檻": label,
        "n": n,
        "mean_excess_pct": round(vals.mean() * 100, 3),
        "median_excess_pct": round(vals.median() * 100, 3),
        "win_rate_pct": round((vals > 0).mean() * 100, 1),
        "t": round(float(t_stat), 3),
        "p": round(float(p_val), 4),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trader-id", default="9217")
    ap.add_argument("--stock-id", default="6147")
    ap.add_argument(
        "--amt-sweep-m",
        type=float,
        nargs="+",
        default=[5, 10, 20, 30, 50, 80, 100, 150, 200],
        help="絕對金額門檻 sweep（百萬元），可依股票量級調整",
    )
    args = ap.parse_args()
    trader_id = args.trader_id
    stock_id = args.stock_id

    conn = connect(DEFAULT_DB_PATH)
    bench = load_benchmark_close(conn, code="IX0001")
    buy_days = load_buy_days(conn, trader_id, stock_id)
    bars = load_bars(conn, stock_id)
    conn.close()

    print(f"[INFO] 分點：{trader_id}，股票：{stock_id}，總買進交易日：{len(buy_days)}")
    print(buy_days["buy_amt"].describe(percentiles=[0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95]))

    results = []

    bt = l1h7_backtest(buy_days["trade_date"].tolist(), bars, bench)
    results.append(summarize(bt, "全部（無門檻）"))

    for pctile in (0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95):
        floor = buy_days["buy_amt"].quantile(pctile)
        sel = buy_days[buy_days["buy_amt"] >= floor]
        bt = l1h7_backtest(sel["trade_date"].tolist(), bars, bench)
        label = f"前{round((1-pctile)*100)}%（≥{floor/1e6:.2f}百萬）"
        results.append(summarize(bt, label))

    for amt_m in args.amt_sweep_m:
        floor = amt_m * 1e6
        sel = buy_days[buy_days["buy_amt"] >= floor]
        bt = l1h7_backtest(sel["trade_date"].tolist(), bars, bench)
        results.append(summarize(bt, f"≥{amt_m:g}百萬元"))

    out = pd.DataFrame(results)
    print(f"\n=== {trader_id}/{stock_id} 門檻 Sweep 結果 ===")
    print(out.to_string(index=False))
    out_path = OUT_DIR / f"whale_{trader_id}_{stock_id}_threshold_sweep.csv"
    out.to_csv(out_path, index=False)
    print(f"\n[OK] 寫入 {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
