#!/usr/bin/env python3
"""920F 深挖（本地部分）：用已算好的 L1H7 events.csv 做 (a) 全交易剖面、(c)/(d) 統計顯著性、
(e) 金額門檻 sweep。不需要連 mini 的 DB，因為 L1H7 forward return 已經算好在 CSV 裡。

Run:
  PYTHONPATH=src .venv/bin/python scripts/research/whale_branch_deepdive_920F_local.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
from scipy import stats

TRADER_ID = "920F"
OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"


def main():
    df = pd.read_csv(
        OUT_DIR / "whale_branch_l1h7_universe_events.csv",
        dtype={"stock_id": str, "securities_trader_id": str},
    )
    ev = df[df["securities_trader_id"] == TRADER_ID].copy()
    print(f"920F L1H7 events: n={len(ev)}, distinct stocks={ev['stock_id'].nunique()}")
    print(f"date range: {ev['signal_date'].min()} .. {ev['signal_date'].max()}")

    # (a) 全交易剖面
    ev["buy_amt_yi"] = ev["buy_amt"] / 1e8
    stock_stats = ev.groupby("stock_id")["buy_amt"].agg(["count", "sum", "median", "mean", "max"]).sort_values(
        "count", ascending=False
    )
    stock_stats.to_csv(OUT_DIR / "whale_branch_l1h7_deepdive_920F_stock_profile.csv")
    print("\n--- (a) 全交易剖面 ---")
    print(f"總計 Top1 買方 L1H7 事件數: {len(ev)}, 涵蓋股票數: {ev['stock_id'].nunique()}")
    print(f"買超金額(億元) 分布: median={ev['buy_amt_yi'].median():.3f}, mean={ev['buy_amt_yi'].mean():.3f}, max={ev['buy_amt_yi'].max():.3f}, min={ev['buy_amt_yi'].min():.3f}")
    months = pd.to_datetime(ev["signal_date"]).dt.to_period("M").nunique()
    print(f"活躍月數: {months}, 平均每月事件數: {len(ev)/months:.2f}")
    print("\nTop10 個股集中度 (事件數):")
    print(stock_stats.head(10).to_string())
    hh = (stock_stats["count"] / stock_stats["count"].sum()) ** 2
    print(f"\nHHI (以事件數計, 個股集中度): {hh.sum():.4f} (等效分散檔數 1/HHI={1/hh.sum():.1f})")
    single_event_stocks = (stock_stats["count"] == 1).sum()
    print(f"只出現1次的個股數: {single_event_stocks} / {len(stock_stats)} ({100*single_event_stocks/len(stock_stats):.1f}%)")

    # (c)/(d) 統計顯著性
    print("\n--- (c)/(d) forward return (r_adj_pct, L1H7 beta調整後超額報酬) 統計顯著性 ---")
    r = ev["r_adj_pct"].dropna()
    n = len(r)
    mean = r.mean()
    median = r.median()
    tstat, pval = stats.ttest_1samp(r, 0)
    ci = stats.t.interval(0.95, df=n - 1, loc=mean, scale=stats.sem(r))
    print(f"n={n}, mean={mean:.4f}%, median={median:.4f}%, t={tstat:.4f}, p={pval:.4g}, 95% CI (t-based)=({ci[0]:.3f}%, {ci[1]:.3f}%)")

    win_rate = (r > 0).mean() * 100
    print(f"勝率: {win_rate:.2f}%")

    # bootstrap on mean
    rng = np.random.default_rng(42)
    boots_mean = [np.mean(rng.choice(r.values, size=n, replace=True)) for _ in range(5000)]
    boot_ci_mean = (np.percentile(boots_mean, 2.5), np.percentile(boots_mean, 97.5))
    print(f"Bootstrap 95% CI on mean (5000 resamples): ({boot_ci_mean[0]:.4f}%, {boot_ci_mean[1]:.4f}%)")

    # bootstrap on median (more robust)
    boots_median = [np.median(rng.choice(r.values, size=n, replace=True)) for _ in range(5000)]
    boot_ci_median = (np.percentile(boots_median, 2.5), np.percentile(boots_median, 97.5))
    print(f"Bootstrap 95% CI on median (5000 resamples): ({boot_ci_median[0]:.4f}%, {boot_ci_median[1]:.4f}%)")

    # bootstrap on win rate
    boots_win = [(rng.choice(r.values, size=n, replace=True) > 0).mean() * 100 for _ in range(5000)]
    boot_ci_win = (np.percentile(boots_win, 2.5), np.percentile(boots_win, 97.5))
    print(f"Bootstrap 95% CI on win rate (5000 resamples): ({boot_ci_win[0]:.2f}%, {boot_ci_win[1]:.2f}%)")
    frac_boot_above_50 = np.mean(np.array(boots_win) > 50)
    print(f"  bootstrap 樣本中勝率>50%的比例: {frac_boot_above_50*100:.1f}%")
    frac_boot_mean_above_0 = np.mean(np.array(boots_mean) > 0)
    print(f"  bootstrap 樣本中 mean>0 的比例: {frac_boot_mean_above_0*100:.1f}%")

    # winsorize / trim extremes
    lo, hi = np.percentile(r, [5, 95])
    r_trim = r[(r >= lo) & (r <= hi)]
    print(f"\n去極端值後 (5%~95% trim): n={len(r_trim)}, mean={r_trim.mean():.4f}%, median={r_trim.median():.4f}%")

    # remove top3 positive outliers
    sorted_r = r.sort_values(ascending=False)
    top3_removed_mean = sorted_r.iloc[3:].mean()
    top3_removed_median = sorted_r.iloc[3:].median()
    print(f"移除最大的3個正報酬後: n={len(sorted_r)-3}, mean={top3_removed_mean:.4f}%, median={top3_removed_median:.4f}%")

    # remove bottom3 negative outliers too (symmetric extreme check)
    sorted_r_asc = r.sort_values()
    both3_removed = sorted_r_asc.iloc[3:-3]
    print(f"移除最大3正+最大3負報酬後: n={len(both3_removed)}, mean={both3_removed.mean():.4f}%, median={both3_removed.median():.4f}%")

    # net of cost sensitivity: cost already baked into r_pct (30bps). show what mean would be pre-cost
    # r_adj = r_s(after cost) - 1.15*r_ix; r_pct already has -0.3% cost subtracted at the stock leg.
    # So mean effect size vs the 30bps cost:
    print(f"\n30bps成本 vs mean效果量比較: mean(r_adj_pct)={mean:.4f}%，成本本身={0.30:.2f}%（cost已內含在r_pct，這裡只是量級對照）")

    # cross-check r_pct (raw, no beta adj) vs r_adj_pct
    r_raw = ev["r_pct"].dropna()
    r_ix = ev["r_ix_pct"].dropna()
    print(f"\n對照: raw r_pct(個股報酬-30bps成本, 未beta調整) mean={r_raw.mean():.4f}%, median={r_raw.median():.4f}%")
    print(f"對照: r_ix_pct(同期IX0001 L1H7報酬) mean={r_ix.mean():.4f}%, median={r_ix.median():.4f}%")

    # by month cohort — is the effect stable over time or driven by one period?
    print("\n--- 依訊號月份分組 (檢查效果是否集中在特定期間) ---")
    ev["month"] = pd.to_datetime(ev["signal_date"]).dt.to_period("M")
    month_stats = ev.groupby("month")["r_adj_pct"].agg(["count", "mean", "median"])
    print(month_stats.to_string())
    month_stats.to_csv(OUT_DIR / "whale_branch_l1h7_deepdive_920F_by_month.csv")

    # (e) 金額門檻 sweep
    print("\n--- (e) 買超金額門檻 sweep (更高門檻是否訊號更集中更強) ---")
    for thr_yi in (0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0):
        sub = ev[ev["buy_amt"] >= thr_yi * 1e8]
        s = sub["r_adj_pct"].dropna()
        if len(s) < 5:
            print(f"  金額>={thr_yi}億: n={len(s)} (太少，略過)")
            continue
        t, p = stats.ttest_1samp(s, 0)
        print(f"  金額>={thr_yi}億: n={len(s)}, mean={s.mean():.4f}%, median={s.median():.4f}%, win%={100*(s>0).mean():.1f}, t={t:.3f}, p={p:.4g}")

    ev.to_csv(OUT_DIR / "whale_branch_l1h7_deepdive_920F_events.csv", index=False)
    print("\nDone.")


if __name__ == "__main__":
    main()
