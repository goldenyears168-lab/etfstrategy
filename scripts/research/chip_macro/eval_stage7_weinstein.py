"""Stage-7: Weinstein 4-stage regime as a better filter for the foreign-futures chip signal.

Weinstein Stage Analysis on the INDEX (daily proxy of his weekly method):
  MA30w  ~ 150 trading days;  slope over ~5 weeks (25d).
  Stage 2 (advancing): close > MA150 AND MA150 rising
  Stage 3 (topping)  : close > MA150 AND MA150 falling
  Stage 4 (declining): close < MA150 AND MA150 falling
  Stage 1 (basing)   : close < MA150 AND MA150 rising
Volume confirmation: ix_money above its own 50d MA (Weinstein wants volume expansion in Stage 2).

Hypothesis (from our Stage-6 result + Weinstein + herding-regime literature):
  the foreign-futures chip signal (z60>0) has edge specifically in Stage 2, and
  Stage-2 is a cleaner regime gate than the raw close>MA200 rule.

No look-ahead: MA150/slope/volume all use data up to and including close t; chip known
after close t; position applied to open(t+1)->close(t+1). Any winner -> Deflated Sharpe.
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


def rz(s, w):
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def sharpe(x):
    x = pd.Series(x).dropna()
    return x.mean() / x.std() * ANN if len(x) > 20 and x.std() > 0 else np.nan


def lf(pos, day_ret, cost=COST_BPS):
    pos = pd.Series(pos, index=day_ret.index).astype(float).fillna(0.0)
    return (pos * day_ret - pos.diff().abs().fillna(pos.abs()) * cost / 1e4).dropna()


def dsr(ret, var_trials, N):
    r = pd.Series(ret).dropna().values
    n = len(r); sr = r.mean() / r.std()
    g3 = pd.Series(r).skew(); g4 = pd.Series(r).kurtosis() + 3
    sr_star = np.sqrt(var_trials) * ((1-GAMMA)*norm.ppf(1-1/N) + GAMMA*norm.ppf(1-1/(N*np.e)))
    return norm.cdf((sr - sr_star)*np.sqrt(n-1)/np.sqrt(1 - g3*sr + (g4-1)/4*sr**2)), sr*ANN


def main():
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    day_ret = (p["ix_close"] / p["ix_open"] - 1.0).shift(-1)
    n = len(p); oos = pd.Series(p.index >= int(n * 0.7), index=p.index)

    # --- Weinstein stage ---
    ma150 = p["ix_close"].rolling(150, min_periods=150).mean()
    slope = ma150 - ma150.shift(25)
    above = p["ix_close"] > ma150
    rising = slope > 0
    stage = pd.Series(index=p.index, dtype="object")
    stage[above & rising] = "S2_advancing"
    stage[above & ~rising] = "S3_topping"
    stage[~above & ~rising] = "S4_declining"
    stage[~above & rising] = "S1_basing"
    vol_expand = p["ix_money"] > p["ix_money"].rolling(50, min_periods=50).mean()

    z60 = rz(p["fut_foreign_oi"], 60)
    chip = z60 > 0
    ma200 = p["ix_close"] > p["ix_close"].rolling(200, min_periods=200).mean()

    # --- chip Sharpe within each stage (does chip edge concentrate in S2?) ---
    print("=== chip (z60>0) forward daily return by Weinstein stage ===")
    champ = lf(chip.astype(float), day_ret)
    st = stage.reindex(champ.index)
    for s in ["S1_basing", "S2_advancing", "S3_topping", "S4_declining"]:
        m = st == s
        print(f"  {s:14s}: chip Sharpe={sharpe(champ[m]):+.2f}  n_days_in_stage={int((stage==s).sum())}  "
              f"time%={(stage==s).mean()*100:.0f}")

    # --- regime-gate comparison ---
    print("\n=== regime gate comparison for chip signal (OOS / full Sharpe) ===")
    gates = {
        "chip alone (no gate)":            chip.astype(float),
        "chip x close>MA200 (Stage-6 win)": (chip & ma200).astype(float),
        "chip x Stage2":                    (chip & (stage == "S2_advancing")).astype(float),
        "chip x (Stage2 or Stage1)":        (chip & stage.isin(["S2_advancing", "S1_basing"])).astype(float),
        "chip x Stage2 x vol-expand":       (chip & (stage == "S2_advancing") & vol_expand).astype(float),
        "Stage2 only (no chip)":            (stage == "S2_advancing").astype(float),
        "close>MA200 only (no chip)":       ma200.astype(float),
    }
    rows = []
    for name, pos in gates.items():
        r = lf(pos, day_ret)
        s_oos = sharpe(r[oos.reindex(r.index).fillna(False)])
        s_full = sharpe(r)
        expo = pd.Series(pos, index=p.index).fillna(0).astype(bool).mean()
        rows.append((name, s_oos, s_full, expo))
        print(f"  {name:34s} OOS={s_oos:+.2f}  full={s_full:+.2f}  expo={expo*100:.0f}%")

    # --- does chip ADD over the best pure-stage gate? (incremental value) ---
    print("\n=== incremental value of chip within Stage2 ===")
    s2 = stage == "S2_advancing"
    print(f"  Stage2 only          OOS={sharpe(lf(s2.astype(float), day_ret)[oos.reindex(day_ret.index).fillna(False)]):+.2f}")
    print(f"  Stage2 AND chip      OOS={sharpe(lf((s2&chip).astype(float), day_ret)[oos.reindex(day_ret.index).fillna(False)]):+.2f}")
    print(f"  Stage2 AND chip-veto-off (chip<0 flat) same as above")

    # --- DSR on best gate ---
    print("\n=== Deflated Sharpe on best gate (counting all trials to date) ===")
    srs = []
    for _, pos in gates.items():
        r = lf(pos, day_ret); ro = r[oos.reindex(r.index).fillna(False)]
        if len(ro) > 20 and ro.std() > 0:
            srs.append(ro.mean()/ro.std())
    var_trials = np.var(srs, ddof=1)
    N = 146 + len(gates)  # cumulative search
    best_name = max(rows, key=lambda t: (t[1] if pd.notna(t[1]) else -9))[0]
    best_pos = gates[best_name]
    r_full = lf(best_pos, day_ret)
    r_oos = r_full[oos.reindex(r_full.index).fillna(False)]
    d_full, a_full = dsr(r_full, var_trials, N)
    d_oos, a_oos = dsr(r_oos, var_trials, N)
    print(f"  best gate: {best_name}")
    print(f"  N={N}  full annSharpe={a_full:+.2f} DSR={d_full:.3f} | OOS annSharpe={a_oos:+.2f} DSR={d_oos:.3f}"
          f"  -> {'SURVIVES' if d_oos>0.95 else 'FAILS'}")

    # --- current state ---
    last = p.iloc[-1]
    print(f"\n=== current ({last['date']}) ===")
    print(f"  stage={stage.iloc[-1]}  close={last['ix_close']:,.0f} MA150={ma150.iloc[-1]:,.0f} "
          f"slope25d={slope.iloc[-1]:+,.0f}  chip z60={z60.iloc[-1]:+.2f}  vol_expand={bool(vol_expand.iloc[-1])}")


if __name__ == "__main__":
    main()
