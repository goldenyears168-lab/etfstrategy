"""L0+L2 a-priori layered system — thresholds FROZEN from theory, tested ONCE.

L0 regime gate (armed=1 / disarmed=0), all known at close t:
    armed = (close > MA150) AND (MA150 rising over 25d) AND (126d TSMOM > 0)
    (= Weinstein Stage-2 rebuilt from validated parts: MA-slope + time-series momentum)
L2 conviction size from foreign-futures positioning z60:
    z60 <= 0 -> 0.0 (flat) ; 0 < z60 < 1 -> 0.5 (half) ; z60 >= 1 -> 1.0 (full)
Position = L0_armed * L2_size, applied to open(t+1)->close(t+1). Costs on turnover.

Reported ONCE against B&H / L0-only / L2-only, with per-year, Deflated Sharpe, and a
documented vol-scaled variant. NO parameter is optimized on the data here; the only
values reused from earlier exploration are z60's window(60) and the >0 cut (flagged).
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


def sharpe(x):
    x = pd.Series(x).dropna()
    return x.mean() / x.std() * ANN if len(x) > 20 and x.std() > 0 else np.nan


def metrics(x):
    x = pd.Series(x).dropna()
    if len(x) < 20 or x.std() == 0:
        return dict(sharpe=np.nan, cagr=np.nan, maxdd=np.nan, vol=np.nan)
    cum = (1 + x).cumprod()
    return dict(
        sharpe=x.mean() / x.std() * ANN,
        cagr=cum.iloc[-1] ** (252 / len(x)) - 1,
        maxdd=(cum / cum.cummax() - 1).min(),
        vol=x.std() * ANN,
    )


def strat_ret(pos, day_ret, cost=COST_BPS):
    pos = pd.Series(pos, index=day_ret.index).astype(float).fillna(0.0)
    return (pos * day_ret - pos.diff().abs().fillna(pos.abs()) * cost / 1e4)


def dsr(ret, var_trials, N):
    r = pd.Series(ret).dropna().values
    n = len(r); sr = r.mean() / r.std()
    g3 = pd.Series(r).skew(); g4 = pd.Series(r).kurtosis() + 3
    sr_star = np.sqrt(var_trials) * ((1-GAMMA)*norm.ppf(1-1/N) + GAMMA*norm.ppf(1-1/(N*np.e)))
    return norm.cdf((sr - sr_star)*np.sqrt(n-1)/np.sqrt(1 - g3*sr + (g4-1)/4*sr**2))


def main():
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    day_ret = (p["ix_close"] / p["ix_open"] - 1.0).shift(-1)
    n = len(p); oos = pd.Series(p.index >= int(n * 0.7), index=p.index)

    # ---- L0 regime gate (frozen) ----
    ma150 = p["ix_close"].rolling(150, min_periods=150).mean()
    slope_up = (ma150 - ma150.shift(25)) > 0
    tsmom = (p["ix_close"] / p["ix_close"].shift(126) - 1.0) > 0
    armed = ((p["ix_close"] > ma150) & slope_up & tsmom).astype(float)

    # ---- L2 conviction size (frozen ladder) ----
    z60 = (p["fut_foreign_oi"] - p["fut_foreign_oi"].rolling(60, min_periods=60).mean()) \
        / p["fut_foreign_oi"].rolling(60, min_periods=60).std()
    size = pd.Series(0.0, index=p.index)
    size[(z60 > 0) & (z60 < 1)] = 0.5
    size[z60 >= 1] = 1.0

    # ---- positions ----
    pos_system = armed * size
    pos_l0 = armed                      # regime only, full size when armed
    pos_l2 = size                       # chip size, no regime gate
    bh = day_ret

    strategies = {
        "SYSTEM L0xL2":   strat_ret(pos_system, day_ret),
        "L0 regime only": strat_ret(pos_l0, day_ret),
        "L2 chip only":   strat_ret(pos_l2, day_ret),
        "BUY&HOLD":       bh,
    }

    print("=== a-priori layered system — tested once ===")
    print(f"{'strategy':16s} {'seg':4s} {'Sharpe':>7s} {'CAGR':>7s} {'MaxDD':>7s} {'expo%':>6s}")
    for name, r in strategies.items():
        pos = {"SYSTEM L0xL2": pos_system, "L0 regime only": pos_l0, "L2 chip only": pos_l2}.get(name)
        expo = pos.mean() if pos is not None else 1.0
        for seg, mask in (("full", pd.Series(True, index=p.index)), ("OOS", oos)):
            m = metrics(r[mask.reindex(r.index).fillna(False)])
            print(f"{name:16s} {seg:4s} {m['sharpe']:+7.2f} {m['cagr']*100:+6.1f}% {m['maxdd']*100:+6.1f}% "
                  f"{expo*100:5.0f}" if seg == 'full' else
                  f"{'':16s} {seg:4s} {m['sharpe']:+7.2f} {m['cagr']*100:+6.1f}% {m['maxdd']*100:+6.1f}%")

    # ---- per-year (system vs B&H) ----
    print("\n=== per-year Sharpe: SYSTEM vs B&H ===")
    yr = p["date"].str[:4]
    sysr = strategies["SYSTEM L0xL2"]
    cmp = pd.DataFrame({"yr": yr, "s": sysr, "b": bh}).dropna()
    for y, g in cmp.groupby("yr"):
        print(f"  {y}: system={sharpe(g['s']):+.2f}  B&H={sharpe(g['b']):+.2f}  n={len(g)}")

    # ---- vol-scaled variant (documented) ----
    rv = day_ret.rolling(20, min_periods=20).std() * ANN
    volscale = (0.15 / rv).clip(0, 1.0)
    pos_vs = (armed * size * volscale.shift(1))  # use lagged vol (known at t)
    r_vs = strat_ret(pos_vs, day_ret)
    print("\n=== vol-scaled variant (15% target, cap 1.0) ===")
    for seg, mask in (("full", pd.Series(True, index=p.index)), ("OOS", oos)):
        m = metrics(r_vs[mask.reindex(r_vs.index).fillna(False)])
        print(f"  {seg}: Sharpe={m['sharpe']:+.2f} CAGR={m['cagr']*100:+.1f}% MaxDD={m['maxdd']*100:+.1f}%")

    # ---- Deflated Sharpe on the SYSTEM ----
    var_trials = np.var([sharpe(r[oos.reindex(r.index).fillna(False)]) / ANN
                         for r in strategies.values()], ddof=1)
    N = 160  # cumulative trials across all stages (conservative)
    d_full = dsr(sysr, var_trials, N)
    d_oos = dsr(sysr[oos.reindex(sysr.index).fillna(False)], var_trials, N)
    print(f"\n=== Deflated Sharpe (N={N}) ===")
    print(f"  SYSTEM full DSR={d_full:.3f}  OOS DSR={d_oos:.3f}  -> {'SURVIVES' if d_oos>0.95 else 'FAILS'} 5%")

    # ---- current state ----
    last = p.index[-1]
    print(f"\n=== current ({p.loc[last,'date']}) ===")
    print(f"  L0 armed={bool(armed.iloc[-1])} (close>{ma150.iloc[-1]:,.0f}MA150={p.loc[last,'ix_close']>ma150.iloc[-1]}, "
          f"slope_up={bool(slope_up.iloc[-1])}, 6m_tsmom={bool(tsmom.iloc[-1])})")
    print(f"  L2 z60={z60.iloc[-1]:+.2f} -> size={size.iloc[-1]}")
    print(f"  => SYSTEM position = {pos_system.iloc[-1]:.1f}")

    OUT.mkdir(parents=True, exist_ok=True)
    outdf = pd.DataFrame({
        "date": p["date"], "ix_close": p["ix_close"], "armed": armed, "z60": z60,
        "size": size, "position": pos_system,
    })
    outdf.to_csv(OUT / "layered_system_daily.csv", index=False)
    print(f"\n-> {OUT/'layered_system_daily.csv'}")


if __name__ == "__main__":
    main()
