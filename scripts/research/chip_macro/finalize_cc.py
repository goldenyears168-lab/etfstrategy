"""FINALIZE Stage 6/7/8 on the corrected close-to-close (overnight-held) convention.

Return engine (executable, no look-ahead):
  target position pos(t) decided at close t (chip/regime info known post-close).
  Held from OPEN t+1. On the first day of a holding spell -> open(t+1)->close(t+1)
  (r_oc); on subsequent held days -> close->close (r_cc). Flat days = 0. Cost on
  target-position turnover. This forfeits the first overnight (honest; data publishes
  after close so you can only act at the next open). Benchmark = real B&H = r_cc held.

Prints one canonical table per stage. Also re-runs Deflated Sharpe on the Stage-8 system.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[3]
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
ANN = np.sqrt(252)
GAMMA = 0.5772156649
COST_BPS = 4.0

p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
r_cc = p["ix_close"] / p["ix_close"].shift(1) - 1.0
r_oc = p["ix_close"] / p["ix_open"] - 1.0
N = len(p)
OOS = pd.Series(p.index >= int(N * 0.7), index=p.index)


def rz(s, w):
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def strat_cc(pos, cost=COST_BPS):
    """Corrected return: open-entry then hold overnight."""
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
        return dict(shp=np.nan, cagr=np.nan, mdd=np.nan, fin=np.nan)
    cum = (1 + x).cumprod()
    return dict(shp=x.mean() / x.std() * ANN, cagr=cum.iloc[-1] ** (252 / len(x)) - 1,
                mdd=(cum / cum.cummax() - 1).min(), fin=cum.iloc[-1])


def line(name, ret, pos=None):
    f, o = m(ret), m(ret, OOS)
    expo = (pd.Series(pos, index=p.index).fillna(0).astype(bool).mean() * 100) if pos is not None else 100
    print(f"  {name:34s} | full Shp {f['shp']:+.2f} CAGR {f['cagr']*100:+5.1f}% MDD {f['mdd']*100:+6.1f}% x{f['fin']:.2f}"
          f" | OOS Shp {o['shp']:+.2f} CAGR {o['cagr']*100:+5.1f}% | expo {expo:.0f}%")


# shared signals
ma150 = p["ix_close"].rolling(150, min_periods=150).mean()
ma200 = p["ix_close"].rolling(200, min_periods=200).mean()
z60 = rz(p["fut_foreign_oi"], 60)
chip = z60 > 0
bull200 = p["ix_close"] > ma200
slope_up = (ma150 - ma150.shift(25)) > 0
tsmom = (p["ix_close"] / p["ix_close"].shift(126) - 1) > 0
stage_s2 = (p["ix_close"] > ma150) & slope_up
vol_expand = p["ix_money"] > p["ix_money"].rolling(50, min_periods=50).mean()

print("real B&H:", end=" "); line("BUY&HOLD (close-to-close)", r_cc)

print("\n===== STAGE 6 (regime split) — corrected =====")
line("chip alone (z60>0)", strat_cc(chip.astype(float)), chip)
line("chip x bull(>MA200)", strat_cc((chip & bull200).astype(float)), chip & bull200)
line("chip ONLY in bear", strat_cc((chip & ~bull200).astype(float)), chip & ~bull200)
line("bull(>MA200) only", strat_cc(bull200.astype(float)), bull200)

print("\n===== STAGE 7 (Weinstein) — corrected =====")
print("  chip Sharpe by stage (full):")
ma150_slopeneg = ~slope_up
stage = pd.Series("?", index=p.index)
stage[(p["ix_close"] > ma150) & slope_up] = "S2_adv"
stage[(p["ix_close"] > ma150) & ~slope_up] = "S3_top"
stage[(p["ix_close"] <= ma150) & ~slope_up] = "S4_dec"
stage[(p["ix_close"] <= ma150) & slope_up] = "S1_base"
cr = strat_cc(chip.astype(float))
for s in ["S1_base", "S2_adv", "S3_top", "S4_dec"]:
    mask = (stage == s)
    print(f"    {s:8s}: chip Shp {m(cr, mask)['shp']:+.2f}  ({mask.mean()*100:.0f}% of time)")
line("chip x Stage2", strat_cc((chip & stage_s2).astype(float)), chip & stage_s2)
line("chip x Stage2 x vol-expand", strat_cc((chip & stage_s2 & vol_expand).astype(float)), chip & stage_s2 & vol_expand)
line("Stage2 only", strat_cc(stage_s2.astype(float)), stage_s2)

print("\n===== STAGE 8 (L0xL2 a-priori system) — corrected =====")
armed = (stage_s2 & tsmom).astype(float)
size = pd.Series(0.0, index=p.index)
size[(z60 > 0) & (z60 < 1)] = 0.5
size[z60 >= 1] = 1.0
pos_sys = armed * size
sys_ret = strat_cc(pos_sys)
line("SYSTEM L0xL2", sys_ret, pos_sys)
line("L0 regime only", strat_cc(armed), armed)
line("L2 chip-size only", strat_cc(size), size)

print("\n  per-year Sharpe: SYSTEM vs B&H (corrected)")
yr = p["date"].str[:4]
cmp = pd.DataFrame({"yr": yr, "s": sys_ret, "b": r_cc}).dropna()
for y, g in cmp.groupby("yr"):
    ss = g["s"].mean()/g["s"].std()*ANN if g["s"].std() > 0 else np.nan
    sb = g["b"].mean()/g["b"].std()*ANN if g["b"].std() > 0 else np.nan
    print(f"    {y}: system {ss:+.2f}  B&H {sb:+.2f}")

# DSR on corrected system
def dsr(ret, var_trials, Ntr):
    r = pd.Series(ret).dropna().values
    n = len(r); sr = r.mean()/r.std()
    g3 = pd.Series(r).skew(); g4 = pd.Series(r).kurtosis()+3
    ss = np.sqrt(var_trials)*((1-GAMMA)*norm.ppf(1-1/Ntr)+GAMMA*norm.ppf(1-1/(Ntr*np.e)))
    return norm.cdf((sr-ss)*np.sqrt(n-1)/np.sqrt(1-g3*sr+(g4-1)/4*sr**2))
trials = [strat_cc(x) for x in [chip.astype(float), (chip&bull200).astype(float),
          (chip&stage_s2).astype(float), stage_s2.astype(float), armed, size, pos_sys]]
var_t = np.var([m(t, OOS)['shp']/ANN for t in trials], ddof=1)
print(f"\n  Deflated Sharpe (N=160): full {dsr(sys_ret, var_t, 160):.3f}  OOS {dsr(sys_ret[OOS], var_t, 160):.3f}")
