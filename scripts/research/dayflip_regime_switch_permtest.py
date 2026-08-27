"""Permutation test for the Family A (|night_mom| regime switch) result found
in dayflip_regime_switch_fgap.py, to check the observed train-selection ->
test-evaluation pipeline isn't just multiple-testing luck.

Procedure: shuffle night_mom values across the 74 rows (breaking any real
date-alignment / autocorrelation with pnl_pct), then re-run the EXACT same
selection procedure (sweep 144 settings on train, require n>=20, pick best
train sharpe, evaluate once on test AND full). Repeat many times. Compare the
distribution of permuted full-sample sharpe (and test-sample sharpe) against
the real observed values.
"""
import sqlite3
import sys
import numpy as np
import pandas as pd

TRADELOG = "reports/research/branch-footprint-screen/dayflip_gapup_short/single_pick_tradelog.csv"
BARS_DB = "/Users/jackm4/goldenstocks-data/cache/tmf_channel/bars.sqlite"


def load_night_momentum(days):
    conn = sqlite3.connect(BARS_DB)
    days_str = sorted(set(d.strftime("%Y-%m-%d") for d in days))
    placeholders = ",".join("?" for _ in days_str)
    q = (
        f"select day, sess, t, c from bars where source='tx_1m_tick_built_582d' "
        f"and day in ({placeholders}) and sess in ('day','night')"
    )
    rows = conn.execute(q, days_str).fetchall()
    conn.close()
    df = pd.DataFrame(rows, columns=["day", "sess", "t", "c"])
    out = {}
    for day, g in df.groupby("day"):
        dg = g[g.sess == "day"]
        ng = g[g.sess == "night"]
        if dg.empty or ng.empty:
            continue
        dc = dg.sort_values("t").iloc[-1]["c"]
        nc = ng.sort_values("t").iloc[-1]["c"]
        out[day] = (nc / dc - 1.0) * 100.0
    return out


def sharpe_like(x):
    x = np.asarray(x, dtype=float)
    if len(x) < 2 or x.std(ddof=1) == 0:
        return np.nan
    return x.mean() / x.std(ddof=1)


def evaluate_abs(df, night_col, split_point, base_lo, base_hi):
    hi_mask = df[night_col].abs().values >= split_point
    eff_thresh = np.where(hi_mask, base_hi, base_lo)
    keep = df["fgap"].values >= eff_thresh
    sub = df.loc[keep]
    n = len(sub)
    if n == 0:
        return dict(n=0, mean=np.nan, sharpe=np.nan)
    return dict(n=n, mean=sub["pnl_pct"].mean(), sharpe=sharpe_like(sub["pnl_pct"]))


def run_pipeline(df, night_col, train_cutoff_date, split_points, base_pairs):
    train = df[df["signal_date"] <= train_cutoff_date]
    test = df[df["signal_date"] > train_cutoff_date]
    settings = []
    for sp in split_points:
        for blo, bhi in base_pairs:
            r = evaluate_abs(train, night_col, sp, blo, bhi)
            settings.append((sp, blo, bhi, r))
    eligible = [s for s in settings if s[3]["n"] >= 20]
    if not eligible:
        return None
    best = max(eligible, key=lambda s: s[3]["sharpe"] if not np.isnan(s[3]["sharpe"]) else -999)
    sp, blo, bhi, rtr = best
    rte = evaluate_abs(test, night_col, sp, blo, bhi)
    rfull = evaluate_abs(df, night_col, sp, blo, bhi)
    return dict(sp=sp, blo=blo, bhi=bhi, train=rtr, test=rte, full=rfull)


def main():
    df = pd.read_csv(TRADELOG, parse_dates=["signal_date", "trade_date"])
    df = df.sort_values("signal_date").reset_index(drop=True)
    n_train = int(round(len(df) * 0.7))
    train_cutoff_date = df.iloc[:n_train]["signal_date"].max()

    night_map = load_night_momentum(df["signal_date"])
    df["night_mom"] = df["signal_date"].dt.strftime("%Y-%m-%d").map(night_map)

    split_points = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0]
    base_pairs = []
    for base_lo in [4, 5, 6, 7]:
        for base_hi in [4, 5, 6, 7, 8]:
            if base_lo == base_hi:
                continue
            base_pairs.append((base_lo, base_hi))

    real = run_pipeline(df, "night_mom", train_cutoff_date, split_points, base_pairs)
    print("REAL (observed) result:")
    print(f"  setting: split=|night_mom|>={real['sp']}, lo={real['blo']}, hi={real['bhi']}")
    print(f"  train: n={real['train']['n']} sharpe={real['train']['sharpe']:.4f} mean={real['train']['mean']:.4f}")
    print(f"  test:  n={real['test']['n']} sharpe={real['test']['sharpe']:.4f} mean={real['test']['mean']:.4f}")
    print(f"  full:  n={real['full']['n']} sharpe={real['full']['sharpe']:.4f} mean={real['full']['mean']:.4f}")

    rng = np.random.default_rng(20260809)
    n_perm = 500
    perm_full_sharpe = []
    perm_test_sharpe = []
    perm_train_sharpe = []
    df_perm_base = df.copy()
    night_values = df["night_mom"].values.copy()

    for i in range(n_perm):
        shuffled = night_values.copy()
        rng.shuffle(shuffled)
        df_perm_base["night_mom_perm"] = shuffled
        res = run_pipeline(df_perm_base, "night_mom_perm", train_cutoff_date, split_points, base_pairs)
        if res is None:
            continue
        perm_train_sharpe.append(res["train"]["sharpe"])
        perm_test_sharpe.append(res["test"]["sharpe"])
        perm_full_sharpe.append(res["full"]["sharpe"])

    perm_full_sharpe = np.array([x for x in perm_full_sharpe if not np.isnan(x)])
    perm_test_sharpe = np.array([x for x in perm_test_sharpe if not np.isnan(x)])
    perm_train_sharpe = np.array([x for x in perm_train_sharpe if not np.isnan(x)])

    print(f"\nPermutation test (n={len(perm_full_sharpe)} valid permutations out of {n_perm}):")
    print(f"  train sharpe: real={real['train']['sharpe']:.4f}, perm mean={perm_train_sharpe.mean():.4f}, "
          f"perm p95={np.percentile(perm_train_sharpe,95):.4f}, "
          f"frac(perm>=real)={ (perm_train_sharpe>=real['train']['sharpe']).mean():.3f}")
    print(f"  test sharpe:  real={real['test']['sharpe']:.4f}, perm mean={perm_test_sharpe.mean():.4f}, "
          f"perm p95={np.percentile(perm_test_sharpe,95):.4f}, "
          f"frac(perm>=real)={ (perm_test_sharpe>=real['test']['sharpe']).mean():.3f}")
    print(f"  full sharpe:  real={real['full']['sharpe']:.4f}, perm mean={perm_full_sharpe.mean():.4f}, "
          f"perm p95={np.percentile(perm_full_sharpe,95):.4f}, "
          f"frac(perm>=real)={ (perm_full_sharpe>=real['full']['sharpe']).mean():.3f}")


if __name__ == "__main__":
    main()
