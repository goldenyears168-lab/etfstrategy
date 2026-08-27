"""dayflip-futures-short: nonlinear night-momentum threshold sweep.

Family under test (per task spec): NONLINEAR transforms of night-session
momentum (already-rejected family was strictly linear threshold = base + k*night_return).

Transforms tested:
  A) squared magnitude: threshold = base + k * night_return^2  (direction-agnostic)
  B) abs / sqrt-dampened magnitude: threshold = base + k * sqrt(|night_return|)
  C) log1p-dampened magnitude: threshold = base + k * log1p(|night_return|)
  D) expanding-window percentile rank of |night_return| (causal): threshold
     = base + k * rank_pctile, rank computed only using days strictly before
     the current signal_date (no lookahead)

Methodology (mandated):
  1. Sort 74 rows by signal_date. First 70% = train, last 30% = test (chronological).
  2. On TRAIN ONLY sweep (transform, base, k). For each combo, compute
     n, win_rate, mean pnl_pct, std, sharpe_like=mean/std among trades where
     fgap >= effective_threshold(day). Require n>=20 to be eligible as "best".
  3. Take train-selected best combo per transform, evaluate ONCE on TEST unchanged.
  4. Report full-74-sample (day-clustered / honest) number too.
  5. List every combo tried (no cherry picking silently).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
import math
import statistics as st

import pandas as pd
import stock_db

REPO = Path("/Users/jackm4/goldenstocks")
TRADELOG = REPO / "reports/research/branch-footprint-screen/dayflip_gapup_short/single_pick_tradelog.csv"
BARS_DB = Path.home() / "goldenstocks-data/cache/tmf_channel/bars.sqlite"

BASE_FLAT = 0.06  # 6% flat baseline, fgap column is already in percent units (e.g. 9.8 = 9.8%)


def load_tradelog() -> pd.DataFrame:
    df = pd.read_csv(TRADELOG, dtype={"stock": str})
    df["signal_date"] = pd.to_datetime(df["signal_date"])
    df = df.sort_values("signal_date").reset_index(drop=True)
    return df


def load_night_momentum() -> pd.DataFrame:
    """night_return(T0) in % = last night-session close for day=T0 / last day-session close for day=T0 - 1."""
    conn = sqlite3.connect(str(BARS_DB))
    q = """
        select day, sess, t, c
        from bars
        where source = 'tx_1m_tick_built_582d' and sess in ('day','night')
        order by day, sess, t
    """
    raw = pd.read_sql_query(q, conn)
    conn.close()
    # last close per (day, sess)
    last = raw.groupby(["day", "sess"], as_index=False).last()
    piv = last.pivot(index="day", columns="sess", values="c")
    piv.columns.name = None
    piv = piv.rename(columns={"day": "day_close", "night": "night_close"})
    piv["night_return"] = (piv["night_close"] / piv["day_close"] - 1.0) * 100.0
    piv.index.name = "trading_day"
    piv = piv.reset_index().rename(columns={"trading_day": "day"})
    piv["day"] = pd.to_datetime(piv["day"])
    return piv[["day", "night_return"]]


def attach_night_momentum(trades: pd.DataFrame, night: pd.DataFrame) -> pd.DataFrame:
    merged = trades.merge(night, left_on="signal_date", right_on="day", how="left")
    return merged


def sharpe_like(pnls: list[float]) -> float:
    if len(pnls) < 2:
        return float("nan")
    sd = st.pstdev(pnls)
    if sd == 0:
        return float("nan")
    return st.mean(pnls) / sd


def summarize(df: pd.DataFrame, mask: pd.Series) -> dict:
    sub = df[mask]
    n = len(sub)
    if n == 0:
        return dict(n=0, win_rate=float("nan"), mean=float("nan"), std=float("nan"), sharpe=float("nan"))
    pnls = sub["pnl_pct"].tolist()
    win_rate = (sub["pnl_pct"] > 0).mean()
    mean_p = st.mean(pnls)
    std_p = st.pstdev(pnls) if n >= 2 else float("nan")
    sh = sharpe_like(pnls)
    return dict(n=n, win_rate=win_rate, mean=mean_p, std=std_p, sharpe=sh)


def expanding_percentile_rank(series_by_date: pd.Series) -> pd.Series:
    """Causal expanding-window percentile rank of |night_return|, using only
    strictly-prior days (no lookahead). First occurrence -> NaN (no history)."""
    vals = series_by_date.values
    out = [float("nan")] * len(vals)
    history: list[float] = []
    for i, v in enumerate(vals):
        if len(history) >= 1:
            # percentile rank of v within history (strictly prior days)
            less_eq = sum(1 for h in history if h <= v)
            out[i] = less_eq / len(history)
        history.append(v)
    return pd.Series(out, index=series_by_date.index)


def main():
    trades = load_tradelog()
    night = load_night_momentum()
    df = attach_night_momentum(trades, night)

    n_missing = df["night_return"].isna().sum()
    print(f"rows total={len(df)}, night_return missing={n_missing}")

    df["abs_night"] = df["night_return"].abs()
    df["sq_night"] = df["night_return"] ** 2
    df["sqrt_abs_night"] = df["abs_night"] ** 0.5
    df["log1p_abs_night"] = df["abs_night"].apply(lambda x: math.log1p(x) if pd.notna(x) else float("nan"))
    # expanding percentile rank, computed on the FULL chronological sequence (causal by construction:
    # only prior days' abs_night used), so it's fine to compute once on the whole sorted df.
    df["pctile_abs_night"] = expanding_percentile_rank(df["abs_night"])

    n_split = int(len(df) * 0.7)
    train = df.iloc[:n_split].copy()
    test = df.iloc[n_split:].copy()
    print(f"train n={len(train)} ({train['signal_date'].min().date()}..{train['signal_date'].max().date()})")
    print(f"test  n={len(test)} ({test['signal_date'].min().date()}..{test['signal_date'].max().date()})")

    # ---- Baseline: flat 6% ----
    base_train = summarize(train, train["fgap"] >= BASE_FLAT * 100)
    base_test = summarize(test, test["fgap"] >= BASE_FLAT * 100)
    base_full = summarize(df, df["fgap"] >= BASE_FLAT * 100)
    print("\n=== BASELINE flat fgap>=6% ===")
    print("train:", base_train)
    print("test :", base_test)
    print("full :", base_full)

    transforms = {
        "sq_magnitude": ("sq_night", [0, 0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5]),
        "sqrt_magnitude": ("sqrt_abs_night", [0, 0.2, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0]),
        "log1p_magnitude": ("log1p_abs_night", [0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0]),
        "pctile_rank": ("pctile_abs_night", [0, 1, 2, 3, 4, 5, 6, 8]),
    }
    bases = [4.0, 5.0, 6.0, 7.0]

    all_results = {}
    for tname, (col, ks) in transforms.items():
        print(f"\n\n########## TRANSFORM: {tname} (col={col}) ##########")
        rows = []
        for base in bases:
            for k in ks:
                # effective threshold in %, direction-agnostic widening (>=0 assumed; feature is
                # already magnitude-based so k>=0 means "bigger overnight move -> higher bar")
                eff_thr = base + k * train[col]
                mask = train["fgap"] >= eff_thr
                stats = summarize(train, mask)
                rows.append(dict(base=base, k=k, **stats))
        res_df = pd.DataFrame(rows)
        eligible = res_df[res_df["n"] >= 20].copy()
        print(f"n combos tried: {len(res_df)}; eligible (n>=20): {len(eligible)}")
        print(res_df.to_string(index=False))
        all_results[tname] = res_df

        if eligible.empty:
            print(f"[{tname}] NO eligible combo with n>=20 on train. NEGATIVE finding: cannot select a parameter.")
            continue

        best = eligible.sort_values("sharpe", ascending=False).iloc[0]
        base_b, k_b = best["base"], best["k"]
        print(f"[{tname}] TRAIN-selected best: base={base_b}, k={k_b} -> train stats: n={best['n']:.0f} "
              f"win_rate={best['win_rate']:.3f} mean={best['mean']:.3f} std={best['std']:.3f} sharpe={best['sharpe']:.3f}")

        # Evaluate ONCE on test, unchanged params
        eff_thr_test = base_b + k_b * test[col]
        test_mask = test["fgap"] >= eff_thr_test
        test_stats = summarize(test, test_mask)
        print(f"[{tname}] TEST (out-of-sample, params frozen): {test_stats}")

        # Full-sample honest check (74 rows, day-clustered = full sample since 1 trade/day)
        eff_thr_full = base_b + k_b * df[col]
        full_mask = df["fgap"] >= eff_thr_full
        full_stats = summarize(df, full_mask)
        print(f"[{tname}] FULL-74 (honest day-clustered check): {full_stats}")

        # verdict
        beats_train = best["sharpe"] > base_train["sharpe"] if pd.notna(base_train["sharpe"]) else False
        beats_test = (test_stats["sharpe"] > base_test["sharpe"]) if pd.notna(test_stats["sharpe"]) and pd.notna(base_test["sharpe"]) else False
        same_sign = (test_stats["sharpe"] > 0) == (best["sharpe"] > 0) if pd.notna(test_stats["sharpe"]) else False
        print(f"[{tname}] VERDICT: beats_baseline_train={beats_train}, beats_baseline_test={beats_test}, "
              f"train/test same-sign consistency={same_sign}")

    print("\n\nDONE.")


if __name__ == "__main__":
    main()
