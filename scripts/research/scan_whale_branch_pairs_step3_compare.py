#!/usr/bin/env python3
"""H-BRANCH-PAIR-CONSENSUS step3 · 比較「共識日」vs「單獨日」forward return。

輸入 step2 產出的 whale_branch_round3_pairconsensus_joint_events_fwdret.csv / whale_branch_round3_pairconsensus_solo_events_fwdret.csv，
對每個候選配對算 median/mean/win_rate（H7/H14/H20），joint vs solo 兩兩比較，
並附上 Welch t-test（joint vs solo）與樣本數/涉及股票數，供人工判讀。

純本地跑（不用 SSH mini）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"
HORIZONS = (7, 14, 20)


def summarize(g: pd.DataFrame, h: int) -> dict:
    col = f"r_adj_h{h}_pct"
    vals = g[col].dropna().to_numpy()
    n = len(vals)
    if n == 0:
        return {f"n_h{h}": 0, f"median_h{h}": None, f"mean_h{h}": None, f"win_rate_h{h}": None}
    return {
        f"n_h{h}": n,
        f"median_h{h}": float(np.median(vals)),
        f"mean_h{h}": float(np.mean(vals)),
        f"win_rate_h{h}": float((vals > 0).mean() * 100),
    }


def main() -> int:
    cand = pd.read_csv(OUT_DIR / "whale_branch_round3_pairconsensus_candidates.csv", dtype={"trader_a": str, "trader_b": str})
    joint = pd.read_csv(OUT_DIR / "whale_branch_round3_pairconsensus_joint_events_fwdret.csv", dtype={"trader_a": str, "trader_b": str, "stock_id": str})
    solo = pd.read_csv(OUT_DIR / "whale_branch_round3_pairconsensus_solo_events_fwdret.csv", dtype={"trader_a": str, "trader_b": str, "stock_id": str})

    rows = []
    for c in cand.itertuples(index=False):
        pid = c.pair_id
        jg = joint[joint["pair_id"] == pid]
        sg = solo[solo["pair_id"] == pid]

        row: dict = {
            "pair_id": pid,
            "trader_a": c.trader_a,
            "trader_b": c.trader_b,
            "selection": c.selection,
            "n_stocks_joint": c.n_stocks_joint,
            "stocks_joint": c.stocks_joint,
        }
        for h in HORIZONS:
            js = summarize(jg, h)
            ss = summarize(sg, h)
            row.update({f"joint_{k}": v for k, v in js.items()})
            row.update({f"solo_{k}": v for k, v in ss.items()})
            jvals = jg[f"r_adj_h{h}_pct"].dropna().to_numpy()
            svals = sg[f"r_adj_h{h}_pct"].dropna().to_numpy()
            if len(jvals) >= 3 and len(svals) >= 3:
                t_stat, p_val = stats.ttest_ind(jvals, svals, equal_var=False)
                row[f"welch_p_h{h}"] = float(p_val)
                row[f"median_diff_h{h}"] = row[f"joint_median_h{h}"] - row[f"solo_median_h{h}"]
                row[f"mean_diff_h{h}"] = row[f"joint_mean_h{h}"] - row[f"solo_mean_h{h}"]
            else:
                row[f"welch_p_h{h}"] = None
                row[f"median_diff_h{h}"] = None
                row[f"mean_diff_h{h}"] = None
        rows.append(row)

    summary = pd.DataFrame(rows)
    summary.to_csv(OUT_DIR / "whale_branch_round3_pairconsensus_joint_vs_solo_summary.csv", index=False)

    # Print a compact human-readable view, sorted by H7 joint sample size desc for the
    # top_raw group, then boutique group.
    print("=" * 100)
    print("[H7 view] pair | selection | n_joint | n_stocks_joint | joint_median | solo_median | diff | welch_p")
    print("=" * 100)
    view = summary.copy()
    view = view.sort_values(["selection", "joint_n_h7"], ascending=[True, False])
    for r in view.itertuples(index=False):
        print(
            f"{r.trader_a:>5}-{r.trader_b:<5} {r.selection:<24} "
            f"n_joint={r.joint_n_h7:>5} n_solo={r.solo_n_h7:>6} n_stocks_joint={r.n_stocks_joint:>4}  "
            f"joint_med={r.joint_median_h7 if r.joint_median_h7 is not None else float('nan'):7.2f}  "
            f"solo_med={r.solo_median_h7 if r.solo_median_h7 is not None else float('nan'):7.2f}  "
            f"diff={r.median_diff_h7 if r.median_diff_h7 is not None else float('nan'):7.2f}  "
            f"welch_p={r.welch_p_h7 if r.welch_p_h7 is not None else float('nan'):.3f}"
        )

    print(f"\n[OK] full summary -> whale_branch_round3_pairconsensus_joint_vs_solo_summary.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
