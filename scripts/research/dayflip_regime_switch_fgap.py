"""
Explore a BINARY regime-switch family for the dayflip-futures-short fgap
threshold (as opposed to the already-rejected continuous linear
night-momentum adjustment).

For each T0 (signal_date), classify into "high vol" / "low vol" regime using
either:
  (a) |TX night-session momentum| vs a split threshold, or
  (b) VIX(T0) level vs its own median computed IN THE TRAINING WINDOW ONLY.

Then apply regime-specific fixed fgap thresholds (base_lo for low-vol,
base_hi for high-vol regime) and ask: would that trade have been TAKEN
(fgap >= applicable regime threshold) under the alternate rule, vs the
baseline flat 6% rule which already determined which trades exist in the
tradelog (all rows have fgap >= 6, since that's the live threshold).

IMPORTANT DATA CONSTRAINT: the tradelog only contains trades where the
ORIGINAL flat 6% rule already fired and the smallest_qualifying_gap pick
rule already picked a single stock per day. We do NOT have counterfactual
"what if a different stock had been picked because the threshold moved"
data - we can only re-filter the existing 74 realized single-picks by
"would this day's picked trade have survived at a higher/lower per-regime
threshold". This is a real limitation shared by all fgap-threshold-tuning
analyses done today on this tradelog; we report it honestly.

So the experiment logic per parameter setting (split_col, split_point,
base_lo, base_hi):
  - regime(T0) = 'hi' if split_col(T0) >= split_point else 'lo'
    (or for VIX: 'hi' if VIX(T0) >= train_median else 'lo')
  - effective_threshold(T0) = base_hi if regime=='hi' else base_lo
  - keep trade iff fgap(T0) >= effective_threshold(T0)
  - among kept trades, compute n, win_rate, mean pnl_pct, std, sharpe_like

We sweep split points and (base_lo, base_hi) pairs on TRAIN ONLY, require
n>=20, pick the best sharpe_like, then evaluate that single setting ONCE on
TEST and on the FULL 74-row sample.
"""
import sqlite3
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, "src")
import stock_db  # noqa: E402

TRADELOG = "reports/research/branch-footprint-screen/dayflip_gapup_short/single_pick_tradelog.csv"
BARS_DB = "/Users/jackm4/goldenstocks-data/cache/tmf_channel/bars.sqlite"


def load_tradelog():
    df = pd.read_csv(TRADELOG, parse_dates=["signal_date", "trade_date"])
    df = df.sort_values("signal_date").reset_index(drop=True)
    return df


def load_night_momentum(days):
    """night_mom(T0) = last night-session close for day=T0 / last day-session
    close for day=T0 - 1, in %. Causally available before T0+1 08:45 open."""
    conn = sqlite3.connect(BARS_DB)
    days_str = sorted(set(d.strftime("%Y-%m-%d") for d in days))
    placeholders = ",".join("?" for _ in days_str)
    q = f"""
        select day, sess, t, c
        from bars
        where source='tx_1m_tick_built_582d'
          and day in ({placeholders})
          and sess in ('day','night')
    """
    rows = conn.execute(q, days_str).fetchall()
    conn.close()
    df = pd.DataFrame(rows, columns=["day", "sess", "t", "c"])
    out = {}
    for day, g in df.groupby("day"):
        day_g = g[g.sess == "day"]
        night_g = g[g.sess == "night"]
        if day_g.empty or night_g.empty:
            continue
        day_close = day_g.sort_values("t").iloc[-1]["c"]
        night_close = night_g.sort_values("t").iloc[-1]["c"]
        out[day] = (night_close / day_close - 1.0) * 100.0
    return out


def load_vix(days):
    conn = sqlite3.connect(stock_db.DEFAULT_DB_PATH)
    q = "select date, close from market_vix_daily where symbol='VIX' and source='yahoo' order by date"
    df = pd.read_sql_query(q, conn)
    conn.close()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    out = {}
    for d in days:
        sub = df[df["date"] <= d]
        if sub.empty:
            continue
        out[d] = sub.iloc[-1]["close"]
    return out


