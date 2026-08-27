"""
dayflip-futures-short: test COMBINED VIX-change x TX-night-momentum signal
as an adaptive fgap threshold, vs the flat 6% baseline.

Methodology (per task spec):
  - 74 trades sorted by signal_date; first 70% = train, last 30% = test.
  - Sweep parameters on TRAIN ONLY; require n>=20 to be eligible as "best".
  - Evaluate train-selected best setting ONCE on TEST, unchanged.
  - Report train, test, and full-74 (day-clustered / honest) numbers.
  - No candidate-level (multi-stock-per-day) data is available -- only the
    already-selected single_pick_tradelog. So "adaptive threshold" is
    implemented as a FILTER on the already-picked trades: keep trade iff
    fgap >= adaptive_threshold(day); this mirrors how the prior (rejected)
    excess_gap_v2 test in this same repo was implemented.
"""
import sqlite3
import statistics as st
import pandas as pd
import numpy as np

REPO = "/Users/jackm4/goldenstocks"
TRADELOG = f"{REPO}/reports/research/branch-footprint-screen/dayflip_gapup_short/single_pick_tradelog.csv"
BARS_DB = "/Users/jackm4/goldenstocks-data/cache/tmf_channel/bars.sqlite"
STOCKS_DB = "/Users/jackm4/goldenstocks-data/data/stocks.db"

BASE = 6.0  # flat baseline threshold (%)


def load_tradelog():
    df = pd.read_csv(TRADELOG)
    df["signal_date"] = pd.to_datetime(df["signal_date"])
    df = df.sort_values("signal_date").reset_index(drop=True)
    return df


def night_momentum_for_day(con, day_str):
    """(last night-session close for day=D) / (last day-session close for day=D) - 1, in %."""
    cur = con.cursor()
    cur.execute(
        "select c from bars where source='tx_1m_tick_built_582d' and day=? and sess='day' order by t desc limit 1",
        (day_str,),
    )
    row_day = cur.fetchone()
    cur.execute(
        "select c from bars where source='tx_1m_tick_built_582d' and day=? and sess='night' order by t desc limit 1",
        (day_str,),
    )
    row_night = cur.fetchone()
    if row_day is None or row_night is None:
        return None
    day_close = row_day[0]
    night_close = row_night[0]
    if day_close is None or day_close == 0:
        return None
    return (night_close / day_close - 1.0) * 100.0


def vix_change_for_day(con, day_str):
    """VIX(T0) - VIX(prior available close before T0), in points. Causally valid before T0+1 open."""
    cur = con.cursor()
    cur.execute(
        "select date, close from market_vix_daily where symbol='VIX' and source='yahoo' and date<=? order by date desc limit 2",
        (day_str,),
    )
    rows = cur.fetchall()
    if len(rows) < 2:
        return None
    v_t0 = rows[0][1]
    v_prev = rows[1][1]
    if v_t0 is None or v_prev is None:
        return None
    return v_t0 - v_prev


def build_dataset():
    df = load_tradelog()
    con_bars = sqlite3.connect(BARS_DB)
    con_vix = sqlite3.connect(STOCKS_DB)

    night_mom = []
    vix_chg = []
    for _, row in df.iterrows():
        d = row["signal_date"].strftime("%Y-%m-%d")
        night_mom.append(night_momentum_for_day(con_bars, d))
        vix_chg.append(vix_change_for_day(con_vix, d))
    df["night_mom"] = night_mom
    df["vix_chg"] = vix_chg
    con_bars.close()
    con_vix.close()
    return df


def sharpe_like(pnls):
    if len(pnls) < 2:
        return float("nan")
    sd = st.pstdev(pnls) if len(pnls) > 1 else float("nan")
    if sd == 0:
        return float("nan")
    return st.mean(pnls) / sd


def summarize(sub, label=""):
    n = len(sub)
    if n == 0:
        return dict(label=label, n=0, win_rate=float("nan"), mean=float("nan"),
                     std=float("nan"), sharpe=float("nan"))
    pnls = sub["pnl_pct"].tolist()
    win_rate = sum(1 for p in pnls if p > 0) / n
    mean = st.mean(pnls)
    sd = st.pstdev(pnls) if n > 1 else float("nan")
    sh = sharpe_like(pnls)
    return dict(label=label, n=n, win_rate=win_rate, mean=mean, std=sd, sharpe=sh)


