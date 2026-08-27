"""item AO (wave 8) — does combining fgap (smallest-qualifying-gap pick rule) with
si_lend_pct (institutional securities-lending level, item R finding) beat either
factor alone for dayflip-short candidate quality?

Reuses reports/research/asquith_dayflip_crosscheck/trades_with_si.csv (190
reconstructed dayflip-short trades; already has both fgap and si_lend_pct joined).
Read-only analysis, no order/strategy config touched.

Outputs:
  reports/research/dayflip_lending_gap_twofactor/subset_with_ranks.csv
  reports/research/dayflip_lending_gap_twofactor/summary.json
"""
import json

import numpy as np
import pandas as pd
from scipy import stats

SRC = "reports/research/asquith_dayflip_crosscheck/trades_with_si.csv"
OUT_DIR = "reports/research/dayflip_lending_gap_twofactor"


def tercile_report(df: pd.DataFrame, col: str, label: str) -> pd.DataFrame:
    d = df.copy()
    d["tercile"] = pd.qcut(d[col].rank(method="first"), 3, labels=["T1_best", "T2_mid", "T3_worst"])
    g = d.groupby("tercile", observed=True).agg(
        n=("pnl_pct", "size"), mean_pnl=("pnl_pct", "mean"), hitrate=("hit", "mean")
    )
    print(f"\n--- {label} ---")
    print(g)

    t1 = d.loc[d["tercile"] == "T1_best", "pnl_pct"].values
    t3 = d.loc[d["tercile"] == "T3_worst", "pnl_pct"].values
    obs_diff = t1.mean() - t3.mean()
    pooled = np.concatenate([t1, t3])
    n1 = len(t1)
    rng = np.random.default_rng(42)
    diffs = np.empty(20000)
    for i in range(20000):
        perm = rng.permutation(pooled)
        diffs[i] = perm[:n1].mean() - perm[n1:].mean()
    p_perm = (np.sum(np.abs(diffs) >= abs(obs_diff)) + 1) / (20000 + 1)
    print(f"T1 vs T3 pnl diff: {obs_diff:.3f}pp, permutation p={p_perm:.4f}")
    print(f"T1 hitrate {t1.mean() if False else d.loc[d['tercile']=='T1_best','hit'].mean():.3f} "
          f"(n={len(t1)}) vs T3 hitrate {d.loc[d['tercile']=='T3_worst','hit'].mean():.3f} (n={len(t3)})")
    return g


def main() -> None:
    si = pd.read_csv(SRC)
    df = si[si["si_lend_pct"].notnull() & si["fgap"].notnull() & si["pnl_pct"].notnull()].copy()
    print("n subset:", len(df), "unique stocks:", df["stock"].nunique())

    rho, p = stats.spearmanr(df["fgap"], df["si_lend_pct"])
    print(f"Spearman fgap vs si_lend_pct: rho={rho:.3f}, p={p:.4f}")

    df["rank_gap"] = df["fgap"].rank(method="average")  # 1 = smallest gap = best (production pick rule)
    df["rank_lend"] = df["si_lend_pct"].rank(method="average")  # 1 = lowest lending = best (item R)
    df["rank_combined"] = (df["rank_gap"] + df["rank_lend"]) / 2

    tercile_report(df, "rank_gap", "GAP ALONE (T1=smallest gap)")
    tercile_report(df, "rank_lend", "LENDING ALONE (T1=lowest lending)")
    tercile_report(df, "rank_combined", "COMBINED (avg rank, T1=best on both)")

    df.to_csv(f"{OUT_DIR}/subset_with_ranks.csv", index=False)


if __name__ == "__main__":
    main()
