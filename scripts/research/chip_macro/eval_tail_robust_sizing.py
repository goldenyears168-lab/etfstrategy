"""Tail-robust position-sizing variants on top of the FROZEN SYSTEM L0xL2 signal.

Motivation (RESEARCH_LOG.md Stage 8 / Stage 5): the champion signal's DSR fails
mainly because of return kurtosis (~22, crash days), not because the signal
itself is fake (permutation p=0.002/0.036 already show that). This script asks:
can a *risk-management* overlay on TOP of the already-frozen L0 armed-gate /
L2 z60-size ladder reduce kurtosis enough to move the DSR needle, without
touching the signal/gate logic itself?

Hard constraint (causality / no hindsight):
  - L0 armed and L2 size definitions are copied byte-for-byte from
    build_layered_system.py / finalize_cc.py. NOT re-derived, NOT re-fit.
  - Both overlays tested here only use INFORMATION KNOWN AT close(t) (trailing
    realized vol, lagged by construction) to scale position(t) that is then
    held from open(t+1). No overlay parameter was chosen by looking at OOS
    returns or at specific crash dates; thresholds are round, pre-registered-
    style numbers (15% target vol / 1.5x vol-of-vol trigger) of the same kind
    already used in the ALREADY-DOCUMENTED (Stage 8, "vol-scaled variant")
    part of build_layered_system.py -- this is not a new degree of freedom
    invented post-hoc for this task.
  - Return-level winsorization/capping of realized daily P&L (i.e. "cap the
    loss on the exact day it happens") is EXPLICITLY REJECTED here: it is not
    causal, since on day t you cannot know in advance that day t's return
    will land in the extreme percentile before the close happens. Any "cap
    forward-looking realized-return" scheme is a hindsight construct and is
    not tested. See report footer.

Both variants studied are trailing-information EXPOSURE SCALARS multiplied
onto pos_sys(t) = armed(t) * size(t):
  V1 vol-target : scalar = clip(target_vol / trailing_20d_realized_vol, 0, 1)
                  (same rule already in build_layered_system.py, re-verified
                  here on the corrected close-to-close engine + fresh panel)
  V2 shock-brake: scalar = 0.5 if trailing_5d realized |return| range implies
                  vol > VOL_SPIKE_MULT x its own 60d trailing median, else 1.0
                  (a discrete "crash regime" circuit breaker, using only past
                  vol-of-vol, i.e. a vol-of-vol threshold, not a specific
                  return level)

Run: PYTHONPATH=src .venv/bin/python scripts/research/chip_macro/eval_tail_robust_sizing.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[3]
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.csv"
OUT = ROOT / "reports" / "research" / "chip-macro"
ANN = np.sqrt(252)
GAMMA = 0.5772156649
COST_BPS = 4.0

p = pd.read_csv(PANEL).sort_values("date").reset_index(drop=True)
r_cc = p["ix_close"] / p["ix_close"].shift(1) - 1.0
r_oc = p["ix_close"] / p["ix_open"] - 1.0
N = len(p)
OOS = pd.Series(p.index >= int(N * 0.7), index=p.index)


def rz(s, w):
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def strat_cc(pos, cost=COST_BPS):
    """Same executable return engine as finalize_cc.py: open-entry, close-to-close hold."""
    pos = pd.Series(pos, index=p.index).astype(float).fillna(0.0)
    pos_prev = pos.shift(1)
    fresh = (pos_prev > 0) & (pos.shift(2).fillna(0) == 0)
    base = pd.Series(np.where(fresh, r_oc, r_cc), index=p.index)
    return pos_prev * base - pos.diff().abs().fillna(pos.abs()) * cost / 1e4


def m(x, mask=None):
    x = pd.Series(x)
    if mask is not None:
        x = x[mask.reindex(x.index).fillna(False)]
    x = x.dropna()
    if len(x) < 20 or x.std() == 0:
        return dict(shp=np.nan, cagr=np.nan, mdd=np.nan, fin=np.nan, skew=np.nan, kurt=np.nan)
    cum = (1 + x).cumprod()
    return dict(
        shp=x.mean() / x.std() * ANN,
        cagr=cum.iloc[-1] ** (252 / len(x)) - 1,
        mdd=(cum / cum.cummax() - 1).min(),
        fin=cum.iloc[-1],
        skew=x.skew(),
        kurt=x.kurtosis() + 3.0,
    )


def line(name, ret, pos=None):
    f, o = m(ret), m(ret, OOS)
    expo = (pd.Series(pos, index=p.index).fillna(0).astype(bool).mean() * 100) if pos is not None else 100
    print(
        f"  {name:34s} | full Shp {f['shp']:+.2f} CAGR {f['cagr']*100:+5.1f}% MDD {f['mdd']*100:+6.1f}% "
        f"x{f['fin']:.2f} kurt {f['kurt']:5.1f} | OOS Shp {o['shp']:+.2f} CAGR {o['cagr']*100:+5.1f}% "
        f"kurt {o['kurt']:5.1f} | expo {expo:.0f}%"
    )
    return f, o


def dsr(ret, var_trials, Ntr):
    r = pd.Series(ret).dropna().values
    n = len(r)
    sr = r.mean() / r.std()
    g3 = pd.Series(r).skew()
    g4 = pd.Series(r).kurtosis() + 3
    ss = np.sqrt(var_trials) * ((1 - GAMMA) * norm.ppf(1 - 1 / Ntr) + GAMMA * norm.ppf(1 - 1 / (Ntr * np.e)))
    return norm.cdf((sr - ss) * np.sqrt(n - 1) / np.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr**2))


# ---- frozen L0 x L2 signal (copied verbatim from finalize_cc.py / build_layered_system.py) ----
ma150 = p["ix_close"].rolling(150, min_periods=150).mean()
slope_up = (ma150 - ma150.shift(25)) > 0
tsmom = (p["ix_close"] / p["ix_close"].shift(126) - 1) > 0
stage_s2 = (p["ix_close"] > ma150) & slope_up
z60 = rz(p["fut_foreign_oi"], 60)
armed = (stage_s2 & tsmom).astype(float)
size = pd.Series(0.0, index=p.index)
size[(z60 > 0) & (z60 < 1)] = 0.5
size[z60 >= 1] = 1.0
pos_sys = armed * size

print("=== Baseline: SYSTEM L0xL2, corrected close-to-close engine, panel through", p["date"].iloc[-1], "===")
print("real B&H:", end=" ")
line("BUY&HOLD (close-to-close)", r_cc)
base_f, base_o = line("SYSTEM L0xL2 (baseline, frozen)", strat_cc(pos_sys), pos_sys)

# ---- V1: vol-targeting overlay (already-documented rule, re-verified) ----
TARGET_VOL = 0.15
VOL_WIN = 20
rv = r_cc.rolling(VOL_WIN, min_periods=VOL_WIN).std() * ANN
vol_scalar = (TARGET_VOL / rv).clip(0, 1.0).shift(1)  # lag 1: known at close(t) for pos(t)
pos_v1 = pos_sys * vol_scalar.fillna(1.0)

print(f"\n=== V1: vol-target overlay (target {TARGET_VOL:.0%} ann vol, {VOL_WIN}d trailing realized vol, cap 1.0x) ===")
v1_f, v1_o = line("SYSTEM x vol-target", strat_cc(pos_v1), pos_v1)

# ---- V2: shock-brake / vol-of-vol circuit breaker ----
VOL_SPIKE_MULT = 1.5
SHORT_WIN, LONG_WIN = 5, 60
rv_short = r_cc.rolling(SHORT_WIN, min_periods=SHORT_WIN).std()
rv_long_med = rv_short.rolling(LONG_WIN, min_periods=LONG_WIN).median()
shock = (rv_short > VOL_SPIKE_MULT * rv_long_med).shift(1).fillna(False)  # lag 1
brake_scalar = pd.Series(np.where(shock, 0.5, 1.0), index=p.index)
pos_v2 = pos_sys * brake_scalar

print(f"\n=== V2: shock-brake (halve size when trailing {SHORT_WIN}d vol > {VOL_SPIKE_MULT}x its {LONG_WIN}d median) ===")
v2_f, v2_o = line("SYSTEM x shock-brake", strat_cc(pos_v2), pos_v2)

# ---- V3: combined (V1 then V2, multiplicative) ----
pos_v3 = pos_sys * vol_scalar.fillna(1.0) * brake_scalar
print("\n=== V3: combined vol-target + shock-brake ===")
v3_f, v3_o = line("SYSTEM x combined", strat_cc(pos_v3), pos_v3)

# ---- DSR for baseline + each variant, same trial-dispersion / N convention as finalize_cc.py ----
trials = [
    strat_cc((z60 > 0).astype(float)),
    strat_cc(((z60 > 0) & (p["ix_close"] > p["ix_close"].rolling(200, min_periods=200).mean())).astype(float)),
    strat_cc((z60 > 0) & stage_s2 * 1.0),
    strat_cc(stage_s2.astype(float)),
    strat_cc(armed),
    strat_cc(size),
    strat_cc(pos_sys),
]
var_t = np.var([m(t, OOS)["shp"] / ANN for t in trials], ddof=1)
NTRIALS = 160

print(f"\n=== Deflated Sharpe (N={NTRIALS}, same var_trials convention as finalize_cc.py) ===")
for name, ret in [
    ("baseline (frozen)", strat_cc(pos_sys)),
    ("V1 vol-target", strat_cc(pos_v1)),
    ("V2 shock-brake", strat_cc(pos_v2)),
    ("V3 combined", strat_cc(pos_v3)),
]:
    d_full = dsr(ret, var_t, NTRIALS)
    d_oos = dsr(ret[OOS], var_t, NTRIALS)
    print(f"  {name:20s} full DSR={d_full:.3f}  OOS DSR={d_oos:.3f}")

# ---- summary table for the report ----
rows = []
for name, ret, pos in [
    ("BUY&HOLD", r_cc, None),
    ("SYSTEM L0xL2 (baseline)", strat_cc(pos_sys), pos_sys),
    ("V1 vol-target", strat_cc(pos_v1), pos_v1),
    ("V2 shock-brake", strat_cc(pos_v2), pos_v2),
    ("V3 combined", strat_cc(pos_v3), pos_v3),
]:
    f, o = m(ret), m(ret, OOS)
    d_full = dsr(ret, var_t, NTRIALS) if name != "BUY&HOLD" else np.nan
    d_oos = dsr(ret[OOS], var_t, NTRIALS) if name != "BUY&HOLD" else np.nan
    rows.append(
        dict(
            variant=name,
            full_sharpe=f["shp"], full_cagr=f["cagr"], full_mdd=f["mdd"], full_kurt=f["kurt"],
            oos_sharpe=o["shp"], oos_cagr=o["cagr"], oos_kurt=o["kurt"],
            full_dsr=d_full, oos_dsr=d_oos,
        )
    )
outdf = pd.DataFrame(rows)
OUT.mkdir(parents=True, exist_ok=True)
outdf.to_csv(OUT / "tail_robust_sizing_variants.csv", index=False)
print(f"\n-> {OUT/'tail_robust_sizing_variants.csv'}")
