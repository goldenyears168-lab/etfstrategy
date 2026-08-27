#!/usr/bin/env python3
"""大跌溫度計 · contrarian 重新框架測試（2026-08-08）.

Research diagnostic · 非正式風控規則 · 非可下單訊號。

## 動機

`reports/research/branch-footprint-screen/crash_thermometer_lookahead_reaudit_20260727.md`
已用兩種獨立 OOS 方法證偽「分點賣超集中度預示大跌」這個看空前兆假設
（expanding-window walk-forward 判別力 39.9~47.3%、擴充歷史真正沒看過的
13 個事件 31.0~34.9%，皆低於或接近亂猜 50%）。

同時，`reports/research/dashboard-completeness/margin_maintenance.md`
第 4b 節對「近斷頭部位廣度」（概念上同一種「壓力集中度」訊號）做了同樣的
證偽，但額外測了一個從未在分點賣超溫度計上測過的**反向假設**：
不是「集中度高 → 要崩盤了」（已證偽），而是「集中度**極端** → 那已經是
恐慌 flush 之後、標記觸底」——結果 fwd20 lift +3.5%(p95)/+8.4%(p99)。

本腳本把同一個 contrarian 重新框架，套在**溫度計原本的分點賣超集中度公式**
上（完全複用，不重新定義）：直接 import
`run_market_crash_thermometer_dashboard.py` 的 `build_branch_panel` /
`compute_lb_pctile` / `weighted_composite` / 原 8 家 PANEL 與權重，算出
`composite_score`（0~1，1=賣超集中度最熱），然後測試「當日 composite_score
落在自身歷史極端尾部時，往後 5/10/20 個交易日的 IX0001 報酬是否顯著高於
無條件基準」——而不是原本已證偽的「是否預示大跌」。

沿用原研究的完整可得樣本（分點資料 2024-07-01 起，本地 DB 唯一可得範圍，
即原 look-ahead 重審報告使用的同一個 8 家 PANEL 與同一段歷史）。

輸出：
  reports/research/crash_thermometer_contrarian_reframe/composite_score_daily.csv
  reports/research/crash_thermometer_contrarian_reframe/contrarian_reframe_results.csv
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as st

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stock_db import DEFAULT_DB_PATH  # noqa: E402
from market_benchmark import load_benchmark_close  # noqa: E402

OUT = ROOT / "reports" / "research" / "crash_thermometer_contrarian_reframe"
OUT.mkdir(parents=True, exist_ok=True)

# ---- reuse the ORIGINAL thermometer module's exact metric definition ----
_spec = importlib.util.spec_from_file_location(
    "crash_thermometer_dashboard",
    ROOT / "scripts" / "research" / "run_market_crash_thermometer_dashboard.py",
)
therm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(therm)  # type: ignore[union-attr]

PANEL = therm.PANEL  # dict[branch_id] -> (name, weight)
WEIGHTS = {bid: w for bid, (_name, w) in PANEL.items()}
IDS = list(PANEL.keys())
LOOKBACK_DAYS = therm.LOOKBACK_DAYS_DEFAULT
SELF_RANK_DAYS = therm.SELF_RANK_DAYS_DEFAULT
CRASH_THRESHOLD = therm.CRASH_THRESHOLD if hasattr(therm, "CRASH_THRESHOLD") else -0.03
RALLY_THRESHOLD = therm.RALLY_THRESHOLD if hasattr(therm, "RALLY_THRESHOLD") else 0.03

RNG = np.random.default_rng(11)
ANN = np.sqrt(252)


def sharpe(x):
    x = pd.Series(x).dropna()
    return x.mean() / x.std() * ANN if len(x) > 20 and x.std() > 0 else np.nan


def perm_p_mean(sample: np.ndarray, pool: np.ndarray, n: int = 5000) -> float:
    """One-sided permutation test: is mean(sample) higher than a random
    same-size draw from pool (drawn WITHOUT replacement, i.i.d. days)?"""
    k = len(sample)
    obs = float(np.nanmean(sample))
    N = len(pool)
    if k == 0 or k >= N:
        return np.nan
    null = np.array([np.nanmean(RNG.choice(pool, size=k, replace=False)) for _ in range(n)])
    return float((null >= obs).mean())


def block_perm_p_mean(sample_dates, all_dates, values_by_date: dict, block: int = 10, n: int = 5000) -> float:
    """Block-bootstrap permutation to respect autocorrelation of overlapping
    forward-return windows: draw contiguous blocks of `block` consecutive
    calendar-index days from the full series, same total sample size as the
    real extreme-reading count, compare mean."""
    idx_of = {d: i for i, d in enumerate(all_dates)}
    k = len(sample_dates)
    obs = float(np.nanmean([values_by_date[d] for d in sample_dates if d in values_by_date]))
    N = len(all_dates)
    if k == 0 or N < block:
        return np.nan
    n_blocks = max(1, k // block)
    remainder = k - n_blocks * block
    null_means = []
    vals_arr = np.array([values_by_date.get(d, np.nan) for d in all_dates])
    for _ in range(n):
        starts = RNG.integers(0, N - block, size=n_blocks)
        picked = np.concatenate([vals_arr[s : s + block] for s in starts])
        if remainder:
            s = RNG.integers(0, N - remainder)
            picked = np.concatenate([picked, vals_arr[s : s + remainder]])
        null_means.append(np.nanmean(picked))
    null_means = np.array(null_means)
    return float((null_means >= obs).mean())


def main() -> None:
    conn = sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)

    latest = therm.latest_trade_date(conn)
    full_cal = therm.build_calendar(conn, end=latest, n_days=10000, extra_ids=IDS)

    # IMPORTANT: `stock_broker_branch_daily` for this 8-branch PANEL only starts
    # 2024-07-01 (same real-data window the original crash-thermometer study used).
    # build_full_grid() zero-fills any (branch, date) combo with no panel row, so
    # extending the calendar further back than the branch data's real start would
    # feed compute_lb_pctile() a long stretch of degenerate all-zero net_amt days
    # (net_amt=0 for every branch => lb_sum ties => degenerate composite_score).
    # Restrict to the real panel data span, same as the production dashboard script
    # restricting via n_days=self_rank_days+lookback_days+5 from asof.
    branch_start_row = conn.execute(
        f"SELECT MIN(trade_date) FROM stock_broker_branch_daily WHERE source=? "
        f"AND securities_trader_id IN ({','.join('?' for _ in IDS)})",
        (therm.SOURCE, *IDS),
    ).fetchone()
    branch_start = str(branch_start_row[0])
    cal = [d for d in full_cal if d >= branch_start]
    print(f"[cal] real branch-data window: {len(cal)} trading days, {cal[0]}..{cal[-1]} "
          f"(full calendar back to {full_cal[0]} would zero-pad pre-{branch_start} days)")

    panel = therm.build_branch_panel(conn, IDS, start=cal[0], end=cal[-1])
    grid = therm.build_full_grid(panel, IDS, cal)
    scored = therm.compute_lb_pctile(grid, LOOKBACK_DAYS, SELF_RANK_DAYS)
    comp = therm.weighted_composite(scored, WEIGHTS)  # trade_date, composite_score, consensus_n
    comp = comp.dropna(subset=["composite_score"]).sort_values("trade_date").reset_index(drop=True)
    print(f"[composite] {len(comp)} days with valid composite_score "
          f"({comp['trade_date'].min()}..{comp['trade_date'].max()})")

    bench = load_benchmark_close(conn, code="IX0001").sort_index()
    bench.index = bench.index.astype(str)

    df = comp.merge(bench.rename("ix_close"), left_on="trade_date", right_index=True, how="inner")
    df = df.sort_values("trade_date").reset_index(drop=True)
    df["fwd5"] = df["ix_close"].shift(-5) / df["ix_close"] - 1.0
    df["fwd10"] = df["ix_close"].shift(-10) / df["ix_close"] - 1.0
    df["fwd20"] = df["ix_close"].shift(-20) / df["ix_close"] - 1.0
    df["ret1"] = df["ix_close"].pct_change()

    df.to_csv(OUT / "composite_score_daily.csv", index=False)
    print(f"-> {OUT/'composite_score_daily.csv'} ({len(df)} rows)")

    # original crash/rally event dates from the SAME thermometer module
    crash_dates, event_dates = therm.load_event_dates(conn, cal)
    print(f"[events] {len(crash_dates)} crash days (<= {CRASH_THRESHOLD:.0%}), "
          f"{len(event_dates)} total crash+rally days in this DB's history")

    results = []

    n_total = len(df)
    unconditional = {h: df[h].mean() for h in ("fwd5", "fwd10", "fwd20")}
    unconditional_med = {h: df[h].median() for h in ("fwd5", "fwd10", "fwd20")}
    print("\n=== unconditional baseline (all days, full sample) ===")
    for h in ("fwd5", "fwd10", "fwd20"):
        print(f"  {h}: mean={unconditional[h]:+.3%} median={unconditional_med[h]:+.3%} n={df[h].notna().sum()}")

    # ---- CONTRARIAN test: extreme composite_score -> forward return lift ----
    print("\n=== contrarian reframe: extreme composite_score -> fwd IX0001 return ===")
    print("(composite_score near 1.0 = most extreme branch sell-concentration = 'hottest' reading)")
    for q in (0.80, 0.90, 0.95, 0.99):
        thr = df["composite_score"].quantile(q)
        hi = df["composite_score"] >= thr
        n = int(hi.sum())
        row = {"method": "in_sample_quantile", "q": q, "thr": thr, "n": n}
        for h in ("fwd5", "fwd10", "fwd20"):
            samp = df.loc[hi, h].dropna()
            lift = samp.mean() - unconditional[h]
            tstat, pval_t = st.ttest_ind(samp, df[h].dropna(), equal_var=False) if len(samp) > 1 else (np.nan, np.nan)
            row[f"{h}_mean"] = samp.mean()
            row[f"{h}_median"] = samp.median()
            row[f"{h}_lift"] = lift
            row[f"{h}_welch_p"] = pval_t
        print(f"  p{int(q*100)} (thr={thr:.3f}, n={n}d): "
              f"fwd5 {row['fwd5_mean']:+.3%}(lift {row['fwd5_lift']:+.3%}, p={row['fwd5_welch_p']:.3f}) | "
              f"fwd10 {row['fwd10_mean']:+.3%}(lift {row['fwd10_lift']:+.3%}, p={row['fwd10_welch_p']:.3f}) | "
              f"fwd20 {row['fwd20_mean']:+.3%}(lift {row['fwd20_lift']:+.3%}, p={row['fwd20_welch_p']:.3f})")
        results.append(row)

    # ---- walk-forward-safe version: expanding self-quantile threshold (no look-ahead in the threshold itself) ----
    print("\n=== same test, but threshold computed WALK-FORWARD (expanding quantile, no look-ahead) ===")
    scores = df["composite_score"].to_numpy()
    n = len(scores)
    MIN_HIST = 60
    for q in (0.90, 0.95):
        flags = np.zeros(n, dtype=bool)
        for i in range(MIN_HIST, n):
            hist = scores[:i]
            thr_i = np.quantile(hist, q)
            flags[i] = scores[i] >= thr_i
        hi = pd.Series(flags, index=df.index)
        cnt = int(hi.sum())
        row = {"method": "walkforward_expanding_quantile", "q": q, "thr": np.nan, "n": cnt}
        for h in ("fwd5", "fwd10", "fwd20"):
            samp = df.loc[hi, h].dropna()
            lift = samp.mean() - unconditional[h]
            row[f"{h}_mean"] = samp.mean()
            row[f"{h}_median"] = samp.median()
            row[f"{h}_lift"] = lift
        print(f"  p{int(q*100)} walk-forward (n={cnt}d): "
              f"fwd5 {row['fwd5_mean']:+.3%}(lift {row['fwd5_lift']:+.3%}) | "
              f"fwd10 {row['fwd10_mean']:+.3%}(lift {row['fwd10_lift']:+.3%}) | "
              f"fwd20 {row['fwd20_mean']:+.3%}(lift {row['fwd20_lift']:+.3%})")
        results.append(row)

    # ---- IS/OOS time split (70/30), same spirit as margin_nearcall_breadth_study ----
    cut = int(n * 0.7)
    is_df, oos_df = df.iloc[:cut], df.iloc[cut:]
    print(f"\n=== IS/OOS split (cut@{cut}: IS {is_df['trade_date'].min()}..{is_df['trade_date'].max()}, "
          f"OOS {oos_df['trade_date'].min()}..{oos_df['trade_date'].max()}) ===")
    for label, part in (("IS", is_df), ("OOS", oos_df)):
        thr95 = part["composite_score"].quantile(0.95) if label == "IS" else is_df["composite_score"].quantile(0.95)
        hi = part["composite_score"] >= thr95
        n_hi = int(hi.sum())
        base = part["fwd20"].mean()
        samp = part.loc[hi, "fwd20"].dropna()
        lift = (samp.mean() - base) if n_hi else np.nan
        print(f"  {label}: p95 thr(from IS)={thr95:.3f} n_hi={n_hi} base_fwd20={base:+.3%} "
              f"hi_fwd20={samp.mean() if n_hi else float('nan'):+.3%} lift={lift:+.3%}")
        results.append({"method": f"IS_OOS_{label}", "q": 0.95, "thr": thr95, "n": n_hi,
                         "fwd20_mean": samp.mean() if n_hi else np.nan,
                         "fwd20_lift": lift})

    # ---- significance: block-bootstrap permutation on the p95/p99 fwd20 lift (respects
    #      autocorrelation from overlapping 20-day forward windows + event clustering) ----
    print("\n=== significance: block-bootstrap permutation (block=10d, n=5000), fwd20 ===")
    all_dates = df["trade_date"].tolist()
    values_by_date = dict(zip(df["trade_date"], df["fwd20"]))
    for q in (0.90, 0.95, 0.99):
        thr = df["composite_score"].quantile(q)
        hi_dates = df.loc[df["composite_score"] >= thr, "trade_date"].tolist()
        p = block_perm_p_mean(hi_dates, all_dates, values_by_date, block=10, n=5000)
        print(f"  p{int(q*100)} (n={len(hi_dates)}): block-perm p={p:.4f} "
              f"(fraction of random {len(hi_dates)}-day blocks-of-10 samples with mean fwd20 >= observed)")
        results.append({"method": "block_perm_p", "q": q, "n": len(hi_dates), "fwd20_block_perm_p": p})

    # ---- cross-check: composite_score reading ON the actual historical crash days themselves ----
    print("\n=== composite_score ON actual historical crash days (IX0001 daily ret <= -3%) ===")
    crash_rows = df[df["trade_date"].isin(crash_dates)]
    if len(crash_rows):
        print(f"  n={len(crash_rows)} crash days in overlap window; "
              f"composite_score mean={crash_rows['composite_score'].mean():.3f} "
              f"(percentile of full-sample dist: "
              f"{st.percentileofscore(df['composite_score'], crash_rows['composite_score'].mean()):.1f}%)")
        print(f"  fwd5/10/20 AFTER the crash day itself: "
              f"{crash_rows['fwd5'].mean():+.3%} / {crash_rows['fwd10'].mean():+.3%} / "
              f"{crash_rows['fwd20'].mean():+.3%}  (vs unconditional "
              f"{unconditional['fwd5']:+.3%} / {unconditional['fwd10']:+.3%} / {unconditional['fwd20']:+.3%})")
        results.append({
            "method": "on_crash_day_itself", "n": len(crash_rows),
            "composite_score_mean": crash_rows["composite_score"].mean(),
            "fwd5_mean": crash_rows["fwd5"].mean(), "fwd10_mean": crash_rows["fwd10"].mean(),
            "fwd20_mean": crash_rows["fwd20"].mean(),
        })
    else:
        print("  no overlap (crash dates fall outside the branch-data / fwd-return window)")

    pd.DataFrame(results).to_csv(OUT / "contrarian_reframe_results.csv", index=False)
    print(f"\n-> {OUT/'contrarian_reframe_results.csv'}")


if __name__ == "__main__":
    main()
