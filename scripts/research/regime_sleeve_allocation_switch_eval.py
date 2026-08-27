"""Item AI of the creative-combination research plan (2026-08-08):

Does the chip-macro regime (same PIT-correct definition as item A:
fut_foreign_oi z60>0 AND ix_close>MA200 = "regime_bull") act as a
DIFFERENTIATED ALLOCATION SWITCH between two already-researched sleeves of
opposite STYLE, rather than as a single-signal on/off gate (item A tested the
gate framing on 9217 branch-follow and found REJECT / outlier-driven)?

Sleeves compared:
  1. leading-dip (adopted Strategy, momentum/trend-adjacent: buys an
     intraday dip expecting continuation/recovery). Trade series =
     reports/research/rrg/20260715_leading_dip_events.csv, return metric =
     `ex3` (T+3 excess return vs same-source benchmark; the metric used
     throughout the adoption report reports/research/rrg/
     20260715_leading_wma20_excess6_wma3_confirm.md, hit = ex3>0).
     Signal date = `date` column (post-close trigger day).

  2. dayflip-futures-short (frozen spec, G2 OOS pending -- NOT yet adopted
     into config/strategy.yaml; treated here as a research sleeve, not a
     live strategy, despite the task brief's "adopted" framing -- status is
     reported accurately below). Explicitly contrarian/fade: shorts a stock
     future on a >=6% gap-up, betting the gap fades. Trade series =
     reports/research/dayflip_revenue_momentum_filter/trades_with_revyoy.csv,
     return metric = `pnl_pct` (already the frozen spec's tick-replayed
     per-leg P&L%). Signal date = `signal_date` column (T0, the whale
     accumulation day the short trade is conditioned on).

Regime tagging: identical recipe + identical panel to
scripts/research/regime_gate_branch_fusion_eval.py --
  chip_z60   = zscore(fut_foreign_oi, window=60) > 0
  bull200    = ix_close > ix_close.rolling(200).mean()
  regime_bull = chip_z60 & bull200
computed causally (trailing windows only) on data/research/chip_macro/panel.csv,
looked up by each trade's signal date.

Hypothesis under test: leading-dip (trend-following) should do relatively
BETTER in regime_bull days, and dayflip-short (fade) should do relatively
BETTER in regime_bear days -- i.e. the two sleeves' (bull-mean minus
bear-mean) deltas should have OPPOSITE SIGN (complementary allocation
switch), not the same sign (regime just moving both the same way, no
differentiated-allocation evidence).

Primary test = INTERACTION / difference-in-differences:
    DiD = (leading_dip_bull_mean - leading_dip_bear_mean)
          - (dayflip_short_bull_mean - dayflip_short_bear_mean)
A large POSITIVE DiD with the individual deltas having opposite signs
supports the complementary-switch hypothesis. Permutation test: shuffle the
regime_bull label across the full population of dates where chip_z60/ma200
are both defined (population-level shuffle, NOT trade-level -- same
honesty argument as item A: trade composition per sleeve is held fixed,
only the regime-label lookup is randomized), independently re-tag BOTH
sleeves' trades under each shuffle, recompute DiD, build a null distribution.

Read-only: no sqlite3 connection opened (both trade CSVs and the chip panel
are already materialized on disk). Does not touch config/order.yaml,
config/strategy.yaml, src/order/, or launchd.

Outputs (under reports/research/regime_sleeve_allocation_switch/):
  - leading_dip_regime_tagged.csv
  - dayflip_short_regime_tagged.csv
  - result.json
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
PANEL_CSV = ROOT / "data" / "research" / "chip_macro" / "panel.csv"
LD_CSV = ROOT / "reports" / "research" / "rrg" / "20260715_leading_dip_events.csv"
DFS_CSV = ROOT / "reports" / "research" / "dayflip_revenue_momentum_filter" / "trades_with_revyoy.csv"
OUT_DIR = ROOT / "reports" / "research" / "regime_sleeve_allocation_switch"
OUT_LD = OUT_DIR / "leading_dip_regime_tagged.csv"
OUT_DFS = OUT_DIR / "dayflip_short_regime_tagged.csv"
OUT_JSON = OUT_DIR / "result.json"

N_PERM = 10000
SEED = 20260808


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


def tag(trades: pd.DataFrame, date_col: str, panel: pd.DataFrame) -> pd.DataFrame:
    merged = trades.merge(
        panel[["date", "chip_z60", "chip_gt0", "bull200", "regime_bull", "regime_defined"]],
        left_on=date_col,
        right_on="date",
        how="left",
        suffixes=("", "_panel"),
    )
    missing = merged["regime_bull"].isna().sum()
    assert missing == 0, f"{missing} trade dates failed to resolve a panel regime label"
    assert merged["regime_defined"].all(), "chip/ma200 must be defined for every trade date"
    if "date_panel" in merged.columns:
        merged = merged.drop(columns=["date_panel"])
    elif date_col != "date":
        merged = merged.drop(columns=["date"])
    return merged


def bucket_summary(merged: pd.DataFrame, ret_col: str) -> dict:
    g = merged.groupby("regime_bull")[ret_col].agg(
        n="count", mean="mean", median="median", win_rate=lambda s: float((s > 0).mean())
    )
    out = {}
    for bull_flag, row in g.iterrows():
        key = "bull" if bull_flag else "bear"
        out[key] = {k: (float(v) if k != "n" else int(v)) for k, v in row.items()}
    return out


def did_permutation_test(
    dates_pop: np.ndarray,
    labels_pop: np.ndarray,
    ld_dates: np.ndarray,
    ld_returns: np.ndarray,
    dfs_dates: np.ndarray,
    dfs_returns: np.ndarray,
    n_perm: int,
    seed: int,
) -> dict:
    rng = np.random.default_rng(seed)
    date_to_idx = {d: i for i, d in enumerate(dates_pop)}
    ld_idx = np.array([date_to_idx[d] for d in ld_dates])
    dfs_idx = np.array([date_to_idx[d] for d in dfs_dates])

    def diff_for(labels: np.ndarray, idx: np.ndarray, returns: np.ndarray) -> float | None:
        lab = labels[idx]
        if lab.all() or (~lab).all():
            return None
        return float(returns[lab].mean() - returns[~lab].mean())

    real_labels = labels_pop
    obs_ld_diff = diff_for(real_labels, ld_idx, ld_returns)
    obs_dfs_diff = diff_for(real_labels, dfs_idx, dfs_returns)
    obs_did = obs_ld_diff - obs_dfs_diff

    perm_did = np.full(n_perm, np.nan)
    perm_ld = np.full(n_perm, np.nan)
    perm_dfs = np.full(n_perm, np.nan)
    for i in range(n_perm):
        shuffled = rng.permutation(labels_pop)
        d_ld = diff_for(shuffled, ld_idx, ld_returns)
        d_dfs = diff_for(shuffled, dfs_idx, dfs_returns)
        if d_ld is None or d_dfs is None:
            continue
        perm_ld[i] = d_ld
        perm_dfs[i] = d_dfs
        perm_did[i] = d_ld - d_dfs

    valid = ~np.isnan(perm_did)
    null_did = perm_did[valid]
    p_two_sided = float(np.mean(np.abs(null_did) >= abs(obs_did))) if len(null_did) else float("nan")
    p_one_sided_pos = float(np.mean(null_did >= obs_did)) if len(null_did) else float("nan")

    return {
        "leading_dip": {
            "n_bull": int(real_labels[ld_idx].sum()),
            "n_bear": int((~real_labels[ld_idx]).sum()),
            "bull_minus_bear_mean_ex3pct": obs_ld_diff,
        },
        "dayflip_short": {
            "n_bull": int(real_labels[dfs_idx].sum()),
            "n_bear": int((~real_labels[dfs_idx]).sum()),
            "bull_minus_bear_mean_pnlpct": obs_dfs_diff,
        },
        "observed_did": obs_did,
        "complementary_signs": bool(
            (obs_ld_diff > 0 and obs_dfs_diff < 0) or (obs_ld_diff < 0 and obs_dfs_diff > 0)
        ),
        "n_perm": int(n_perm),
        "n_valid_perms": int(len(null_did)),
        "null_did_mean": float(np.mean(null_did)) if len(null_did) else None,
        "null_did_std": float(np.std(null_did)) if len(null_did) else None,
        "p_value_two_sided": p_two_sided,
        "p_value_one_sided_did_gt_0": p_one_sided_pos,
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    panel = build_regime_series()

    ld = pd.read_csv(LD_CSV, dtype={"sid": str})
    dfs = pd.read_csv(DFS_CSV, dtype={"stock": str})

    ld_tagged = tag(ld, "date", panel)
    dfs_tagged = tag(dfs, "signal_date", panel)

    ld_tagged.to_csv(OUT_LD, index=False)
    dfs_tagged.to_csv(OUT_DFS, index=False)
    print(f"[OK] wrote {OUT_LD} (n={len(ld_tagged)})")
    print(f"[OK] wrote {OUT_DFS} (n={len(dfs_tagged)})")

    ld_summary = bucket_summary(ld_tagged, "ex3")
    dfs_summary = bucket_summary(dfs_tagged, "pnl_pct")
    print("\n[leading-dip ex3 by regime]")
    print(json.dumps(ld_summary, indent=2))
    print("\n[dayflip-short pnl_pct by regime]")
    print(json.dumps(dfs_summary, indent=2))

    dates_pop = panel.loc[panel["regime_defined"], "date"].to_numpy()
    labels_pop = panel.loc[panel["regime_defined"], "regime_bull"].to_numpy()

    did_result = did_permutation_test(
        dates_pop=dates_pop,
        labels_pop=labels_pop,
        ld_dates=ld_tagged["date"].to_numpy() if "date" in ld_tagged.columns else ld_tagged["date_orig"].to_numpy(),
        ld_returns=ld_tagged["ex3"].to_numpy(),
        dfs_dates=dfs_tagged["signal_date"].to_numpy(),
        dfs_returns=dfs_tagged["pnl_pct"].to_numpy(),
        n_perm=N_PERM,
        seed=SEED,
    )

    result = {
        "leading_dip_bucket_summary_ex3pct": ld_summary,
        "dayflip_short_bucket_summary_pnlpct": dfs_summary,
        "interaction_diff_in_diff": did_result,
        "population_base_rate_bull": float(labels_pop.mean()),
        "n_pop_dates": int(len(dates_pop)),
        "method": (
            "date-label permutation: shuffle regime_bull across the full population of "
            "dates where chip_z60/ma200 are both defined, re-tag BOTH sleeves' real trade "
            "dates under each shuffle, compute DiD = (leading_dip bull-bear) - "
            "(dayflip_short bull-bear) each iteration. Trade composition held fixed; only "
            "the regime-label lookup is randomized."
        ),
        "notes": (
            "dayflip-futures-short is status=frozen/phase=hypothesis in config/research.yaml "
            "(G2 OOS holdout pending) -- NOT yet adopted into config/strategy.yaml, contrary "
            "to the loose 'adopted' framing in the task brief. Treated here as a research "
            "sleeve for the style-contrast test, not a live-order sleeve."
        ),
    }

    print("\n" + "=" * 88)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print("=" * 88)

    OUT_JSON.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[OK] wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
