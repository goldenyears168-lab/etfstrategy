"""Tech-regime (trend / MA / Weinstein-stage / TSMOM) as the L0 gate layer.

WHAT THIS IS
  The "technical-trend / moving-average / Weinstein-stage / regime" dimension,
  operationalised the way this project's LAYERED_DESIGN prescribes: NOT as a
  standalone alpha, but as the L0 *regime gate* that decides WHEN the validated
  leading chip factor (外資台指期 positioning, fut_foreign_oi_z60) is allowed to
  fire. We falsify three claims, in this order:
    (1) Do coarse trend gates (close>MA200, MA200-slope, 12M TSMOM, vol-scaling)
        have any standalone OOS edge over buy&hold?  (expected: weak / none)
    (2) Is the champion's edge concentrated in the bull regime?  (expected: yes)
    (3) Does gating the champion by regime IMPROVE risk-adjusted OOS return
        (the real L0 value: cut the bear-regime tail, not add alpha)?

  Methodology follows the project spine: direction fixed from IN-SAMPLE IC only,
  70/30 IS/OOS time split, same-exposure permutation, Deflated-Sharpe (Bailey &
  López de Prado), regime-conditioning, and an explicit collinearity-with-price
  check (these signals ARE price momentum by construction).

DATA SOURCE (all LOCAL — implementable now, zero external calls)
  data/research/chip_macro/panel.parquet
    ix_open/ix_high/ix_low/ix_close = TAIEX daily OHLC (2018-06 -> 2026-07)
    fut_foreign_oi = 外資台指期淨未平倉 (champion raw value)
  Individual-stock RS extension reads data/stocks.db (stock_daily_bars.adj_close
  + daily_bars IX0001) but is fenced behind --with-rs and flagged for
  survivorship bias (only currently-listed names) — treat as EXPLORATORY.

HOW TO RUN
  cd /Users/jackm4/goldenstocks && source .venv/bin/activate
  python scripts/research/dashboard/tech_regime_study.py            # index L0 study
  python scripts/research/dashboard/tech_regime_study.py --with-rs  # + stock RS demo

LEAD/LAG + LAYER
  Classification: 同步→落後 as a *signal* (MA/stage confirm turns AFTER they
  happen); as a *function* it is the L0 gate. NOT a precursor. It pairs with the
  champion (領先) by FILTERING it — Stage-4/below-MA200 must veto re-arming longs.

Not investment advice. Research falsification only.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[3]
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
DB = ROOT / "data" / "stocks.db"
OUT = ROOT / "reports" / "research" / "dashboard-completeness"

COST_BPS = 4.0          # per-side, matches chip-macro convention
ANN = np.sqrt(252)
GAMMA = 0.5772156649    # Euler-Mascheroni, for expected-max-SR
RNG = np.random.default_rng(7)


# ----------------------------------------------------------------------------
# primitives (aligned with scripts/research/chip_macro/*)
# ----------------------------------------------------------------------------
def sharpe(x) -> float:
    x = pd.Series(x).dropna()
    return x.mean() / x.std() * ANN if len(x) > 20 and x.std() > 0 else np.nan


def lf(pos, fwd, cost=COST_BPS) -> pd.Series:
    """Long/flat P&L: continuous position in [0,1], turnover-costed."""
    pos = pd.Series(pos, index=fwd.index).astype(float).fillna(0.0)
    return pos * fwd - pos.diff().abs().fillna(pos.abs()) * (cost / 1e4)


def perm_p_same_exposure(pos, fwd, mask, n=2000) -> tuple[float, float]:
    """Permutation vs random days holding the SAME number of long days (OOS)."""
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
    """Bailey & López de Prado DSR: PSR against expected-max-SR of n_trials nulls."""
    r = pd.Series(ret).dropna().values
    n = len(r)
    if n < 30 or r.std() == 0:
        return {"daily_sr": np.nan, "ann": np.nan, "psr0": np.nan, "dsr": np.nan}
    sr = r.mean() / r.std()
    g3 = pd.Series(r).skew()
    g4 = pd.Series(r).kurtosis() + 3.0
    # variance of SR across trials ~ 1/n (null i.i.d.); use conservative 1/n
    var_trials = 1.0 / n
    sr_star = np.sqrt(var_trials) * (
        (1 - GAMMA) * norm.ppf(1 - 1.0 / n_trials)
        + GAMMA * norm.ppf(1 - 1.0 / (n_trials * np.e))
    )

    def psr(sr_star_):
        denom = np.sqrt(1 - g3 * sr + (g4 - 1) / 4.0 * sr**2)
        return float(norm.cdf((sr - sr_star_) * np.sqrt(n - 1) / denom))

    return {
        "daily_sr": sr, "ann": sr * ANN,
        "psr0": psr(0.0), "dsr": psr(sr_star), "sr_star_ann": sr_star * ANN,
    }


# ----------------------------------------------------------------------------
# signal construction (L0 regime candidates)
# ----------------------------------------------------------------------------
def build_signals(p: pd.DataFrame) -> dict[str, pd.Series]:
    """All computed on close(t); acted on t+1 (fwd is next-day close->close)."""
    c = p["ix_close"]
    ma150 = c.rolling(150, min_periods=150).mean()
    ma200 = c.rolling(200, min_periods=200).mean()
    slope200 = ma200.diff(22)                      # 200d MA rising over ~1 month
    ret252 = c.pct_change(252)                     # 12M TSMOM raw return
    ret126 = c.pct_change(126)                     # 6M TSMOM
    realvol = c.pct_change().rolling(20).std()     # 20d realized vol

    sig = {
        # binary long/flat gates (position in {0,1})
        "close_gt_ma200":       (c > ma200).astype(float),
        "close_gt_ma150":       (c > ma150).astype(float),
        "ma200_slope_up":       (slope200 > 0).astype(float),
        # Weinstein Stage-2 proxy: above rising MA200 (LAYERED_DESIGN: ~= MA200 alone)
        "stage2_proxy":         ((c > ma200) & (slope200 > 0)).astype(float),
        "tsmom_12m":            (ret252 > 0).astype(float),
        "tsmom_6m":             (ret126 > 0).astype(float),
        # continuous vol-scaled buy&hold (Moreira-Muir): inverse-vol position, capped 1
        "volscaled_bh":         (realvol.median() / realvol).clip(upper=1.0).fillna(0.0),
    }
    return sig


# ----------------------------------------------------------------------------
# main index-level study
# ----------------------------------------------------------------------------
def run_index_study() -> str:
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    fwd = p["ix_close"].pct_change().shift(-1)     # hold t+1, close->close
    n = len(p)
    is_cut = int(n * 0.7)
    ismask = pd.Series(p.index < is_cut, index=p.index)
    oos = pd.Series(p.index >= is_cut, index=p.index)
    lines = []

    def out(s=""):
        print(s)
        lines.append(s)

    out(f"panel {p['date'].min()} -> {p['date'].max()}  n={n}  "
        f"IS<{p['date'].iloc[is_cut]}  OOS>=")
    bh_is, bh_oos = sharpe(fwd[ismask]), sharpe(fwd[oos])
    out(f"buy&hold  IS Sharpe={bh_is:+.2f}  OOS Sharpe={bh_oos:+.2f}\n")

    signals = build_signals(p)

    # ---- (1) standalone regime gates ----
    out("=== (1) STANDALONE regime gates (long/flat, direction a-priori long) ===")
    rows = []
    for name, s in signals.items():
        s = s.reindex(p.index)
        r = lf(s, fwd)
        ic_is = pd.DataFrame({"v": s, "f": fwd})[ismask].dropna().corr().iloc[0, 1]
        rows.append({
            "signal": name,
            "exposure": float(s.clip(0, 1).reindex(fwd.index).fillna(0).mean()),
            "IC_IS": ic_is,
            "IS_Sharpe": sharpe(r[ismask]),
            "OOS_Sharpe": sharpe(r[oos]),
        })
    res = pd.DataFrame(rows).sort_values("OOS_Sharpe", ascending=False)
    pd.set_option("display.float_format", lambda x: f"{x:+.3f}" if isinstance(x, float) else str(x))
    out(res.to_string(index=False))
    out(f"\n  reference: B&H OOS={bh_oos:+.2f}. A gate 'adds' only if OOS>B&H "
        f"AND it does so by cutting drawdown, not by luck (perm below).")

    # permutation for the canonical gate
    for probe in ["close_gt_ma200", "stage2_proxy", "tsmom_12m"]:
        s = signals[probe].reindex(p.index)
        a, pv = perm_p_same_exposure(s, fwd, oos)
        out(f"  perm[{probe}] OOS Sharpe={a:+.2f}  p(vs same-exposure random)={pv:.3f}")

    # ---- (2) regime-conditioning of the champion ----
    out("\n=== (2) REGIME-CONDITIONING: is the champion's edge bull-only? ===")
    champ_val = (p["fut_foreign_oi"] - p["fut_foreign_oi"].rolling(60).mean()) \
        / p["fut_foreign_oi"].rolling(60).std()
    champ_bull = (champ_val > 0).astype(float)
    champ_ret = lf(champ_bull, fwd)
    bull = signals["close_gt_ma200"].reindex(p.index).astype(bool)
    for label, m in [("ALL", pd.Series(True, index=p.index)),
                     ("bull(>MA200)", bull), ("bear(<=MA200)", ~bull)]:
        mm = m & oos
        out(f"  champion OOS Sharpe [{label:14s}] = {sharpe(champ_ret[mm]):+.2f}  "
            f"(days={int(mm.sum())})")

    # ---- (3) gating the champion by regime (the real L0 value) ----
    out("\n=== (3) GATED CHAMPION: champion AND regime vs champion alone ===")
    out("  (L0 value = cut bear-regime tail / raise risk-adjusted return, not add alpha)")
    gate_rows = []
    for gate_name in ["close_gt_ma200", "stage2_proxy", "tsmom_12m", "ma200_slope_up"]:
        g = signals[gate_name].reindex(p.index).astype(bool)
        gated = (champ_bull.astype(bool) & g).astype(float)
        gr = lf(gated, fwd)
        # max drawdown on OOS cumulative
        def maxdd(x):
            e = (1 + x.fillna(0)).cumprod()
            return float((e / e.cummax() - 1).min())
        gate_rows.append({
            "gate": gate_name,
            "champ_OOS": sharpe(champ_ret[oos]),
            "gated_OOS": sharpe(gr[oos]),
            "champ_maxDD": maxdd(champ_ret[oos]),
            "gated_maxDD": maxdd(gr[oos]),
            "exposure": float(gated.reindex(fwd.index).fillna(0).mean()),
        })
    gdf = pd.DataFrame(gate_rows)
    out(gdf.to_string(index=False))
    a, pv = perm_p_same_exposure(
        (champ_bull.astype(bool) & signals["close_gt_ma200"].reindex(p.index).astype(bool)).astype(float),
        fwd, oos)
    out(f"\n  perm[gated champion @ >MA200] OOS Sharpe={a:+.2f}  p={pv:.3f}")

    # ---- (4) collinearity with price momentum (the fatal pitfall) ----
    out("\n=== (4) COLLINEARITY with price momentum (these signals ARE momentum) ===")
    mom = p["ix_close"].pct_change(60)
    for name in ["close_gt_ma200", "tsmom_12m", "ma200_slope_up"]:
        c = pd.DataFrame({"a": signals[name].reindex(p.index), "b": mom}).dropna().corr().iloc[0, 1]
        out(f"  corr({name}, 60d price momentum) = {c:+.2f}")

    # ---- (5) Deflated Sharpe on the best standalone gate & gated champion ----
    out("\n=== (5) DEFLATED SHARPE (7 gate trials + champion) ===")
    n_trials = len(signals) + 1
    best = res.iloc[0]["signal"]
    for name, r in [(f"best gate={best}", lf(signals[best].reindex(p.index), fwd)[oos]),
                    ("gated champion @>MA200",
                     lf((champ_bull.astype(bool) & bull).astype(float), fwd)[oos]),
                    ("champion alone", champ_ret[oos])]:
        d = deflated_sharpe(r, n_trials)
        verdict = "SURVIVES" if (d["dsr"] is not np.nan and d["dsr"] > 0.95) else "FAILS"
        out(f"  [{name:26s}] ann={d['ann']:+.2f} PSR0={d['psr0']:.3f} "
            f"DSR={d['dsr']:.3f} -> {verdict} 0.95")

    res.to_csv(OUT / "tech_regime_gates.csv", index=False)
    out(f"\n-> {OUT/'tech_regime_gates.csv'}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# EXPLORATORY individual-stock RS extension (survivorship-biased — flagged)
# ----------------------------------------------------------------------------
def run_rs_demo() -> str:
    """RS = adj_close/IX0001 vs its own 52w MA (Mansfield). EXPLORATORY only:
    stock_daily_bars holds only currently/recently listed names -> survivorship
    bias inflates any breakout return (project-wide blind spot). No OOS claim made.
    """
    lines = ["\n=== RS DEMO (EXPLORATORY — survivorship-biased, no OOS verdict) ==="]

    def out(s):
        print(s)
        lines.append(s)

    con = sqlite3.connect(DB)
    ix = pd.read_sql(
        "SELECT date, close FROM daily_bars WHERE code='IX0001' AND source='finmind' ORDER BY date",
        con).rename(columns={"close": "ix"})
    ix["date"] = pd.to_datetime(ix["date"])
    # a handful of liquid names just to prove the pipeline computes Mansfield RS
    ids = ["2330", "2317", "2454", "2308", "2412"]
    hits = 0
    for sid in ids:
        b = pd.read_sql(
            "SELECT trade_date date, adj_close FROM stock_daily_bars "
            f"WHERE stock_id='{sid}' AND source='finmind' ORDER BY trade_date", con)
        if b.empty:
            continue
        b["date"] = pd.to_datetime(b["date"])
        m = b.merge(ix, on="date", how="inner")
        rs = m["adj_close"] / m["ix"]
        mansfield = rs / rs.rolling(252, min_periods=252).mean() - 1.0  # >0 => RS above own 52w MA
        latest = mansfield.dropna().iloc[-1] if mansfield.notna().any() else np.nan
        out(f"  {sid}: Mansfield RS (latest) = {latest:+.3f}  -> "
            f"{'RS-leader' if latest > 0 else 'RS-laggard'}")
        hits += 1
    con.close()
    out(f"  computed for {hits} names. TO VALIDATE PROPERLY: need adjusted-price "
        "universe INCLUDING delisted stocks (FinMind TaiwanStockPriceAdj + delisted "
        "set) before any IS/OOS/permutation/DSR claim.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-rs", action="store_true", help="also run stock RS demo")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    txt = run_index_study()
    if args.with_rs:
        txt += run_rs_demo()
    print("\n[done]")


if __name__ == "__main__":
    main()
