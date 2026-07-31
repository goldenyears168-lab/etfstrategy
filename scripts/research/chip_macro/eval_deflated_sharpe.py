"""Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014) for the champion.

Academic reviewers of this kind of work insist that a Sharpe found after screening
many strategies be DEFLATED for (a) number of trials, (b) return skew/kurtosis,
(c) sample length. We tested ~110 signal variants, so a raw Sharpe overstates skill.

PSR(SR*) = Prob( true SR > SR* ) given observed SR, n, skew g3, kurt g4:
    PSR = Phi( (SR - SR*) * sqrt(n-1) / sqrt(1 - g3*SR + (g4-1)/4 * SR^2) )
DSR = PSR evaluated at SR* = expected max Sharpe under N independent null trials:
    SR* = sqrt(Var_trials) * [ (1-gamma)*Z^{-1}(1-1/N) + gamma*Z^{-1}(1-1/(N*e)) ]
(gamma = Euler-Mascheroni). DSR > 0.95 => survives multiple-testing at 5%.
All Sharpes here are per-period (daily); n = number of daily returns.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[3]
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
GAMMA = 0.5772156649  # Euler-Mascheroni
COST_BPS = 4.0


def rz(s, w):
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def lf(bull, day_ret, cost=COST_BPS):
    pos = pd.Series(bull, index=day_ret.index).astype(float).fillna(0.0)
    return (pos * day_ret - pos.diff().abs().fillna(pos.abs()) * cost / 1e4).dropna()


def psr(sr, n, g3, g4, sr_star):
    """Probabilistic Sharpe Ratio (per-period SR)."""
    denom = np.sqrt(1 - g3 * sr + (g4 - 1) / 4.0 * sr**2)
    return norm.cdf((sr - sr_star) * np.sqrt(n - 1) / denom)


def expected_max_sr(var_trials, N):
    """E[max SR] across N independent null strategies with per-trial SR variance."""
    e = np.e
    return np.sqrt(var_trials) * (
        (1 - GAMMA) * norm.ppf(1 - 1.0 / N) + GAMMA * norm.ppf(1 - 1.0 / (N * e))
    )


def report(name, ret, var_trials, N):
    r = ret.values
    n = len(r)
    sr = r.mean() / r.std()  # daily SR
    g3 = pd.Series(r).skew()
    g4 = pd.Series(r).kurtosis() + 3.0  # pandas gives excess kurt
    sr_star = expected_max_sr(var_trials, N)
    dsr = psr(sr, n, g3, g4, sr_star)
    psr0 = psr(sr, n, g3, g4, 0.0)
    ann = sr * np.sqrt(252)
    print(f"\n[{name}]  n={n}  ann.Sharpe={ann:+.2f}  daily SR={sr:+.4f}  skew={g3:+.2f} kurt={g4:.1f}")
    print(f"  PSR(vs 0)         = {psr0:.3f}   (prob true Sharpe>0)")
    print(f"  N_trials={N}  SR*_expected_max(daily)={sr_star:.4f} (ann {sr_star*np.sqrt(252):+.2f})")
    print(f"  DEFLATED SR (PSR vs SR*) = {dsr:.3f}   -> {'SURVIVES' if dsr>0.95 else 'FAILS'} 5% multiple-testing")


def main():
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    day_ret = (p["ix_close"] / p["ix_open"] - 1.0).shift(-1)
    fut = p["fut_foreign_oi"]
    n = len(p); oos = pd.Series(p.index >= int(n * 0.7), index=p.index)

    # trial dispersion: Sharpe variance across the param grid (window x threshold), the honest search space
    wins = [40, 60, 90, 120, 180]
    thrs = [-0.5, -0.25, 0.0, 0.25, 0.5]
    trial_sr = []
    for w in wins:
        z = rz(fut, w)
        for t in thrs:
            r = lf(z > t, day_ret)
            ro = r[oos.reindex(r.index).fillna(False)]
            if len(ro) > 20 and ro.std() > 0:
                trial_sr.append(ro.mean() / ro.std())  # daily SR OOS
    var_trials = np.var(trial_sr, ddof=1)
    # be conservative on N: whole stage-1 scan (~110) + this grid (25)
    N = 110 + len(trial_sr)
    print(f"trial SR dispersion: n_trials(for var)={len(trial_sr)}  var={var_trials:.2e}  "
          f"std(daily SR)={np.sqrt(var_trials):.4f}  N_effective={N}")

    z60 = rz(fut, 60)
    champ_full = lf(z60 > 0, day_ret)
    champ_oos = champ_full[oos.reindex(champ_full.index).fillna(False)]
    report("champion z60>0  FULL", champ_full, var_trials, N)
    report("champion z60>0  OOS", champ_oos, var_trials, N)


if __name__ == "__main__":
    main()
