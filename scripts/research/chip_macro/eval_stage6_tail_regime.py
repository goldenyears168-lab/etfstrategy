"""Stage-6: tail-conditioning (IRFE 2011) + regime-split (bull/bear) on foreign-futures OI.

Motivation from the literature:
  - Chang et al. (IRFE 2011): linear OI/imbalance regressions are insignificant, but
    EXTREME imbalance + extreme recent return predicts (reversal at the low tail,
    residual momentum at the high tail). => test the signal in the TAILS, not linearly.
  - Herding literature (Cogent 2025): institutional-flow predictability is regime-
    dependent (stabilizes in downturns, fails in booms). => split bull vs bear.

No look-ahead: chip value known after close t; regime = ix_close[t] vs its trailing
200d MA (known at t); position at t applied to open(t+1)->close(t+1) daily return.
Stats use next-DAY (non-overlapping) returns so t-stats aren't overlap-inflated.
Any tradable winner gets a Deflated-Sharpe re-check counting the extra trials.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[3]
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
OUT = ROOT / "reports" / "research" / "chip-macro"
ANN = np.sqrt(252)
GAMMA = 0.5772156649
COST_BPS = 4.0


def rz(s, w):
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def sharpe(x):
    x = pd.Series(x).dropna()
    return x.mean() / x.std() * ANN if len(x) > 20 and x.std() > 0 else np.nan


def lf(pos, day_ret, cost=COST_BPS):
    pos = pd.Series(pos, index=day_ret.index).astype(float).fillna(0.0)
    return (pos * day_ret - pos.diff().abs().fillna(pos.abs()) * cost / 1e4).dropna()


def tstat(x):
    x = pd.Series(x).dropna()
    return x.mean() / (x.std() / np.sqrt(len(x))) if len(x) > 5 and x.std() > 0 else np.nan


def dsr(ret, var_trials, N):
    r = pd.Series(ret).dropna().values
    n = len(r); sr = r.mean() / r.std()
    g3 = pd.Series(r).skew(); g4 = pd.Series(r).kurtosis() + 3
    sr_star = np.sqrt(var_trials) * ((1 - GAMMA) * norm.ppf(1 - 1/N) + GAMMA * norm.ppf(1 - 1/(N*np.e)))
    return norm.cdf((sr - sr_star) * np.sqrt(n - 1) / np.sqrt(1 - g3*sr + (g4-1)/4*sr**2)), sr*ANN


def main():
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    day_ret = (p["ix_close"] / p["ix_open"] - 1.0).shift(-1)
    fwd1 = day_ret  # next-day, non-overlapping
    n = len(p); oos = pd.Series(p.index >= int(n * 0.7), index=p.index)

    z60 = rz(p["fut_foreign_oi"], 60)
    doi_z = rz(p["fut_foreign_doi"], 20)          # daily OI change, standardized
    ret5 = p["ix_close"].pct_change(5)            # trailing 5d return (known at t)
    ma200 = p["ix_close"].rolling(200, min_periods=200).mean()
    bull = p["ix_close"] > ma200                  # regime known at t

    # ================= EXPERIMENT 1: TAIL CONDITIONING =================
    print("=" * 70)
    print("EXP 1 — tail-conditioning (IRFE-2011 style 2x2), stats on next-day return")
    print("=" * 70)
    for zname, z in [("fut_foreign_oi z60 (level)", z60), ("fut_foreign_doi z20 (change)", doi_z)]:
        d = pd.DataFrame({"z": z, "ret5": ret5, "f": fwd1}).dropna()
        qlo, qhi = d["z"].quantile(0.2), d["z"].quantile(0.8)
        def zb(v):
            return "LOW20" if v <= qlo else ("HIGH20" if v >= qhi else "mid")
        d["zbucket"] = d["z"].map(zb)
        d["retdir"] = np.where(d["ret5"] >= 0, "up", "down")
        print(f"\n[{zname}]  next-day mean % by (OI bucket × trailing-5d ret):")
        tbl = d.groupby(["zbucket", "retdir"])["f"].agg(["mean", "count"])
        for (zbk, rd), row in tbl.iterrows():
            sub = d[(d.zbucket == zbk) & (d.retdir == rd)]["f"]
            print(f"    {zbk:6s} × {rd:4s}: mean={row['mean']*100:+.3f}%  t={tstat(sub):+.2f}  n={int(row['count'])}")

    # tail-only trading rule from the level signal: long HIGH20, short LOW20, flat mid
    tail_pos = pd.Series(0.0, index=p.index)
    tail_pos[z60 >= z60.quantile(0.8)] = 1.0
    tail_pos[z60 <= z60.quantile(0.2)] = -1.0
    tail_ret = lf(tail_pos, day_ret)
    # NOTE: quantile over full sample = mild look-ahead in thresholds; also do rolling-quantile version
    rq_hi = z60.rolling(252, min_periods=120).quantile(0.8)
    rq_lo = z60.rolling(252, min_periods=120).quantile(0.2)
    tail_pos_r = pd.Series(0.0, index=p.index)
    tail_pos_r[z60 >= rq_hi] = 1.0
    tail_pos_r[z60 <= rq_lo] = -1.0
    tail_ret_r = lf(tail_pos_r, day_ret)
    print("\n  tail L/S strategies OOS Sharpe:")
    print(f"    static-quantile  : {sharpe(tail_ret[oos.reindex(tail_ret.index).fillna(False)]):+.2f}")
    print(f"    rolling-quantile : {sharpe(tail_ret_r[oos.reindex(tail_ret_r.index).fillna(False)]):+.2f}  (no-lookahead)")
    print(f"    champion z60>0 LF: {sharpe(lf((z60>0).astype(float), day_ret)[oos.reindex(day_ret.index).fillna(False)]):+.2f}")

    # ================= EXPERIMENT 3: REGIME SPLIT =================
    print("\n" + "=" * 70)
    print("EXP 3 — regime split (bull = TAIEX > 200d MA), champion z60>0")
    print("=" * 70)
    champ_pos = (z60 > 0).astype(float)
    champ = lf(champ_pos, day_ret)
    reg = bull.reindex(champ.index)
    valid = reg.notna()
    for rname, mask in [("BULL (>MA200)", reg == True), ("BEAR (<MA200)", reg == False)]:
        m = mask & valid
        print(f"  {rname:16s}: champ Sharpe={sharpe(champ[m]):+.2f}  B&H={sharpe(day_ret.reindex(champ.index)[m]):+.2f}  n={int(m.sum())}")
    # % of time bull
    print(f"  bull fraction of days: {bull.mean()*100:.0f}%")

    # regime-interacted rules
    print("\n  regime-interacted rules (OOS Sharpe):")
    dr = day_ret
    rules = {
        "z60>0 always (champ)":        (z60 > 0).astype(float),
        "z60>0 ONLY in bull":          ((z60 > 0) & bull).astype(float),
        "z60>0 ONLY in bear":          ((z60 > 0) & ~bull).astype(float),
        "long if bull else flat (MA only)": bull.astype(float),
        "z60>0 AND bull (both filters)":    ((z60 > 0) & bull).astype(float),
        "bull, but flat if z60<0 (chip veto)": (bull & (z60 > 0)).astype(float),
    }
    oosm = oos
    best = None
    for name, pos in rules.items():
        r = lf(pos, dr)
        s_oos = sharpe(r[oosm.reindex(r.index).fillna(False)])
        s_full = sharpe(r)
        expo = pd.Series(pos, index=p.index).fillna(0).astype(bool).mean()
        print(f"    {name:36s} OOS={s_oos:+.2f}  full={s_full:+.2f}  expo={expo*100:.0f}%")
        if best is None or (pd.notna(s_oos) and s_oos > best[1]):
            best = (name, s_oos, r)

    # combined tail × regime: only take tail signals in the 'right' regime
    print("\n  tail × regime combos (OOS Sharpe):")
    combos = {
        "long HIGH20 z60 only in bull": pd.Series(np.where((z60 >= z60.quantile(0.8)) & bull, 1.0, 0.0), index=p.index),
        "short LOW20 z60 only in bear": pd.Series(np.where((z60 <= z60.quantile(0.2)) & ~bull, -1.0, 0.0), index=p.index),
        "bull-long + bear-short(LOW20)": pd.Series(np.where(bull, 1.0, np.where(z60 <= z60.quantile(0.2), -1.0, 0.0)), index=p.index),
    }
    for name, pos in combos.items():
        r = lf(pos, dr)
        print(f"    {name:34s} OOS={sharpe(r[oosm.reindex(r.index).fillna(False)]):+.2f}  full={sharpe(r):+.2f}")

    # ================= DSR re-check on the best regime rule =================
    print("\n" + "=" * 70)
    print("Deflated-Sharpe re-check on best rule (counting extra Stage-6 trials)")
    print("=" * 70)
    # trial dispersion across the regime rules + tail rules we just tried
    trial_rets = list(rules.values()) + list(combos.values()) + [tail_pos, tail_pos_r]
    srs = []
    for pos in trial_rets:
        r = lf(pos, dr); ro = r[oosm.reindex(r.index).fillna(False)]
        if len(ro) > 20 and ro.std() > 0:
            srs.append(ro.mean() / ro.std())
    var_trials = np.var(srs, ddof=1)
    N = 135 + len(srs)  # prior search + this batch
    name, s_oos, r_best = best
    d_full, ann_full = dsr(lf(rules[name], dr), var_trials, N)
    r_oos = lf(rules[name], dr); r_oos = r_oos[oosm.reindex(r_oos.index).fillna(False)]
    d_oos, ann_oos = dsr(r_oos, var_trials, N)
    print(f"  best rule: {name}")
    print(f"  N_trials={N}  full: annSharpe={ann_full:+.2f} DSR={d_full:.3f}  |  OOS: annSharpe={ann_oos:+.2f} DSR={d_oos:.3f}")
    print(f"  -> {'SURVIVES' if d_oos>0.95 else 'FAILS'} 5% deflated bar")


if __name__ == "__main__":
    main()