def print_row(r):
    print(f"  {r['label']:<40s} n={r['n']:>3d}  win={r['win_rate']*100 if r['n'] else float('nan'):6.1f}%  "
          f"mean={r['mean']:7.3f}  std={r['std']:7.3f}  sharpe={r['sharpe']:7.3f}")


def baseline_summary(df):
    return summarize(df, "baseline (flat 6%, all trades)")


def main():
    df = build_dataset()
    missing = df[df["night_mom"].isna() | df["vix_chg"].isna()]
    print(f"Total trades: {len(df)}; missing night_mom or vix_chg: {len(missing)}")
    if len(missing):
        print(missing[["signal_date", "stock", "night_mom", "vix_chg"]].to_string())
    df = df.dropna(subset=["night_mom", "vix_chg"]).reset_index(drop=True)
    n_total = len(df)
    n_train = int(round(n_total * 0.7))
    train = df.iloc[:n_train].reset_index(drop=True)
    test = df.iloc[n_train:].reset_index(drop=True)
    print(f"n_total={n_total}  n_train={len(train)}  n_test={len(test)}")
    print(f"train date range: {train['signal_date'].min().date()} .. {train['signal_date'].max().date()}")
    print(f"test  date range: {test['signal_date'].min().date()} .. {test['signal_date'].max().date()}")

    # Standardize vix_chg and night_mom using TRAIN stats only (avoid leakage),
    # apply the same standardization to test and full.
    vix_mu, vix_sd = train["vix_chg"].mean(), train["vix_chg"].std(ddof=0)
    nm_mu, nm_sd = train["night_mom"].mean(), train["night_mom"].std(ddof=0)
    for d in (df, train, test):
        d["vix_z"] = (d["vix_chg"] - vix_mu) / vix_sd
        d["nm_z"] = (d["night_mom"] - nm_mu) / nm_sd

    print("\n=== BASELINE (flat 6%, no filter -- all trades already qualify by construction) ===")
    b = baseline_summary(df)
    print_row(b)
    b_train = baseline_summary(train)
    b_test = baseline_summary(test)
    print_row({**b_train, "label": "baseline (train subset)"})
    print_row({**b_test, "label": "baseline (test subset)"})

    print("\n############################################################")
    print("# FAMILY A: SUM COMBO")
    print("#   adaptive_threshold = BASE + k1*vix_z + k2*nm_z")
    print("#   keep trade iff fgap >= adaptive_threshold")
    print("############################################################")
    k_grid = [-1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5]
    base_grid = [5.0, 6.0, 7.0]
    sum_results = []
    for base in base_grid:
        for k1 in k_grid:
            for k2 in k_grid:
                thr = base + k1 * train["vix_z"] + k2 * train["nm_z"]
                sub = train[train["fgap"] >= thr]
                r = summarize(sub, f"base={base} k1(vix)={k1} k2(night)={k2}")
                r["base"], r["k1"], r["k2"] = base, k1, k2
                sum_results.append(r)

    eligible = [r for r in sum_results if r["n"] >= 20]
    print(f"\nTotal settings swept: {len(sum_results)}  (base x k1 x k2 = {len(base_grid)}x{len(k_grid)}x{len(k_grid)})")
    print(f"Settings with n>=20 on TRAIN: {len(eligible)}")
    if eligible:
        eligible_sorted = sorted(eligible, key=lambda r: r["sharpe"] if r["sharpe"] == r["sharpe"] else -999, reverse=True)
        print("\nTop 5 TRAIN settings by sharpe (n>=20 only):")
        for r in eligible_sorted[:5]:
            print_row(r)
        best = eligible_sorted[0]
        print(f"\n>>> TRAIN-SELECTED BEST (sum combo): base={best['base']} k1(vix)={best['k1']} k2(night)={best['k2']}")
        print_row(best)

        # Evaluate on TEST unchanged
        thr_test = best["base"] + best["k1"] * test["vix_z"] + best["k2"] * test["nm_z"]
        sub_test = test[test["fgap"] >= thr_test]
        test_r = summarize(sub_test, "TEST (train-selected params, sum combo)")
        print_row(test_r)

        # Full 74-sample honest check (same params)
        thr_full = best["base"] + best["k1"] * df["vix_z"] + best["k2"] * df["nm_z"]
        sub_full = df[df["fgap"] >= thr_full]
        full_r = summarize(sub_full, "FULL-74 honest check (train-selected params, sum combo)")
        print_row(full_r)

        print(f"\nComparison -- baseline test sharpe={b_test['sharpe']:.3f} (n={b_test['n']}) "
              f"vs sum-combo test sharpe={test_r['sharpe']:.3f} (n={test_r['n']})")
        print(f"Train sharpe was {best['sharpe']:.3f} (n={best['n']}); "
              f"{'CONSISTENT' if (test_r['sharpe']==test_r['sharpe'] and (test_r['sharpe']-b_test['sharpe'])*(best['sharpe']-b_train['sharpe'])>0) else 'directionally INCONSISTENT or degenerate'} "
              "sign vs baseline uplift train->test.")
    else:
        print("No sum-combo setting reached n>=20 on TRAIN -- family A yields no eligible pick.")

    print("\n############################################################")
    print("# FAMILY B: 2-of-2 AND-GATE")
    print("#   Only adjust threshold when sign(vix_chg) == sign(night_mom)")
    print("#   (i.e. both signals AGREE on direction); else keep flat BASE.")
    print("#   When they agree: adaptive_threshold = BASE + k*avg(vix_z, nm_z)")
    print("#   (avg of standardized signals, same sign by construction when gated)")
    print("############################################################")
    k_grid_b = [-2.0, -1.5, -1.0, -0.5, 0.5, 1.0, 1.5, 2.0]
    base_grid_b = [5.0, 6.0, 7.0]
    and_results = []

    def and_gate_threshold(d, base, k):
        agree = np.sign(d["vix_chg"]) == np.sign(d["night_mom"])
        combo = (d["vix_z"] + d["nm_z"]) / 2.0
        thr = pd.Series(base, index=d.index, dtype=float)
        thr[agree] = base + k * combo[agree]
        return thr

    for base in base_grid_b:
        for k in k_grid_b:
            thr = and_gate_threshold(train, base, k)
            sub = train[train["fgap"] >= thr]
            r = summarize(sub, f"AND-gate base={base} k={k}")
            r["base"], r["k"] = base, k
            and_results.append(r)

    eligible_b = [r for r in and_results if r["n"] >= 20]
    print(f"\nTotal settings swept: {len(and_results)}  (base x k = {len(base_grid_b)}x{len(k_grid_b)})")
    print(f"Settings with n>=20 on TRAIN: {len(eligible_b)}")
    if eligible_b:
        eligible_b_sorted = sorted(eligible_b, key=lambda r: r["sharpe"] if r["sharpe"] == r["sharpe"] else -999, reverse=True)
        print("\nTop 5 TRAIN settings by sharpe (n>=20 only):")
        for r in eligible_b_sorted[:5]:
            print_row(r)
        best_b = eligible_b_sorted[0]
        print(f"\n>>> TRAIN-SELECTED BEST (AND-gate): base={best_b['base']} k={best_b['k']}")
        print_row(best_b)

        thr_test_b = and_gate_threshold(test, best_b["base"], best_b["k"])
        sub_test_b = test[test["fgap"] >= thr_test_b]
        test_rb = summarize(sub_test_b, "TEST (train-selected params, AND-gate)")
        print_row(test_rb)

        thr_full_b = and_gate_threshold(df, best_b["base"], best_b["k"])
        sub_full_b = df[df["fgap"] >= thr_full_b]
        full_rb = summarize(sub_full_b, "FULL-74 honest check (train-selected params, AND-gate)")
        print_row(full_rb)

        print(f"\nComparison -- baseline test sharpe={b_test['sharpe']:.3f} (n={b_test['n']}) "
              f"vs AND-gate test sharpe={test_rb['sharpe']:.3f} (n={test_rb['n']})")
    else:
        print("No AND-gate setting reached n>=20 on TRAIN -- family B yields no eligible pick.")

    print("\n############################################################")
    print("# Full-74 honest check of the BASELINE itself")
    print("############################################################")
    print_row(b)


if __name__ == "__main__":
    main()
