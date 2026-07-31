"""Stage-8 CROSS-LAYER COMPOSITE — honest walk-forward test of "1+1>2".

Family: take the FEW legs that showed ANY signal across the 6 null rounds and
combine them. Does a composite beat the champion (tech-gated) OOS Sharpe ~ +2.68?

Legs (each oriented risk-on = higher -> long TAIEX next day, PIT expanding z-scores):
  L1 champ   = fut_foreign_oi z60             (positioning)          [full panel]
  L2 vix     = -z(VIX)                         (global vol, risk-on)  [2018+]
  L3 maint   =  z(market maintenance_pct)      (market leverage health)[2018+]
  L4 nearcb  = -z(near-call breadth br_140)    (fewer stressed = risk-on)[2020+]
  L5 medmnt  =  z(mid-small median maintenance)(leverage breadth)     [2020+]

fwd = next-day OPEN->CLOSE return (t+1, no look-ahead), reused from integrated panel.

Composite methods (all counted as trials for DSR):
  (a) equal-weight sign vote          -> long if mean(signed z legs) > 0
  (b) IC-shrunk weights (expanding)    -> weight_i = shrink * spearmanIC_i(past)
  (c) shallow logistic (expanding)     -> long if P(fwd>0 | 5 legs) > 0.5
  + robustness variants (thresholds / #legs-required) also counted.

Everything walk-forward EXPANDING window (refit monthly, only past data).
DSR uses N = every distinct method/variant actually run (honest trial count).
Each survivor answered: is it just champion or MA200-regime restated? (collinearity + residual)
"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd
from scipy.stats import norm, spearmanr

ROOT = Path(__file__).resolve().parents[3]
DASH = ROOT / "data" / "research" / "dashboard"
ANN = np.sqrt(252)
GAMMA = 0.5772156649
COST_BPS = 4.0
MINHIST = 250          # min days before a leg z-score / model is usable
REFIT_EVERY = 21       # ~monthly refit of walk-forward weights/models

# ---------- assemble panel ----------
ig = pd.read_parquet(DASH / "integrated_multigate.parquet").copy()
ig["date"] = pd.to_datetime(ig["date"])
gm = pd.read_parquet(DASH / "global_macro_data.parquet")[["date", "VIX"]].copy()
gm["date"] = pd.to_datetime(gm["date"])
nb = pd.read_parquet(DASH / "margin_nearcall_breadth_data.parquet")[["date", "br_140", "med_maint"]].copy()
nb["date"] = pd.to_datetime(nb["date"])

df = ig.merge(gm, on="date", how="left").merge(nb, on="date", how="left").sort_values("date").reset_index(drop=True)
df["VIX"] = df["VIX"].ffill(limit=3)

def ez(s):
    """Point-in-time EXPANDING z-score (mean/std from data up to & incl. t)."""
    mu = s.expanding(MINHIST).mean()
    sd = s.expanding(MINHIST).std()
    return (s - mu) / sd

# signed risk-on legs
legs = pd.DataFrame(index=df.index)
legs["champ"]  = df["champ_z60"]            # already a z60 score
legs["vix"]    = -ez(df["VIX"])
legs["maint"]  =  ez(df["maintenance_pct"])
legs["nearcb"] = -ez(df["br_140"])
legs["medmnt"] =  ez(df["med_maint"])
LEG_COLS = ["champ", "vix", "maint", "nearcb", "medmnt"]

fwd = df["fwd"].copy()                        # next-day open->close, t+1
oos = df["oos"].values.astype(bool)
r_cc = df["ix_close"] / df["ix_close"].shift(1) - 1.0

# ---------- return engine ----------
def strat_ret(pos):
    """pos in {0,1} decided at close t; earn fwd(t)=open(t+1)->close(t+1); cost on turnover."""
    pos = pd.Series(pos, index=df.index).astype(float).fillna(0.0)
    turn = pos.diff().abs().fillna(pos.abs())
    return (pos * fwd - turn * COST_BPS / 1e4)

def sharpe(r, mask=None):
    r = pd.Series(r)
    if mask is not None:
        r = r[mask]
    r = r.dropna()
    return r.mean() / r.std() * ANN if (len(r) > 20 and r.std() > 0) else np.nan

# ---------- walk-forward composite builders ----------
def wf_equal(threshold=0.0, min_legs=2):
    """(a) equal-weight sign vote using ALL currently-available legs each day."""
    L = legs[LEG_COLS]
    avail = L.notna().sum(axis=1)
    score = L.mean(axis=1)                     # mean over available legs (nan-skip)
    pos = ((score > threshold) & (avail >= min_legs)).astype(float)
    return pos

def wf_icweight(shrink=0.5, threshold=0.0):
    """(b) IC-shrunk weighted, refit monthly on expanding past window."""
    pos = pd.Series(0.0, index=df.index)
    w = pd.Series(0.0, index=LEG_COLS)
    for t in range(MINHIST, len(df)):
        if (t - MINHIST) % REFIT_EVERY == 0:
            past = slice(0, t)                 # strictly < t (fwd[t-1] uses close t-1->t; known at close t)
            L = legs.iloc[past][LEG_COLS]
            y = fwd.iloc[past]
            ic = {}
            for c in LEG_COLS:
                x = L[c]; msk = x.notna() & y.notna()
                if msk.sum() > 60:
                    rho, _ = spearmanr(x[msk], y[msk])
                    ic[c] = 0.0 if np.isnan(rho) else rho
                else:
                    ic[c] = 0.0
            w = pd.Series(ic) * shrink
        row = legs.iloc[t][LEG_COLS]
        val = np.nansum(row.values * w.reindex(LEG_COLS).values)
        pos.iloc[t] = 1.0 if val > threshold else 0.0
    return pos

def wf_logistic(threshold=0.5):
    """(c) shallow logistic on 5 legs, refit monthly, expanding window."""
    from sklearn.linear_model import LogisticRegression
    pos = pd.Series(0.0, index=df.index)
    model = None; mean = None; std = None
    for t in range(MINHIST, len(df)):
        if (t - MINHIST) % REFIT_EVERY == 0:
            L = legs.iloc[:t][LEG_COLS].copy()
            y = (fwd.iloc[:t] > 0).astype(int)
            m = L.notna().all(axis=1) & fwd.iloc[:t].notna()
            if m.sum() > 120 and y[m].nunique() == 2:
                Xtr = L[m]; mean = Xtr.mean(); std = Xtr.std().replace(0, 1)
                Xn = (Xtr - mean) / std
                model = LogisticRegression(C=0.5, max_iter=500).fit(Xn, y[m])
        row = legs.iloc[t][LEG_COLS]
        if model is not None and row.notna().all():
            xn = ((row - mean) / std).values.reshape(1, -1)
            p = model.predict_proba(xn)[0, 1]
            pos.iloc[t] = 1.0 if p > threshold else 0.0
    return pos

# ---------- DSR ----------
def dsr(ret, var_trials, N, mask=None):
    r = pd.Series(ret)
    if mask is not None:
        r = r[mask]
    r = r.dropna().values
    n = len(r)
    if n < 20 or r.std() == 0:
        return np.nan, np.nan
    sr = r.mean() / r.std()
    g3 = pd.Series(r).skew(); g4 = pd.Series(r).kurtosis() + 3
    ss = np.sqrt(var_trials) * ((1 - GAMMA) * norm.ppf(1 - 1.0 / N) + GAMMA * norm.ppf(1 - 1.0 / (N * np.e)))
    d = norm.cdf((sr - ss) * np.sqrt(n - 1) / np.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr**2))
    return d, sr * ANN

# ============ RUN ============
print("legs available (non-nan counts):")
print(legs[LEG_COLS].notna().sum().to_string())
print("\nLEG CORRELATION (the collinearity check — how distinct are these really?):")
print(legs[LEG_COLS].corr().round(2).to_string())

# champion benchmarks recomputed on identical sample
champ_alone = strat_ret(df["champ"])
tech_gated  = strat_ret((df["champ"].astype(bool) & df["tech_ok"].astype(bool)).astype(float))
techvix     = strat_ret((df["champ"].astype(bool) & df["tech_ok"].astype(bool) & df["vix_ok"].astype(bool)).astype(float))

# composite variants — EVERY ONE COUNTS AS A TRIAL
variants = {}
for thr in [0.0, 0.1, 0.2]:
    for ml in [2, 3, 4]:
        variants[f"eq_thr{thr}_ml{ml}"] = wf_equal(thr, ml)
for sh in [0.3, 0.5, 1.0]:
    for thr in [0.0, 0.1]:
        variants[f"ic_sh{sh}_thr{thr}"] = wf_icweight(sh, thr)
for thr in [0.5, 0.55]:
    variants[f"logit_thr{thr}"] = wf_logistic(thr)

N_TRIALS = len(variants)
print(f"\n=== {N_TRIALS} composite variants run (this IS the DSR trial count) ===")

# OOS trial-SR dispersion for DSR SR* (daily, OOS)
oos_series = pd.Series(oos, index=df.index)
trial_sr_daily = []
rows = []
for name, pos in variants.items():
    r = strat_ret(pos)
    ro = r[oos_series]; ro = ro.dropna()
    if len(ro) > 20 and ro.std() > 0:
        trial_sr_daily.append(ro.mean() / ro.std())
    rows.append((name, sharpe(r), sharpe(r, oos_series), pos[oos_series].mean()))
var_trials = np.var(trial_sr_daily, ddof=1) if len(trial_sr_daily) > 1 else 1e-4

print("\n  variant                     full-Shp   OOS-Shp   OOS-expo")
for name, f, o, e in sorted(rows, key=lambda x: -(x[2] if not np.isnan(x[2]) else -9)):
    print(f"  {name:26s}  {f:+6.2f}    {o:+6.2f}    {e*100:4.0f}%")

# benchmarks on same sample
print("\n  --- BENCHMARKS (same sample) ---")
for nm, r in [("B&H close-close", r_cc), ("champ_alone", champ_alone),
              ("champ x tech (system B)", tech_gated), ("champ x tech x vix (C)", techvix)]:
    print(f"  {nm:26s}  {sharpe(r):+6.2f}    {sharpe(r, oos_series):+6.2f}")

# best composite -> full DSR + redundancy audit
best_name = max(rows, key=lambda x: (x[2] if not np.isnan(x[2]) else -9))[0]
best_pos = variants[best_name]
best_ret = strat_ret(best_pos)
d_full, ann_full = dsr(best_ret, var_trials, N_TRIALS)
d_oos, ann_oos = dsr(best_ret, var_trials, N_TRIALS, oos_series)
print(f"\n=== BEST composite = {best_name} ===")
print(f"  full: annSharpe {ann_full:+.2f}  DSR {d_full:.3f}   (N={N_TRIALS})")
print(f"  OOS : annSharpe {ann_oos:+.2f}  DSR {d_oos:.3f}")
print(f"  -> {'SURVIVES' if (d_oos>0.95) else 'FAILS'} strict multiple-testing DSR")

# redundancy: is best composite just champion / MA200 regime?
sub = pd.DataFrame({"comp": best_pos.astype(float), "champ": df["champ"].astype(float),
                    "bull200": df["close_gt_ma200"].astype(float)}).dropna()
print("\n  redundancy — position correlation:")
print(f"    corr(comp, champ)   = {sub['comp'].corr(sub['champ']):+.2f}")
print(f"    corr(comp, bull200) = {sub['comp'].corr(sub['bull200']):+.2f}")
# does composite beat champion-tech OOS? residual test: composite return regressed on tech_gated return
import numpy as _np
cr = pd.DataFrame({"comp": best_ret, "bench": tech_gated})[oos_series].dropna()
if len(cr) > 30 and cr["bench"].std() > 0:
    beta = _np.cov(cr["comp"], cr["bench"])[0, 1] / _np.var(cr["bench"])
    alpha_d = cr["comp"].mean() - beta * cr["bench"].mean()
    resid = cr["comp"] - (alpha_d + beta * cr["bench"])
    t_alpha = alpha_d / (resid.std() / _np.sqrt(len(cr)))
    print(f"\n  residual vs system-B (tech-gated champ), OOS:")
    print(f"    beta={beta:+.2f}  daily alpha={alpha_d*1e4:+.1f}bp  t(alpha)={t_alpha:+.2f}")
    print(f"    -> composite {'adds' if t_alpha>2 else 'does NOT add'} return beyond the tech-gated champion")

out = DASH / "crosslayer_composite_wf.parquet"
save = df[["date", "ix_close", "oos"]].copy()
save["best_composite_pos"] = best_pos.values
save["best_composite_ret"] = best_ret.values
save["tech_gated_ret"] = tech_gated.values
save.to_parquet(out)
print(f"\nsaved -> {out}")
