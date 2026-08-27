"""Permutation control for dayflip_nonlinear_night_momentum.py.

Runs the EXACT same "sweep on train (n>=20 eligible), pick best-by-sharpe,
evaluate once on test" procedure but with a random-noise feature that has
nothing to do with night momentum. If a large fraction of random features
also "beat" the flat-6% baseline under this procedure, that proves the
procedure itself is biased toward false positives on this sample (right-
skewed payoff: most wins capped at pnl=1.95%, losses variable-magnitude),
and any single real-feature "win" from the same procedure is not credible
evidence without this control.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from dayflip_nonlinear_night_momentum import load_tradelog, load_night_momentum, attach_night_momentum, summarize


def main():
    trades = load_tradelog()
    night = load_night_momentum()
    df = attach_night_momentum(trades, night)
    n_split = int(len(df) * 0.7)

    rng = np.random.default_rng(42)
    n_perm = 300
    bases = [4.0, 5.0, 6.0, 7.0]
    ks = [0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0]

    base_test = summarize(df.iloc[n_split:], df.iloc[n_split:]["fgap"] >= 6.0)

    wins = 0
    test_sharpes = []
    for _ in range(n_perm):
        fake = pd.Series(
            rng.normal(0, df["night_return"].abs().std(), size=len(df)), index=df.index
        ).abs()
        d2 = df.copy()
        d2["fake_mag"] = fake
        train, test = d2.iloc[:n_split], d2.iloc[n_split:]
        rows = []
        for base in bases:
            for k in ks:
                thr = base + k * train["fake_mag"]
                rows.append(dict(base=base, k=k, **summarize(train, train["fgap"] >= thr)))
        rdf = pd.DataFrame(rows)
        elig = rdf[rdf["n"] >= 20]
        if elig.empty:
            continue
        best = elig.sort_values("sharpe", ascending=False).iloc[0]
        thr_test = best["base"] + best["k"] * test["fake_mag"]
        test_s = summarize(test, test["fgap"] >= thr_test)
        if pd.isna(test_s["sharpe"]):
            continue
        test_sharpes.append(test_s["sharpe"])
        if test_s["sharpe"] > base_test["sharpe"]:
            wins += 1

    arr = np.array(test_sharpes)
    print(f"permutations run: {len(arr)}")
    print(f"baseline flat-6% test sharpe: {base_test['sharpe']:.3f}")
    print(f"fraction of RANDOM-noise-feature sweeps beating baseline test sharpe: {wins / len(arr):.3f}")
    print(
        f"random-feature test sharpe distribution: mean={arr.mean():.3f} "
        f"median={np.median(arr):.3f} p90={np.percentile(arr, 90):.3f} max={arr.max():.3f}"
    )


if __name__ == "__main__":
    main()
