"""
9217分點決斷買訊(5dnet95, n=36)的星期幾季節性檢定。

背景：本session item Y查過dayflip-short(同日跳空放空)的星期幾效應=弱/nominal
(spearman rho=-0.107, p=0.141, n=190, unadjusted)。這裡查的是完全不同機制的訊號——
9217分點买超(buy_5d>=5000萬 & net_ratio>=0.95)觸發後做多持有7交易日(L1H7)。
n=36，五等分後每桶約7筆，power極弱，僅供exploratory參考。

輸出：
  reports/research/branch_9217_day_of_week/dow_summary.csv        每星期幾 n/mean/median/win_rate
  reports/research/branch_9217_day_of_week/dow_seasonality.json   Kruskal-Wallis + Mon-vs-rest + Fri-vs-rest permutation
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

SRC = Path("reports/research/branch-footprint-screen/whale_9217_5dnet95_trades.csv")
OUTDIR = Path("reports/research/branch_9217_day_of_week")
OUTDIR.mkdir(parents=True, exist_ok=True)

RNG_SEED = 20260808
N_PERM = 20000

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri"]


def permutation_two_group_test(a: np.ndarray, b: np.ndarray, n_perm: int, seed: int) -> float:
    """二元分組 mean-diff permutation test, two-sided p-value."""
    rng = np.random.default_rng(seed)
    pooled = np.concatenate([a, b])
    n_a = len(a)
    obs_diff = a.mean() - b.mean()
    diffs = np.empty(n_perm)
    for i in range(n_perm):
        perm = rng.permutation(pooled)
        diffs[i] = perm[:n_a].mean() - perm[n_a:].mean()
    p = (np.sum(np.abs(diffs) >= abs(obs_diff)) + 1) / (n_perm + 1)
    return float(p), float(obs_diff)


def main() -> None:
    df = pd.read_csv(SRC, parse_dates=["signal_date"])
    assert len(df) == 36, f"expected n=36 decisive trades, got {len(df)}"

    df["weekday_idx"] = df["signal_date"].dt.weekday  # Mon=0..Sun=6
    df["weekday"] = df["weekday_idx"].map(dict(enumerate(WEEKDAY_NAMES + ["Sat", "Sun"])))
    ret_col = "r_adj_pct"  # beta+cost adjusted L1H7 excess return (protocol SSOT)
    assert df["weekday_idx"].max() <= 4, "unexpected weekend signal_date — check calendar"

    rows = []
    groups = {}
    for wd_idx, wd_name in enumerate(WEEKDAY_NAMES):
        sub = df[df["weekday_idx"] == wd_idx][ret_col].to_numpy()
        groups[wd_name] = sub
        if len(sub) == 0:
            rows.append({"weekday": wd_name, "n": 0, "mean_pct": np.nan, "median_pct": np.nan, "win_rate_pct": np.nan})
            continue
        rows.append(
            {
                "weekday": wd_name,
                "n": len(sub),
                "mean_pct": round(float(np.mean(sub)), 3),
                "median_pct": round(float(np.median(sub)), 3),
                "win_rate_pct": round(float(np.mean(sub > 0) * 100), 1),
            }
        )
    dow_summary = pd.DataFrame(rows)
    dow_summary.to_csv(OUTDIR / "dow_summary.csv", index=False)

    # Kruskal-Wallis across the (up to 5) non-empty weekday groups
    nonempty_groups = [g for g in groups.values() if len(g) > 0]
    kw_stat, kw_p = stats.kruskal(*nonempty_groups)

    # one-way ANOVA (parametric companion, same caveats)
    anova_stat, anova_p = stats.f_oneway(*nonempty_groups)

    # Monday-vs-rest, Friday-vs-rest — matches item Y's binary-split shape, permutation-based
    mon = df[df["weekday_idx"] == 0][ret_col].to_numpy()
    rest_of_mon = df[df["weekday_idx"] != 0][ret_col].to_numpy()
    fri = df[df["weekday_idx"] == 4][ret_col].to_numpy()
    rest_of_fri = df[df["weekday_idx"] != 4][ret_col].to_numpy()

    mon_p, mon_diff = permutation_two_group_test(mon, rest_of_mon, N_PERM, RNG_SEED)
    fri_p, fri_diff = permutation_two_group_test(fri, rest_of_fri, N_PERM, RNG_SEED + 1)

    # Spearman rho vs weekday-as-ordinal, to mirror item Y's exact stat (rho, p)
    rho, rho_p = stats.spearmanr(df["weekday_idx"], df[ret_col])

    result = {
        "signal": "whale_9217_5dnet95 decisive branch-follow buy, L1H7 (r_adj_pct = beta+cost-adjusted excess return)",
        "n_total": int(len(df)),
        "power_caveat": "n=36 split 5 ways -> ~5-9 trades per weekday bucket; severely underpowered, exploratory only",
        "per_weekday": rows,
        "kruskal_wallis": {"stat": round(float(kw_stat), 3), "p": round(float(kw_p), 4), "n_groups": len(nonempty_groups)},
        "anova_oneway": {"stat": round(float(anova_stat), 3), "p": round(float(anova_p), 4)},
        "spearman_weekday_ordinal": {"rho": round(float(rho), 4), "p": round(float(rho_p), 4)},
        "monday_vs_rest": {
            "n_mon": len(mon), "n_rest": len(rest_of_mon),
            "mean_mon_pct": round(float(mon.mean()), 3) if len(mon) else None,
            "mean_rest_pct": round(float(rest_of_mon.mean()), 3) if len(rest_of_mon) else None,
            "diff_pct": round(mon_diff, 3),
            "perm_p_two_sided": round(mon_p, 4),
            "n_perm": N_PERM,
        },
        "friday_vs_rest": {
            "n_fri": len(fri), "n_rest": len(rest_of_fri),
            "mean_fri_pct": round(float(fri.mean()), 3) if len(fri) else None,
            "mean_rest_pct": round(float(rest_of_fri.mean()), 3) if len(rest_of_fri) else None,
            "diff_pct": round(fri_diff, 3),
            "perm_p_two_sided": round(fri_p, 4),
            "n_perm": N_PERM,
        },
    }

    with open(OUTDIR / "dow_seasonality.json", "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(dow_summary.to_string(index=False))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