def sharpe_like(x):
    x = np.asarray(x, dtype=float)
    if len(x) < 2 or x.std(ddof=1) == 0:
        return np.nan
    return x.mean() / x.std(ddof=1)


def evaluate(df, split_col, split_point, base_lo, base_hi, direction="ge"):
    """direction='ge' means regime 'hi' if split_col >= split_point"""
    if direction == "ge":
        hi_mask = df[split_col] >= split_point
    else:
        hi_mask = df[split_col].abs() >= split_point
    eff_thresh = np.where(hi_mask, base_hi, base_lo)
    keep = df["fgap"].values >= eff_thresh
    sub = df.loc[keep]
    n = len(sub)
    if n == 0:
        return dict(n=0, win_rate=np.nan, mean=np.nan, std=np.nan, sharpe=np.nan)
    return dict(
        n=n,
        win_rate=(sub["pnl_pct"] > 0).mean(),
        mean=sub["pnl_pct"].mean(),
        std=sub["pnl_pct"].std(ddof=1) if n > 1 else np.nan,
        sharpe=sharpe_like(sub["pnl_pct"]),
    )


def main():
    df = load_tradelog()
    n_total = len(df)
    n_train = int(round(n_total * 0.7))
    train = df.iloc[:n_train].copy()
    test = df.iloc[n_train:].copy()
    print(f"Total rows: {n_total}, train: {len(train)} ({train['signal_date'].min().date()}..{train['signal_date'].max().date()}), "
          f"test: {len(test)} ({test['signal_date'].min().date()}..{test['signal_date'].max().date()})")

    # ---- baseline (flat 6%) on train/test/full ----
    def flat_eval(d, thresh=6.0):
        sub = d.loc[d["fgap"] >= thresh]
        n = len(sub)
        return dict(
            n=n,
            win_rate=(sub["pnl_pct"] > 0).mean() if n else np.nan,
            mean=sub["pnl_pct"].mean() if n else np.nan,
            std=sub["pnl_pct"].std(ddof=1) if n > 1 else np.nan,
            sharpe=sharpe_like(sub["pnl_pct"]) if n else np.nan,
        )

    print("\n=== BASELINE flat 6% (all rows already fgap>=6 in tradelog by construction) ===")
    for name, d in [("train", train), ("test", test), ("full", df)]:
        r = flat_eval(d)
        print(f"  {name}: n={r['n']} win_rate={r['win_rate']:.3f} mean={r['mean']:.3f} std={r['std']:.3f} sharpe={r['sharpe']:.3f}")

    # ---- attach features ----
    night_mom_map = load_night_momentum(df["signal_date"])
    df["night_mom"] = df["signal_date"].dt.strftime("%Y-%m-%d").map(night_mom_map)
    n_missing_night = df["night_mom"].isna().sum()
    print(f"\nnight_mom missing for {n_missing_night}/{n_total} rows (dropped from that sweep)")

    vix_map = load_vix(df["signal_date"])
    df["vix"] = df["signal_date"].map(vix_map)
    n_missing_vix = df["vix"].isna().sum()
    print(f"vix missing for {n_missing_vix}/{n_total} rows (dropped from that sweep)")

    train = df.iloc[:n_train].copy()
    test = df.iloc[n_train:].copy()

    results_log = []

    # =========================================================
    # FAMILY A: |night_mom| regime switch
    # =========================================================
    print("\n" + "=" * 70)
    print("FAMILY A: regime = |TX night momentum| >= split_point")
    print("=" * 70)
    df_a = df.dropna(subset=["night_mom"])
    train_a = df_a.iloc[: int(round(len(df_a) * 0.0))]  # placeholder, recompute below properly
    # Recompute train/test split on the night_mom-available subset using the
    # SAME chronological 70/30 split point (by date) as the full sample, not
    # re-splitting the filtered subset (to stay consistent with the overall
    # train/test date boundary).
    train_cutoff_date = train["signal_date"].max()
    train_a = df_a[df_a["signal_date"] <= train_cutoff_date]
    test_a = df_a[df_a["signal_date"] > train_cutoff_date]
    print(f"  (night_mom subset) train n={len(train_a)}, test n={len(test_a)}")

    split_points = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0]
    base_pairs = []
    for base_lo in [4, 5, 6, 7]:
        for base_hi in [4, 5, 6, 7, 8]:
            if base_lo == base_hi:
                continue
            base_pairs.append((base_lo, base_hi))

    all_settings_a = []
    for sp in split_points:
        for base_lo, base_hi in base_pairs:
            r = evaluate(train_a, "night_mom", sp, base_lo, base_hi, direction="abs")
            all_settings_a.append((sp, base_lo, base_hi, r))

    eligible_a = [s for s in all_settings_a if s[3]["n"] >= 20]
    print(f"  swept {len(all_settings_a)} settings, {len(eligible_a)} eligible (n>=20 on train)")
    if eligible_a:
        best_a = max(eligible_a, key=lambda s: s[3]["sharpe"] if not np.isnan(s[3]["sharpe"]) else -999)
        sp, blo, bhi, rtr = best_a
        print(f"  BEST train setting: split=|night_mom|>={sp}, base_lo={blo} (low-vol), base_hi={bhi} (high-vol)")
        print(f"    train: n={rtr['n']} win_rate={rtr['win_rate']:.3f} mean={rtr['mean']:.3f} std={rtr['std']:.3f} sharpe={rtr['sharpe']:.3f}")
        rte = evaluate(test_a, "night_mom", sp, blo, bhi, direction="abs")
        print(f"    test:  n={rte['n']} win_rate={rte.get('win_rate', float('nan')):.3f} mean={rte.get('mean', float('nan')):.3f} std={rte.get('std', float('nan')):.3f} sharpe={rte.get('sharpe', float('nan')):.3f}")
        rfull = evaluate(df_a, "night_mom", sp, blo, bhi, direction="abs")
        print(f"    full:  n={rfull['n']} win_rate={rfull['win_rate']:.3f} mean={rfull['mean']:.3f} std={rfull['std']:.3f} sharpe={rfull['sharpe']:.3f}")
        results_log.append(("FamilyA_night_abs", sp, blo, bhi, rtr, rte, rfull))
    else:
        print("  NO eligible settings with n>=20 on train.")

    # print full sweep table (top 15 by train sharpe) for transparency
    print("\n  --- top 15 train settings by sharpe (for transparency, incl. n<20) ---")
    all_settings_a_sorted = sorted(
        all_settings_a, key=lambda s: (s[3]["sharpe"] if not np.isnan(s[3]["sharpe"]) else -999), reverse=True
    )
    for sp, blo, bhi, r in all_settings_a_sorted[:15]:
        print(f"    split={sp:.1f} lo={blo} hi={bhi}: n={r['n']:>3} win={r['win_rate'] if r['n'] else float('nan'):.3f} "
              f"mean={r['mean'] if r['n'] else float('nan'):.3f} sharpe={r['sharpe'] if r['n'] else float('nan'):.3f}")

    # =========================================================
    # FAMILY B: VIX level regime switch (median computed on TRAIN only)
    # =========================================================
    print("\n" + "=" * 70)
    print("FAMILY B: regime = VIX(T0) >= median(VIX on TRAIN window) [median from train only]")
    print("=" * 70)
    df_b = df.dropna(subset=["vix"])
    train_b = df_b[df_b["signal_date"] <= train_cutoff_date]
    test_b = df_b[df_b["signal_date"] > train_cutoff_date]
    train_vix_median = train_b["vix"].median()
    print(f"  train VIX median = {train_vix_median:.2f} (n train={len(train_b)}, n test={len(test_b)})")

    # also sweep a few offsets around the median, and a few fixed VIX levels,
    # to not rely on a single arbitrary split
    vix_splits = {
        f"train_median({train_vix_median:.1f})": train_vix_median,
        f"train_median-2({train_vix_median-2:.1f})": train_vix_median - 2,
        f"train_median+2({train_vix_median+2:.1f})": train_vix_median + 2,
        "fixed_15": 15.0,
        "fixed_18": 18.0,
        "fixed_20": 20.0,
    }

    all_settings_b = []
    for split_name, sp in vix_splits.items():
        for base_lo, base_hi in base_pairs:
            r = evaluate(train_b, "vix", sp, base_lo, base_hi, direction="ge")
            all_settings_b.append((split_name, sp, base_lo, base_hi, r))

    eligible_b = [s for s in all_settings_b if s[4]["n"] >= 20]
    print(f"  swept {len(all_settings_b)} settings, {len(eligible_b)} eligible (n>=20 on train)")
    if eligible_b:
        best_b = max(eligible_b, key=lambda s: s[4]["sharpe"] if not np.isnan(s[4]["sharpe"]) else -999)
        sname, sp, blo, bhi, rtr = best_b
        print(f"  BEST train setting: VIX split={sname} (value={sp:.2f}), base_lo={blo} (low-VIX), base_hi={bhi} (high-VIX)")
        print(f"    train: n={rtr['n']} win_rate={rtr['win_rate']:.3f} mean={rtr['mean']:.3f} std={rtr['std']:.3f} sharpe={rtr['sharpe']:.3f}")
        rte = evaluate(test_b, "vix", sp, blo, bhi, direction="ge")
        print(f"    test:  n={rte['n']} win_rate={rte.get('win_rate', float('nan')):.3f} mean={rte.get('mean', float('nan')):.3f} std={rte.get('std', float('nan')):.3f} sharpe={rte.get('sharpe', float('nan')):.3f}")
        rfull = evaluate(df_b, "vix", sp, blo, bhi, direction="ge")
        print(f"    full:  n={rfull['n']} win_rate={rfull['win_rate']:.3f} mean={rfull['mean']:.3f} std={rfull['std']:.3f} sharpe={rfull['sharpe']:.3f}")
        results_log.append(("FamilyB_vix", sname, blo, bhi, rtr, rte, rfull))
    else:
        print("  NO eligible settings with n>=20 on train.")

    print("\n  --- top 15 train settings by sharpe (for transparency, incl. n<20) ---")
    all_settings_b_sorted = sorted(
        all_settings_b, key=lambda s: (s[4]["sharpe"] if not np.isnan(s[4]["sharpe"]) else -999), reverse=True
    )
    for sname, sp, blo, bhi, r in all_settings_b_sorted[:15]:
        print(f"    vix_split={sname} lo={blo} hi={bhi}: n={r['n']:>3} win={r['win_rate'] if r['n'] else float('nan'):.3f} "
              f"mean={r['mean'] if r['n'] else float('nan'):.3f} sharpe={r['sharpe'] if r['n'] else float('nan'):.3f}")

    # =========================================================
    # Summary
    # =========================================================
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    baseline_full = flat_eval(df)
    print(f"Baseline flat 6% FULL sample: n={baseline_full['n']} sharpe={baseline_full['sharpe']:.4f} mean={baseline_full['mean']:.3f}")
    for tag, *rest in results_log:
        r_full = rest[-1]
        print(f"{tag}: FULL n={r_full['n']} sharpe={r_full['sharpe']:.4f} mean={r_full['mean']:.3f} "
              f"(beats baseline: {r_full['sharpe'] > baseline_full['sharpe'] if not np.isnan(r_full['sharpe']) else 'N/A'})")


if __name__ == "__main__":
    main()
