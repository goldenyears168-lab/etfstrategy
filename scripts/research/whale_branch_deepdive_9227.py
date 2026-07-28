#!/usr/bin/env python3
"""9227 (凱基-城中) deep dive: full trading profile, position building, timing quality,
statistical significance, and crossing overlap.

Run:
  PYTHONPATH=src .venv/bin/python scripts/research/whale_branch_deepdive_9227.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
from scipy import stats

from stock_db import connect, DEFAULT_DB_PATH

TRADER_ID = "9227"
START = "2024-07-01"
OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"
OUT_DIR.mkdir(parents=True, exist_ok=True)

conn = connect(DEFAULT_DB_PATH)


def load_top1_days() -> pd.DataFrame:
    """All days where 9227 was Top1 buyer of a stock (net>0, amount>=5,000,000 NTD)."""
    q = """
    WITH ranked AS (
      SELECT b.trade_date, b.stock_id, b.securities_trader_id, b.net,
             d.close,
             b.net * d.close AS amount,
             ROW_NUMBER() OVER (
               PARTITION BY b.trade_date, b.stock_id
               ORDER BY b.net DESC
             ) AS rnk
      FROM stock_broker_branch_daily b
      JOIN stock_daily_bars d
        ON d.stock_id = b.stock_id AND d.trade_date = b.trade_date AND d.source='finmind'
      WHERE b.source='finmind' AND b.trade_date >= ? AND b.net > 0
    )
    SELECT trade_date, stock_id, net, close, amount
    FROM ranked
    WHERE rnk = 1 AND securities_trader_id = ? AND amount >= 5000000
    ORDER BY trade_date
    """
    df = pd.read_sql_query(q, conn, params=(START, TRADER_ID))
    return df


def load_forward_returns(df: pd.DataFrame, horizons=(1, 3, 5, 9, 14, 20)) -> pd.DataFrame:
    stock_ids = df["stock_id"].unique().tolist()
    if not stock_ids:
        return df
    placeholders = ",".join(["?"] * len(stock_ids))
    bars = pd.read_sql_query(
        f"""
        SELECT stock_id, trade_date, close
        FROM stock_daily_bars
        WHERE source='finmind' AND stock_id IN ({placeholders})
        ORDER BY stock_id, trade_date
        """,
        conn,
        params=stock_ids,
    )
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    bench = pd.read_sql_query(
        "SELECT trade_date, close FROM stock_daily_bars WHERE source='finmind' AND stock_id='IX0001' ORDER BY trade_date",
        conn,
    )
    if bench.empty:
        try:
            from market_benchmark import load_benchmark_close
            bser = load_benchmark_close(conn, code="IX0001")
            bench = bser.reset_index()
            bench.columns = ["trade_date", "close"]
        except Exception:
            bench = pd.DataFrame(columns=["trade_date", "close"])
    bench["trade_date"] = pd.to_datetime(bench["trade_date"])
    bench = bench.sort_values("trade_date").set_index("trade_date")["close"]

    out_rows = []
    for sid, g in bars.groupby("stock_id"):
        g = g.sort_values("trade_date").reset_index(drop=True)
        g["idx"] = np.arange(len(g))
        date_to_idx = dict(zip(g["trade_date"], g["idx"]))
        closes = g["close"].values
        dates = g["trade_date"].values
        sub = df[df["stock_id"] == sid]
        for _, row in sub.iterrows():
            td = pd.Timestamp(row["trade_date"])
            if td not in date_to_idx:
                continue
            i0 = date_to_idx[td]
            c0 = closes[i0]
            rec = {"stock_id": sid, "trade_date": row["trade_date"], "net": row["net"], "amount": row["amount"], "close": c0}
            for h in horizons:
                i1 = i0 + h
                if i1 < len(closes):
                    ret = (closes[i1] / c0 - 1.0) * 100
                    d1 = pd.Timestamp(dates[i1])
                    # benchmark excess
                    try:
                        b0 = bench.loc[:td].iloc[-1]
                        b1 = bench.loc[:d1].iloc[-1]
                        bret = (b1 / b0 - 1.0) * 100
                    except Exception:
                        bret = np.nan
                    rec[f"h{h}_ret"] = ret
                    rec[f"h{h}_excess"] = ret - bret if not np.isnan(bret) else np.nan
                else:
                    rec[f"h{h}_ret"] = np.nan
                    rec[f"h{h}_excess"] = np.nan
            out_rows.append(rec)
    return pd.DataFrame(out_rows)


def timing_percentile(df: pd.DataFrame) -> pd.DataFrame:
    """20d trailing high/low percentile of close on the buy day, per stock."""
    stock_ids = df["stock_id"].unique().tolist()
    placeholders = ",".join(["?"] * len(stock_ids))
    bars = pd.read_sql_query(
        f"""
        SELECT stock_id, trade_date, close
        FROM stock_daily_bars
        WHERE source='finmind' AND stock_id IN ({placeholders})
        ORDER BY stock_id, trade_date
        """,
        conn,
        params=stock_ids,
    )
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    bars = bars.sort_values(["stock_id", "trade_date"])
    bars["roll_min20"] = bars.groupby("stock_id")["close"].transform(lambda s: s.rolling(20, min_periods=10).min())
    bars["roll_max20"] = bars.groupby("stock_id")["close"].transform(lambda s: s.rolling(20, min_periods=10).max())
    bars["pctile"] = (bars["close"] - bars["roll_min20"]) / (bars["roll_max20"] - bars["roll_min20"])
    bars_idx = bars.set_index(["stock_id", "trade_date"])["pctile"]

    df = df.copy()
    df["trade_date_dt"] = pd.to_datetime(df["trade_date"])
    vals = []
    for _, row in df.iterrows():
        key = (row["stock_id"], row["trade_date_dt"])
        vals.append(bars_idx.get(key, np.nan))
    df["timing_pctile"] = vals

    # market-wide random baseline: sample of the pctile column across all stock-days
    market_baseline = bars["pctile"].dropna()
    return df, market_baseline


def cumulative_position(df: pd.DataFrame, top_n: int = 8):
    """Cumulative net position time series for the most-traded stocks by this trader."""
    q = """
    SELECT trade_date, stock_id, net
    FROM stock_broker_branch_daily
    WHERE source='finmind' AND securities_trader_id = ? AND trade_date >= ?
    ORDER BY stock_id, trade_date
    """
    all_trades = pd.read_sql_query(q, conn, params=(TRADER_ID, START))
    top_stocks = (
        df.groupby("stock_id")["amount"].agg(["count", "sum"]).sort_values("count", ascending=False).head(top_n)
    )
    summaries = []
    series = {}
    for sid in top_stocks.index:
        g = all_trades[all_trades["stock_id"] == sid].copy()
        g = g.sort_values("trade_date")
        g["cum_net"] = g["net"].cumsum()
        series[sid] = g[["trade_date", "net", "cum_net"]]
        max_pos = g["cum_net"].max()
        min_pos = g["cum_net"].min()
        final_pos = g["cum_net"].iloc[-1] if len(g) else np.nan
        # classify
        rng = max_pos - min_pos if max_pos > min_pos else 1
        final_frac_of_max = final_pos / max_pos if max_pos > 0 else np.nan
        summaries.append({
            "stock_id": sid,
            "n_days_active": len(g),
            "max_cum_net": max_pos,
            "min_cum_net": min_pos,
            "final_cum_net": final_pos,
            "final_frac_of_max": final_frac_of_max,
        })
    return pd.DataFrame(summaries), series


def main():
    df = load_top1_days()
    print(f"Top1 buy days (net>0, amount>=500萬): n={len(df)}, distinct stocks={df['stock_id'].nunique()}")
    print(f"date range: {df['trade_date'].min()} .. {df['trade_date'].max()}")

    # (a) full trading profile
    df["amount_yi"] = df["amount"] / 1e8  # 億元
    stock_stats = df.groupby("stock_id")["amount"].agg(["count", "sum", "median", "mean", "max"]).sort_values("count", ascending=False)
    stock_stats.to_csv(OUT_DIR / "whale_branch_deepdive_9227_stock_profile.csv")
    print("\n--- (a) 全交易剖面 ---")
    print(f"總計 Top1 買方事件數: {len(df)}, 涵蓋股票數: {df['stock_id'].nunique()}")
    print(f"金額(億元) 分布: median={df['amount_yi'].median():.3f}, mean={df['amount_yi'].mean():.3f}, max={df['amount_yi'].max():.3f}")
    months = pd.to_datetime(df["trade_date"]).dt.to_period("M").nunique()
    print(f"活躍月數: {months}, 平均每月 Top1 事件數: {len(df)/months:.2f}")
    print("Top10 個股集中度 (事件數):")
    print(stock_stats.head(10))
    hh = (stock_stats["count"] / stock_stats["count"].sum()) ** 2
    print(f"HHI (以事件數計, 個股集中度): {hh.sum():.4f} (1/N_effective={1/hh.sum():.1f})")

    # (b) position building
    print("\n--- (b) 建倉型態 (Top8 交易最頻繁個股) ---")
    pos_summary, pos_series = cumulative_position(df, top_n=8)
    print(pos_summary.to_string(index=False))
    pos_summary.to_csv(OUT_DIR / "whale_branch_deepdive_9227_position_building.csv", index=False)

    # (c) forward returns + timing
    print("\n--- (c)/(d) 擇時品質 + forward return 統計顯著性 ---")
    fwd = load_forward_returns(df)
    fwd_t, market_baseline = timing_percentile(fwd)
    fwd_t.to_csv(OUT_DIR / "whale_branch_deepdive_9227_events_with_returns.csv", index=False)

    print(f"擇時百分位 (0=trailing20低點, 1=trailing20高點):")
    print(f"  9227 買進日: n={fwd_t['timing_pctile'].notna().sum()}, mean={fwd_t['timing_pctile'].mean():.3f}, median={fwd_t['timing_pctile'].median():.3f}")
    print(f"  市場基準(全市場全交易日): n={len(market_baseline)}, mean={market_baseline.mean():.3f}, median={market_baseline.median():.3f}")
    # ks test
    ks = stats.ks_2samp(fwd_t["timing_pctile"].dropna(), market_baseline)
    print(f"  KS test (9227 vs 市場基準): stat={ks.statistic:.4f}, p={ks.pvalue:.4g}")

    print("\nH9 excess return 統計檢定 (全部Top1買方日):")
    h9 = fwd_t["h9_excess"].dropna()
    n = len(h9)
    mean = h9.mean()
    tstat, pval = stats.ttest_1samp(h9, 0)
    ci = stats.t.interval(0.95, df=n - 1, loc=mean, scale=stats.sem(h9))
    print(f"  n={n}, mean={mean:.3f}%, t={tstat:.3f}, p={pval:.4g}, 95% CI=({ci[0]:.3f}%, {ci[1]:.3f}%)")

    # bootstrap
    rng = np.random.default_rng(42)
    boots = [np.mean(rng.choice(h9.values, size=n, replace=True)) for _ in range(2000)]
    boot_ci = (np.percentile(boots, 2.5), np.percentile(boots, 97.5))
    print(f"  Bootstrap 95% CI (2000 resamples): ({boot_ci[0]:.3f}%, {boot_ci[1]:.3f}%)")

    win_rate = (h9 > 0).mean() * 100
    print(f"  勝率: {win_rate:.1f}%")

    # robustness: winsorize / exclude top+bottom 5% extreme, check if still positive
    lo, hi = np.percentile(h9, [5, 95])
    h9_trim = h9[(h9 >= lo) & (h9 <= hi)]
    print(f"  去極端值後(5%~95% winsorize trim): n={len(h9_trim)}, mean={h9_trim.mean():.3f}%")

    # median (robust to outliers)
    print(f"  median H9 excess: {h9.median():.3f}%")

    # sensitivity: does removing top 3 largest positive outliers kill the effect?
    sorted_h9 = h9.sort_values(ascending=False)
    top3_removed_mean = sorted_h9.iloc[3:].mean()
    print(f"  移除最大的3個正報酬後 mean: {top3_removed_mean:.3f}% (n={len(sorted_h9)-3})")

    # by horizon summary
    print("\n各持有期 (H1/H3/H5/H9/H14/H20) excess return 摘要:")
    for h in (1, 3, 5, 9, 14, 20):
        col = f"h{h}_excess"
        s = fwd_t[col].dropna()
        if len(s) < 5:
            continue
        t, p = stats.ttest_1samp(s, 0)
        print(f"  H{h}: n={len(s)}, mean={s.mean():.3f}%, median={s.median():.3f}%, win%={100*(s>0).mean():.1f}, t={t:.2f}, p={p:.4g}")

    # (e) amount threshold sweep — does raising amount threshold sharpen signal?
    print("\n--- 金額門檻 sweep (更高門檻是否訊號更集中更強) ---")
    for thr_yi in (0.05, 0.1, 0.2, 0.3, 0.5, 1.0):
        sub = fwd_t[fwd_t["amount"] >= thr_yi * 1e8]
        s = sub["h9_excess"].dropna()
        if len(s) < 5:
            print(f"  金額>={thr_yi}億: n={len(s)} (太少，略過)")
            continue
        t, p = stats.ttest_1samp(s, 0)
        print(f"  金額>={thr_yi}億: n={len(s)}, mean={s.mean():.3f}%, win%={100*(s>0).mean():.1f}, t={t:.2f}, p={p:.4g}")

    print("\nDone. CSVs written to", OUT_DIR)


if __name__ == "__main__":
    main()
