"""Integrated multi-gate system test — converge the project's validated gates.

WHAT THIS IS
  The capstone integration test. We stack the three separately-validated gates
  on top of the leading chip champion and measure whether each successive gate
  ACTUALLY improves the risk-adjusted, deflation-corrected system — not just the
  raw Sharpe. Four long/flat systems on TAIEX, identical convention:

    (A) champion alone        = fut_foreign_oi z60 > 0
    (B) tech-gated champion   = A AND (close > rising MA200)      [known DSR~0.995]
    (C) tech + VIX gated      = B AND (VIX 60d z <= 2)            [risk-off veto]
    (D) + margin-maint veto   = C AND (maintenance_pct >= 150)    [de-lever veto]

  Falsification question: does adding the VIX gate / the margin veto FURTHER
  improve OOS Sharpe / maxDD / Deflated-Sharpe, or are they redundant / harmful?

METHODOLOGY (project spine)
  direction fixed a-priori (all long); 70/30 IS/OOS time split; same-exposure
  permutation; Deflated-Sharpe (Bailey & Lopez de Prado); regime-conditioning;
  collinearity-with-champion check. Falsification-first.

POINT-IN-TIME ALIGNMENT (look-ahead control)
  - champion / tech / margin: domestic, value(t) governs the close(t)->close(t+1)
    hold, under the project's standard one-bar convention (matches every other
    dashboard dimension so the four systems stay comparable).
  - VIX: US-session series -> merge_asof(backward, allow_exact_matches=False) so
    each TW date t only sees the last US VIX strictly before t (matches
    global_macro_study; the p=0.035 spike result was built this way).

DATA (all local, zero external calls at run time)
  data/research/chip_macro/panel.parquet          ix OHLC + fut_foreign_oi
  data/research/dashboard/global_macro_data.parquet   VIX (Yahoo ^VIX real)
  data/research/dashboard/margin_maintenance_data.parquet  FinMind official MR

Not investment advice. Research falsification only.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[3]
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
MACRO = ROOT / "data" / "research" / "dashboard" / "global_macro_data.parquet"
MARGIN = ROOT / "data" / "research" / "dashboard" / "margin_maintenance_data.parquet"
OUTDIR = ROOT / "data" / "research" / "dashboard"
REPORTDIR = ROOT / "reports" / "research" / "dashboard-completeness"

COST_BPS = 4.0
ANN = np.sqrt(252)
GAMMA = 0.5772156649
RNG = np.random.default_rng(7)


def sharpe(x) -> float:
    x = pd.Series(x).dropna()
    return x.mean() / x.std() * ANN if len(x) > 20 and x.std() > 0 else np.nan


def lf(pos, fwd, cost=COST_BPS) -> pd.Series:
    pos = pd.Series(pos, index=fwd.index).astype(float).fillna(0.0)
    return pos * fwd - pos.diff().abs().fillna(pos.abs()) * (cost / 1e4)


def maxdd(x) -> float:
    e = (1 + pd.Series(x).fillna(0)).cumprod()
    return float((e / e.cummax() - 1).min())


def perm_p_same_exposure(pos, fwd, mask, n=3000) -> tuple[float, float]:
    fr = fwd[mask].reset_index(drop=True)
    ps = pd.Series(pos, index=fwd.index)[mask].astype(float).reset_index(drop=True)
    ok = fr.notna()
    fr, ps = fr[ok], ps[ok]
    actual = sharpe(ps.values * fr.values)
    k, N = int(round(ps.sum())), len(ps)
    if k == 0 or k >= N:
        return actual, np.nan
    frv = fr.values
    null = np.empty(n)
    for i in range(n):
        idx = RNG.choice(N, k, replace=False)
        rp = np.zeros(N)
        rp[idx] = 1.0
        null[i] = sharpe(rp * frv)
    return actual, float((null >= actual).mean())


def deflated_sharpe(ret: pd.Series, n_trials: int) -> dict:
    r = pd.Series(ret).dropna().values
    n = len(r)
    if n < 30 or r.std() == 0:
        return {"ann": np.nan, "psr0": np.nan, "dsr": np.nan}
    sr = r.mean() / r.std()
    g3 = pd.Series(r).skew()
    g4 = pd.Series(r).kurtosis() + 3.0
    var_trials = 1.0 / n
    sr_star = np.sqrt(var_trials) * (
        (1 - GAMMA) * norm.ppf(1 - 1.0 / n_trials)
        + GAMMA * norm.ppf(1 - 1.0 / (n_trials * np.e))
    )

    def psr(s):
        denom = np.sqrt(1 - g3 * sr + (g4 - 1) / 4.0 * sr**2)
        return float(norm.cdf((sr - s) * np.sqrt(n - 1) / denom))

    return {"ann": sr * ANN, "psr0": psr(0.0), "dsr": psr(sr_star),
            "sr_star_ann": sr_star * ANN}


def build() -> pd.DataFrame:
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    p["date"] = pd.to_datetime(p["date"])

    # champion: fut_foreign_oi z60 > 0
    foi = p["fut_foreign_oi"]
    p["champ_z60"] = (foi - foi.rolling(60).mean()) / foi.rolling(60).std()
    p["champ"] = (p["champ_z60"] > 0).astype(float)

    # tech gate: close > rising MA200  (Weinstein Stage-2 proxy / "上彎MA200")
    c = p["ix_close"]
    ma200 = c.rolling(200, min_periods=200).mean()
    p["ma200"] = ma200
    p["tech_ok"] = ((c > ma200) & (ma200.diff(22) > 0)).astype(float)
    p["close_gt_ma200"] = (c > ma200).astype(float)  # documented DSR0.995 variant

    # --- VIX gate (point-in-time via merge_asof backward, no exact match) ---
    vix = pd.read_parquet(MACRO)[["date", "VIX"]].dropna().copy()
    vix["date"] = pd.to_datetime(vix["date"])
    vix = vix.sort_values("date").reset_index(drop=True)
    vix["vix_z60"] = (vix["VIX"] - vix["VIX"].rolling(60).mean()) / vix["VIX"].rolling(60).std()
    p = pd.merge_asof(p.sort_values("date"), vix[["date", "vix_z60"]].sort_values("date"),
                      on="date", direction="backward", allow_exact_matches=False)
    p["vix_ok"] = (p["vix_z60"] <= 2.0).astype(float)   # veto when spike z>2

    # --- margin maintenance veto (domestic, same-index convention) ---
    mm = pd.read_parquet(MARGIN).copy()
    mm["date"] = pd.to_datetime(mm["date"])
    p = p.merge(mm, on="date", how="left")
    p["maintenance_pct"] = p["maintenance_pct"].ffill()
    p["margin_ok"] = (p["maintenance_pct"] >= 150.0).astype(float)

    p["fwd"] = c.pct_change().shift(-1)
    return p


def main():
    p = build()
    n = len(p)
    is_cut = int(n * 0.7)
    oos = pd.Series(p.index >= is_cut, index=p.index)
    ismask = ~oos
    fwd = p["fwd"]
    lines = []

    def out(s=""):
        print(s)
        lines.append(s)

    out(f"panel {p['date'].min().date()} -> {p['date'].max().date()}  n={n}  "
        f"IS<{p['date'].iloc[is_cut].date()}  OOS n={int(oos.sum())}")
    out(f"buy&hold  IS Sharpe={sharpe(fwd[ismask]):+.2f}  OOS Sharpe={sharpe(fwd[oos]):+.2f}")
    # gate coverage / trigger counts (OOS)
    for g in ["vix_ok", "margin_ok"]:
        veto_oos = int((p[g][oos] == 0).sum())
        out(f"  gate[{g}] OOS veto-days={veto_oos} / {int(oos.sum())} "
            f"({100*veto_oos/int(oos.sum()):.1f}%)")
    out(f"  VIX z60 coverage: {p['vix_z60'].notna().sum()}/{n}   "
        f"maintenance coverage: {p['maintenance_pct'].notna().sum()}/{n}")
    out("")

    champ = p["champ"]
    tech = p["tech_ok"].astype(bool)
    vix_ok = p["vix_ok"].astype(bool)
    margin_ok = p["margin_ok"].astype(bool)

    systems = {
        "A_champ_alone":      champ.astype(float),
        "B_tech_gated":       (champ.astype(bool) & tech).astype(float),
        "C_tech_vix_gated":   (champ.astype(bool) & tech & vix_ok).astype(float),
        "D_plus_margin_veto": (champ.astype(bool) & tech & vix_ok & margin_ok).astype(float),
    }

    # n_trials for DSR: 8 to reproduce the documented tech_regime B~0.995,
    # plus a conservative 16 that accounts for this integration's extra combos.
    N_TRIALS_MAIN = 8
    N_TRIALS_CONS = 16

    rows = []
    out("=== FOUR-SYSTEM COMPARISON (long/flat, OOS) ===")
    for name, pos in systems.items():
        r = lf(pos, fwd)
        r_oos = r[oos]
        a, pv = perm_p_same_exposure(pos, fwd, oos)
        d = deflated_sharpe(r_oos, N_TRIALS_MAIN)
        dc = deflated_sharpe(r_oos, N_TRIALS_CONS)
        ic_is = pd.DataFrame({"v": pos, "f": fwd})[ismask].dropna().corr().iloc[0, 1]
        rows.append({
            "system": name,
            "exposure_oos": float(pos[oos].mean()),
            "IC_IS": ic_is,
            "IS_Sharpe": sharpe(r[ismask]),
            "OOS_Sharpe": sharpe(r_oos),
            "OOS_maxDD": maxdd(r_oos),
            "OOS_perm_p": pv,
            "DSR_nt8": d["dsr"],
            "DSR_nt16": dc["dsr"],
            "ann_SR": d["ann"],
        })
    res = pd.DataFrame(rows)
    pd.set_option("display.float_format", lambda x: f"{x:+.3f}" if isinstance(x, float) else str(x))
    out(res.to_string(index=False))
    out("")

    # incremental deltas
    out("=== INCREMENTAL EFFECT (does each added gate help?) ===")
    def g(nm, col):
        return float(res.loc[res.system == nm, col].iloc[0])
    for frm, to, label in [("A_champ_alone", "B_tech_gated", "+tech"),
                           ("B_tech_gated", "C_tech_vix_gated", "+VIX gate"),
                           ("C_tech_vix_gated", "D_plus_margin_veto", "+margin veto")]:
        out(f"  {label:14s}: dOOS_Sharpe={g(to,'OOS_Sharpe')-g(frm,'OOS_Sharpe'):+.3f}  "
            f"dmaxDD={g(to,'OOS_maxDD')-g(frm,'OOS_maxDD'):+.3f}  "
            f"dDSR(nt8)={g(to,'DSR_nt8')-g(frm,'DSR_nt8'):+.3f}  "
            f"exposure {g(frm,'exposure_oos'):.2f}->{g(to,'exposure_oos'):.2f}")
    out("")

    # regime-conditioning of the final system vs champion
    out("=== REGIME-CONDITIONING (champion edge is bull-only; gates cut bear tail) ===")
    champ_ret = lf(champ, fwd)
    bull = (p["ix_close"] > p["ma200"]).astype(bool)
    for label, m in [("ALL", pd.Series(True, index=p.index)),
                     ("bull(>MA200)", bull), ("bear(<=MA200)", ~bull)]:
        mm2 = m & oos
        out(f"  champion OOS Sharpe [{label:14s}] = {sharpe(champ_ret[mm2]):+.2f}  "
            f"(days={int(mm2.sum())})")
    out("")

    # collinearity of the added gates with the champion (redundancy check)
    out("=== COLLINEARITY of added gates with champion (redundancy) ===")
    for gname in ["tech_ok", "vix_ok", "margin_ok"]:
        cc = pd.DataFrame({"g": p[gname], "champ": p["champ_z60"]}).dropna().corr().iloc[0, 1]
        out(f"  corr({gname}, champion z60) = {cc:+.3f}")
    # collinearity of gates with 60d price momentum
    mom = p["ix_close"].pct_change(60)
    for gname in ["tech_ok", "vix_ok", "margin_ok"]:
        cc = pd.DataFrame({"g": p[gname], "m": mom}).dropna().corr().iloc[0, 1]
        out(f"  corr({gname}, 60d price momentum) = {cc:+.3f}")
    out("")

    # persist per-day system positions + returns for the artifact/parquet
    keep = p[["date", "ix_close", "champ_z60", "champ", "tech_ok", "close_gt_ma200",
              "vix_z60", "vix_ok", "maintenance_pct", "margin_ok", "fwd"]].copy()
    for name, pos in systems.items():
        keep[f"pos_{name}"] = pos.values
        keep[f"ret_{name}"] = lf(pos, fwd).values
    keep["oos"] = oos.values
    OUTDIR.mkdir(parents=True, exist_ok=True)
    keep.to_parquet(OUTDIR / "integrated_multigate.parquet", index=False)
    res.to_csv(REPORTDIR / "integrated_multigate_metrics.csv", index=False)
    out(f"-> {OUTDIR/'integrated_multigate.parquet'}")
    out(f"-> {REPORTDIR/'integrated_multigate_metrics.csv'}")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
