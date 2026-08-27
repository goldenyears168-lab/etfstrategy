"""Item A of the creative-combination research plan (2026-08-07):

Does gating the 9217 (凱基-松山) broker-branch-follow signal by the chip-macro
market-timing regime improve it?

Two independently-validated signals, combined for the first time here:

1. 9217 branch-follow (`scan_5d_net95`): buy_5d(5-day cumulative buy amount)>=0.5億
   AND net_ratio(5-day (buy-sell)/buy)>=0.95. Decisive study =
   reports/research/branch-footprint-screen/whale_9217_5dnet95_trades.csv (n=36),
   validated with a per-stock date-permutation test in
   scripts/research/study_whale_9217_permutation_significance_test.py
   (observed mean r_adj=+5.66%, median=+6.28%, p_mean=0.005, p_median≈0.0).
   r_adj_pct there = beta(1.15)-adjusted excess return over IX0001, L1 (T+1 open
   entry) H7 (7-trading-day hold to close), cost=0.3%. This script reuses that
   n=36 trade list and its r_adj_pct column EXACTLY — no re-derivation.

2. chip-macro regime gate, from scripts/research/chip_macro/finalize_cc.py:
     chip     = zscore(fut_foreign_oi, window=60) > 0
     bull200  = ix_close > ix_close.rolling(200).mean()
     regime_bull = chip & bull200      (the "chip x bull(>MA200)" champion combo,
                                         OOS Sharpe +2.49 standalone on the TAIEX
                                         index-timing question)
   Both chip and bull200 are computed causally (trailing rolling windows only),
   consistent with the panel's documented convention that every row's chip value
   is "known after the close of day T" -- i.e. this is PIT-correct for tagging
   the SAME day-T that a 9217 signal fires on (both decided post-close of T,
   entered at T+1 open).

What this script does:
  - Tags each of the 36 real 9217 signal dates with regime_bull (True/False) as
    of that date, using the SAME shared chip-macro panel used for the champion
    combo (data/research/chip_macro/panel.parquet).
  - Splits the 36 trades into regime-bull vs regime-bear buckets and reports
    n / mean / median / win-rate of r_adj_pct per bucket.
  - Runs a date-label permutation test: shuffle the regime_bull label ACROSS
    THE FULL VALID-DATE POPULATION (not across trades), re-tag the 36 trades
    under each shuffled labeling, and build a null distribution of the
    bull-minus-bear mean difference. This tests whether the observed bucket
    split could arise from an arbitrary regime relabeling with the same base
    rate, honestly (does NOT assume trade dates are large-n or iid).

Read-only DB access is NOT needed here (chip panel and trades CSV are already
materialized on disk); no sqlite3 connection is opened.

Outputs (under reports/research/regime_gate_branch_fusion/):
  - trades_regime_tagged.csv
  - permutation_result.json
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
PANEL_CSV = ROOT / "data" / "research" / "chip_macro" / "panel.csv"
TRADES_CSV = ROOT / "reports" / "research" / "branch-footprint-screen" / "whale_9217_5dnet95_trades.csv"
OUT_DIR = ROOT / "reports" / "research" / "regime_gate_branch_fusion"
OUT_TRADES = OUT_DIR / "trades_regime_tagged.csv"
OUT_JSON = OUT_DIR / "permutation_result.json"

N_PERM = 5000
SEED = 20260807


def build_regime_series() -> pd.DataFrame:
    p = pd.read_csv(PANEL_CSV).sort_values("date").reset_index(drop=True)
    fo = p["fut_foreign_oi"]
    z60 = (fo - fo.rolling(60, min_periods=60).mean()) / fo.rolling(60, min_periods=60).std()
    chip = z60 > 0
    ma200 = p["ix_close"].rolling(200, min_periods=200).mean()
    bull200 = p["ix_close"] > ma200
    defined = z60.notna() & ma200.notna()
    p["chip_z60"] = z60
    p["chip_gt0"] = chip
    p["bull200"] = bull200
    p["regime_defined"] = defined
    p["regime_bull"] = chip & bull200
    return p


def permutation_diff_test(
    dates_pop: np.ndarray,
    labels_pop: np.ndarray,
    trade_dates: np.ndarray,
    trade_returns: np.ndarray,
    n_perm: int,
    seed: int,
) -> dict:
    """Shuffle labels_pop across dates_pop (population-level), re-tag trades, compute
    bull-mean minus bear-mean each iteration. Observed stat uses the REAL labeling.
    """
    rng = np.random.default_rng(seed)
    date_to_idx = {d: i for i, d in enumerate(dates_pop)}
    trade_idx = np.array([date_to_idx[d] for d in trade_dates])  # position in population array

    real_labels_for_trades = labels_pop[trade_idx]
    obs_bull_mean = float(trade_returns[real_labels_for_trades].mean())
    obs_bear_mean = float(trade_returns[~real_labels_for_trades].mean())
    obs_diff = obs_bull_mean - obs_bear_mean
    n_bull = int(real_labels_for_trades.sum())
    n_bear = int((~real_labels_for_trades).sum())

    perm_diffs = np.full(n_perm, np.nan)
    base = labels_pop.copy()
    for i in range(n_perm):
        shuffled = rng.permutation(base)
        lab_for_trades = shuffled[trade_idx]
        if lab_for_trades.all() or (~lab_for_trades).all():
            continue  # degenerate: skip (needs both buckets non-empty)
        bm = trade_returns[lab_for_trades].mean()
        brm = trade_returns[~lab_for_trades].mean()
        perm_diffs[i] = bm - brm

    valid = perm_diffs[~np.isnan(perm_diffs)]
    p_two_sided = float(np.mean(np.abs(valid) >= abs(obs_diff))) if len(valid) else float("nan")
    p_one_sided_bull_gt_bear = float(np.mean(valid >= obs_diff)) if len(valid) else float("nan")

    return {
        "n_events": int(len(trade_dates)),
        "n_bull_events": n_bull,
        "n_bear_events": n_bear,
        "population_base_rate_bull": float(labels_pop.mean()),
        "observed_bull_mean_pct": obs_bull_mean,
        "observed_bear_mean_pct": obs_bear_mean,
        "observed_diff_pct": obs_diff,
        "n_perm": int(n_perm),
        "n_valid_perms": int(len(valid)),
        "null_diff_mean_pct": float(np.mean(valid)) if len(valid) else None,
        "null_diff_std_pct": float(np.std(valid)) if len(valid) else None,
        "p_value_two_sided": p_two_sided,
        "p_value_one_sided_bull_gt_bear": p_one_sided_bull_gt_bear,
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    panel = build_regime_series()
    trades = pd.read_csv(TRADES_CSV, dtype={"stock_id": str, "signal_date": str})

    merged = trades.merge(
        panel[["date", "chip_z60", "chip_gt0", "bull200", "regime_bull", "regime_defined"]],
        left_on="signal_date",
        right_on="date",
        how="left",
    )
    missing = merged["regime_bull"].isna().sum()
    print(f"[INFO] trades n={len(trades)}, missing regime lookup={missing}")
    assert missing == 0, "every 9217 signal date must resolve to a panel regime label"
    assert merged["regime_defined"].all(), "chip/ma200 must be defined (non-NaN) for all 36 signal dates"

    merged = merged.drop(columns=["date"])
    merged.to_csv(OUT_TRADES, index=False)
    print(f"[OK] wrote {OUT_TRADES}")

    for col in ["r_pct", "r_ix_pct", "r_adj_pct"]:
        g = merged.groupby("regime_bull")[col].agg(
            n="count", mean="mean", median="median", win_rate=lambda s: float((s > 0).mean())
        )
        print(f"\n[BUCKET SUMMARY] {col}")
        print(g.to_string())

    # primary metric = r_adj_pct (exact match to decisive-study permutation test metric)
    dates_pop = panel.loc[panel["regime_defined"], "date"].to_numpy()
    labels_pop = panel.loc[panel["regime_defined"], "regime_bull"].to_numpy()

    result = permutation_diff_test(
        dates_pop=dates_pop,
        labels_pop=labels_pop,
        trade_dates=merged["signal_date"].to_numpy(),
        trade_returns=merged["r_adj_pct"].to_numpy(),
        n_perm=N_PERM,
        seed=SEED,
    )
    result["metric"] = "r_adj_pct (beta=1.15-adjusted excess return vs IX0001, L1H7, cost=0.3%)"
    result["method"] = (
        "date-label permutation: shuffle regime_bull across the full population of "
        "dates where chip_z60/ma200 are both defined (n dates = "
        f"{len(dates_pop)}), re-tag the 36 real 9217 signal dates under each shuffle, "
        "compute bull-mean minus bear-mean of r_adj_pct each iteration. Labels are "
        "shuffled across DATES, not across trades -- trade composition (which stock, "
        "which signal date) is held fixed; only the regime-label lookup is randomized."
    )

    print("\n" + "=" * 88)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print("=" * 88)

    OUT_JSON.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[OK] wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
