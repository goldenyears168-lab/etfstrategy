"""Decisive test: does the breadth-strong axis add REAL drift OVER the known
champ x MA200 regime interaction, or is the state-grid winner just a restatement?

Reuses the exact same a-priori binaries as state_grid_conditional_returns.py.
No new threshold search (trials unchanged). Three decisive checks:

 1. Increment table: within S = {champ+ & above_MA200}, split by breadth_str /
    vix_hi -> conditional fwd20; is breadth-strong the discriminator?
 2. Breadth-permutation WITHIN S: randomly relabel which days are "breadth strong"
    (keep same count) 5000x; how often does a random split beat the observed
    strong-minus-weak fwd20 gap? -> is it breadth specifically, not any split?
 3. Overlapping 20-day-hold tradable strategy (position = mean of last FWD fire
    flags) for baseline champ, champ x MA200, and champ x MA200 x breadthStr;
    annualized Sharpe + Deflated-Sharpe with the SAME 16-cell trial dispersion.
"""
from __future__ import annotations
from pathlib import Path
import sqlite3
import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[3]
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
MACRO = ROOT / "data" / "research" / "dashboard" / "global_macro_data.parquet"
BREADTH = ROOT / "data" / "research" / "dashboard" / "pct_ma200_breadth.parquet"
ANN = np.sqrt(252); GAMMA = 0.5772156649; FWD = 20; COST_BPS = 4.0
RNG = np.random.default_rng(23)


def rz(s, w):
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def psr(sr, n, g3, g4, sr_star):
    return norm.cdf((sr - sr_star) * np.sqrt(n - 1) / np.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr**2))


def emax(var, N):
    return np.sqrt(var) * ((1 - GAMMA) * norm.ppf(1 - 1 / N) + GAMMA * norm.ppf(1 - 1 / (N * np.e)))


def main():
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    p["date"] = pd.to_datetime(p["date"])
    macro = pd.read_parquet(MACRO)[["date", "VIX"]].copy(); macro["date"] = pd.to_datetime(macro["date"])
    bd = pd.read_parquet(BREADTH)[["date", "pct_above_ma200"]]; bd["date"] = pd.to_datetime(bd["date"])
    p = p.merge(macro, on="date", how="left").merge(bd, on="date", how="inner").sort_values("date").reset_index(drop=True)

    day_ret = (p["ix_close"] / p["ix_open"] - 1.0).shift(-1)
    fwd20 = p["ix_close"].shift(-FWD) / p["ix_open"].shift(-1) - 1.0
    z60 = rz(p["fut_foreign_oi"], 60)
    A = z60 > 0
    B = p["VIX"] > p["VIX"].rolling(252, min_periods=120).median()
    C = p["pct_above_ma200"] > p["pct_above_ma200"].rolling(252, min_periods=120).median()
    ma200 = p["ix_close"].rolling(200, min_periods=200).mean()
    D = p["ix_close"] > ma200
    valid = A.notna() & B.notna() & C.notna() & D.notna() & z60.notna() & ma200.notna()

    def cond(mask):
        f = fwd20[mask & valid & fwd20.notna()]
        return len(f), f.mean() * 100, (f > 0).mean() * 100

    print("=== Increment within S = champ+ & above_MA200 ===")
    S = A & D & valid
    for name, mm in [
        ("ALL S (champ+ & aMA200)", S),
        ("  + breadth STRONG", S & C),
        ("  + breadth WEAK", S & ~C),
        ("  + vix HIGH", S & B),
        ("  + vix LOW", S & ~B),
        ("  + brStr & vixHi", S & C & B),
        ("  + brStr & vixLo", S & C & ~B),
    ]:
        n, mu, w = cond(mm)
        print(f"  {name:32s} n={n:4d}  fwd20={mu:+.2f}%  win={w:.0f}%")

    # baseline champ- & aMA200 for contrast (breadth effect outside champ+?)
    print("=== breadth effect OUTSIDE champ+ (control: champ- & aMA200) ===")
    Sc = (~A) & D & valid
    for name, mm in [("champ- & aMA200 & brStr", Sc & C), ("champ- & aMA200 & brWk", Sc & ~C)]:
        n, mu, w = cond(mm); print(f"  {name:32s} n={n:4d}  fwd20={mu:+.2f}%  win={w:.0f}%")

    # -------- (2) breadth-permutation within S --------
    fS = fwd20[S & fwd20.notna()]
    cS = C[S & fwd20.notna()].astype(bool).values
    fSv = fS.values
    obs_gap = fSv[cS].mean() - fSv[~cS].mean()
    k = int(cS.sum()); NN = len(cS)
    null = np.empty(5000)
    for i in range(5000):
        idx = RNG.choice(NN, k, replace=False)
        mm = np.zeros(NN, bool); mm[idx] = True
        null[i] = fSv[mm].mean() - fSv[~mm].mean()
    pperm = float((null >= obs_gap).mean())
    print(f"\n=== breadth-strong permutation WITHIN S ===")
    print(f"  observed (brStr - brWk) fwd20 gap = {obs_gap*100:+.2f}%   n_strong={k}/{NN}")
    print(f"  random-split null: mean={null.mean()*100:+.2f}%  p(random>=obs)={pperm:.4f}")

    # -------- (3) overlapping 20-day-hold tradable Sharpe + DSR --------
    idxret = (p["ix_close"] / p["ix_close"].shift(1) - 1.0)   # daily index return for holding
    def overlap_strat(fire):
        f = fire.astype(float).reindex(p.index).fillna(0.0)
        pos = f.shift(1).rolling(FWD, min_periods=1).mean()   # enter next day, hold FWD days
        r = pos * idxret - pos.diff().abs().fillna(0) * (COST_BPS / 1e4)
        return r.dropna()

    # trial dispersion across the 16 cells (overlapping-strat daily SR, OOS)
    n = len(p); oos = pd.Series(p.index >= int(n * 0.7), index=p.index)
    tsr = []
    for a in (True, False):
        for b in (True, False):
            for c in (True, False):
                for d in (True, False):
                    m = valid & (A == a) & (B == b) & (C == c) & (D == d)
                    r = overlap_strat(m)
                    ro = r[oos.reindex(r.index).fillna(False)]
                    if len(ro) > 20 and ro.std() > 0:
                        tsr.append(ro.mean() / ro.std())
    var = np.var(tsr, ddof=1); N16 = 16
    print(f"\n=== overlapping 20d-hold tradable strategies (DSR trial disp: var={var:.2e}, "
          f"SR*_ann={emax(var,N16)*ANN:+.2f}, N={N16}) ===")
    for name, fire in [
        ("champ+ only", A & valid),
        ("champ+ x aMA200 (known regime)", A & D & valid),
        ("champ+ x aMA200 x brStr (candidate)", A & D & C & valid),
        ("champ+ x aMA200 x brWk (contrast)", A & D & ~C & valid),
    ]:
        r = overlap_strat(fire); rr = r.values
        s = rr.mean() / rr.std(); g3 = pd.Series(rr).skew(); g4 = pd.Series(rr).kurtosis() + 3
        d = psr(s, len(rr), g3, g4, emax(var, N16))
        print(f"  {name:38s} ann.Sh={s*ANN:+.2f}  DSR={d:.3f}  {'SURVIVE' if d>0.95 else 'fail'}")


if __name__ == "__main__":
    main()
