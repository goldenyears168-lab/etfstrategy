#!/usr/bin/env python3
"""9216 (凱基-信義) deep dive — Top1-3 buyer, multi-horizon candidate from round-3
broadened screen (n=250, H7 mean+1.89% p=0.030, H14 mean+5.06% p=0.00016,
H20 mean+5.92% p=0.00052).

Replicates the 9227_v2 / 9217_v2 deepdive spec:
  (a) full trading profile (rn distribution, stock concentration, amount profile)
  (b) position-building pattern (top 8-10 stocks, cumulative net position)
  (c) timing quality (20d high/low percentile vs market baseline)
  (d) statistical robustness (t-test, wilcoxon, bootstrap CI, winsorize, outlier removal)
  (e) whale-crossing overlap + self buy/sell role-switch frequency (market-making check)
  (f) H7 vs H14 vs H20 decomposition: is late-horizon alpha "new" contribution
      (H14-H7, H20-H14 legs) or just accumulated noise from a heavier tail?

Run on mini via SSH (stock_broker_branch_daily is NOT reliable in Book's local copy):
  ssh -o BatchMode=yes mac-mini-lan "cd '/Users/jackm4/goldenstocks' && \
    source .venv/bin/activate 2>/dev/null && PYTHONPATH=src python3 \
    scripts/research/whale_branch_deepdive_9216.py"
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
from scipy import stats

from market_benchmark import load_benchmark_close
from stock_db import DEFAULT_DB_PATH, connect

TRADER_ID = "9216"
DATE_START = "2024-07-01"
DATE_END = "2026-07-24"

OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"
EVENTS_PATH = OUT_DIR / "whale_branch_top3_multihorizon_events.csv"


def main():
    conn = connect(DEFAULT_DB_PATH)
    bench = load_benchmark_close(conn, code="IX0001")
    print(f"[INFO] benchmark rows: {len(bench)}")

    events = pd.read_csv(EVENTS_PATH)
    sub = events[events["securities_trader_id"] == "9216"].copy()
    print(f"[INFO] 9216 events (rn 1-3, from round-3 screen): n={len(sub)}")

    print("\n=== (a) 全交易剖面 ===")
    print("rn 分布 (1=唯一/最大買方, 2/3=第二三名):")
    print(sub["rn"].value_counts().sort_index())
    print(f"\nrn==1 佔比: {(sub['rn']==1).mean():.3f}")
    print(f"\nshare_of_day_buy 分布: {sub['share_of_day_buy'].describe()}")
    print(f"\nbuy_amt(萬元) 分布: median={sub['buy_amt'].median()/1e4:.1f}, mean={sub['buy_amt'].mean()/1e4:.1f}, max={sub['buy_amt'].max()/1e4:.1f}")
    stock_counts = sub["stock_id"].value_counts()
    print(f"\n distinct stocks: {sub['stock_id'].nunique()}")
    print("Top 15 個股事件數:")
    print(stock_counts.head(15))
    hh = (stock_counts / stock_counts.sum()) ** 2
    print(f"HHI: {hh.sum():.4f} (1/N_eff={1/hh.sum():.1f})")
    print(f"\n訊號日期範圍: {sub['signal_date'].min()} ~ {sub['signal_date'].max()}")
    # active trading days count (how many distinct dates has 9216 activity, any stock)
    all_dates_active = pd.read_sql_query(
        "SELECT DISTINCT trade_date FROM stock_broker_branch_daily WHERE securities_trader_id=? AND trade_date>=? AND trade_date<=?",
        conn, params=(TRADER_ID, DATE_START, DATE_END),
    )
    total_trading_dates = pd.read_sql_query(
        "SELECT DISTINCT trade_date FROM stock_broker_branch_daily WHERE trade_date>=? AND trade_date<=?",
        conn, params=(DATE_START, DATE_END),
    )
    print(f"\n9216 有出手的交易日數: {len(all_dates_active)} / 全市場交易日總數: {len(total_trading_dates)} "
          f"({len(all_dates_active)/len(total_trading_dates):.3f})")

    print("\n=== (d) 統計顯著性 & 穩健性 (逐 horizon) ===")
    for h in ("h7", "h14", "h20"):
        col = f"r_adj_{h}_pct"
        x = sub[col].dropna().values
        n = len(x)
        t, p = stats.ttest_1samp(x, 0)
        w, pw = stats.wilcoxon(x)
        rng = np.random.default_rng(7)
        boots = [rng.choice(x, size=n, replace=True).mean() for _ in range(5000)]
        lo, hi = np.percentile(boots, [2.5, 97.5])
        win5 = stats.mstats.winsorize(x, limits=[0.05, 0.05]).mean()
        win10 = stats.mstats.winsorize(x, limits=[0.10, 0.10]).mean()
        srt = np.sort(x)[::-1]
        drop_top10 = srt[10:].mean()
        drop_top20 = srt[20:].mean()
        print(f"\n{h.upper()}: n={n}, mean={x.mean():.3f}%, median={np.median(x):.3f}%, win%={100*(x>0).mean():.1f}")
        print(f"  t-test p={p:.5f} | wilcoxon p={pw:.5f} | bootstrap95%CI=({lo:.2f}%,{hi:.2f}%)")
        print(f"  winsorize5%_mean={win5:.3f}% winsorize10%_mean={win10:.3f}%")
        print(f"  drop_top10_winners_mean={drop_top10:.3f}% (n={n-10}) drop_top20_winners_mean={drop_top20:.3f}% (n={n-20})")

    print("\n=== (f) H7/H14/H20 遞增關係分解 (是否訊號需要時間發酵) ===")
    d = sub[["r_adj_h7_pct", "r_adj_h14_pct", "r_adj_h20_pct"]].dropna()
    d["leg_h7"] = d["r_adj_h7_pct"]
    d["leg_h14_minus_h7"] = d["r_adj_h14_pct"] - d["r_adj_h7_pct"]
    d["leg_h20_minus_h14"] = d["r_adj_h20_pct"] - d["r_adj_h14_pct"]
    for leg in ("leg_h7", "leg_h14_minus_h7", "leg_h20_minus_h14"):
        x = d[leg].values
        t, p = stats.ttest_1samp(x, 0)
        w, pw = stats.wilcoxon(x)
        print(f"  {leg}: mean={x.mean():.3f}%, median={np.median(x):.3f}%, t-p={p:.5f}, wilcoxon-p={pw:.5f}, win%={100*(x>0).mean():.1f}")

    print("\n=== (c) 擇時品質（signal_date, 20日高低百分位）===")
    stock_ids = sub["stock_id"].astype(str).unique().tolist()
    placeholders = ",".join("?" * len(stock_ids))
    bars = pd.read_sql_query(
        f"""SELECT stock_id, trade_date, close FROM stock_daily_bars
            WHERE source='finmind' AND stock_id IN ({placeholders}) ORDER BY stock_id, trade_date""",
        conn, params=stock_ids,
    )
    bars["roll_min20"] = bars.groupby("stock_id")["close"].transform(lambda s: s.rolling(20, min_periods=10).min())
    bars["roll_max20"] = bars.groupby("stock_id")["close"].transform(lambda s: s.rolling(20, min_periods=10).max())
    bars["pctile"] = (bars["close"] - bars["roll_min20"]) / (bars["roll_max20"] - bars["roll_min20"])
    key = bars.set_index(["stock_id", "trade_date"])["pctile"]
    tp_vals = []
    for row in sub.itertuples():
        tp_vals.append(key.get((str(row.stock_id), row.signal_date), np.nan))
    tp = pd.Series(tp_vals)
    market_baseline = bars["pctile"].dropna()
    print(f"9216 訊號日擇時百分位: n={tp.notna().sum()}, mean={tp.mean():.3f}, median={tp.median():.3f}")
    print(f"市場基準: n={len(market_baseline)}, mean={market_baseline.mean():.3f}, median={market_baseline.median():.3f}")
    ks = stats.ks_2samp(tp.dropna(), market_baseline)
    print(f"KS test vs market baseline: stat={ks.statistic:.4f}, p={ks.pvalue:.4g}")

    print("\n=== (b) 建倉型態（事件數最多的前 10 檔）===")
    top_stocks = stock_counts.head(10).index.tolist()
    all_trades = pd.read_sql_query(
        "SELECT trade_date, stock_id, buy, sell, net FROM stock_broker_branch_daily WHERE source='finmind' AND securities_trader_id=? AND trade_date>=? ORDER BY stock_id, trade_date",
        conn, params=(TRADER_ID, DATE_START),
    )
    for sid in top_stocks:
        g = all_trades[all_trades["stock_id"].astype(str) == str(sid)].sort_values("trade_date").copy()
        g["cum_net"] = g["net"].cumsum()
        n_days = len(g)
        n_buy_days = (g["buy"] > 0).sum()
        n_sell_days = (g["sell"] > 0).sum()
        n_both_days = ((g["buy"] > 0) & (g["sell"] > 0)).sum()
        maxp, minp, finalp = g["cum_net"].max(), g["cum_net"].min(), g["cum_net"].iloc[-1]
        print(f"  {sid}: n_days={n_days}, buy_days={n_buy_days}, sell_days={n_sell_days}, both_days={n_both_days}, "
              f"max_cum={maxp:,.0f}, min_cum={minp:,.0f}, final_cum={finalp:,.0f}, "
              f"final/max={finalp/maxp if maxp else float('nan'):.2f}")

    print("\n=== (e) 造市/當沖嫌疑檢查：買賣角色切換頻率 & 全樣本 buy/sell balance ===")
    all_trades_full = pd.read_sql_query(
        "SELECT trade_date, stock_id, buy, sell, net FROM stock_broker_branch_daily WHERE source='finmind' AND securities_trader_id=? AND trade_date>=? AND trade_date<=?",
        conn, params=(TRADER_ID, DATE_START, DATE_END),
    )
    total_buy = all_trades_full["buy"].sum()
    total_sell = all_trades_full["sell"].sum()
    print(f"全樣本 (所有股票, {DATE_START}~{DATE_END}): total_buy_shares={total_buy:,.0f}, total_sell_shares={total_sell:,.0f}, "
          f"sell/buy ratio={total_sell/total_buy if total_buy else float('nan'):.3f}")
    both_side_days = ((all_trades_full["buy"] > 0) & (all_trades_full["sell"] > 0)).sum()
    print(f"同日買+賣同一檔股票的 row 數: {both_side_days} / {len(all_trades_full)} ({both_side_days/len(all_trades_full):.3f})")

    sub.to_csv(OUT_DIR / "whale_branch_deepdive_9216_events.csv", index=False)
    print("\nDone.")


if __name__ == "__main__":
    main()
