"""Price-Volume Structure (量價結構) — L1 confirmation-layer falsification study.

WHAT / WHY
  This is the L1 價量 dimension of the layered system (L0 regime -> L1 price-vol ->
  L2 chip core). It operationalises "成交值/量能/帶量突破" into回測-able factors and
  tests, falsification-first, whether market-level volume carries INDEPENDENT
  timing information beyond (a) buy-and-hold, (b) price momentum, and (c) the
  project champion (foreign-futures positioning, fut_foreign_oi z60>0).

  Methodology follows the chip-macro house style (see
  scripts/research/chip_macro/eval_stage4_newhypotheses.py + eval_deflated_sharpe.py):
    - direction of every signal fixed from IN-SAMPLE IC sign only (no OOS peek)
    - fwd return aligned no-lookahead: signal at close[t], trade ix_open[t+1]->ix_close[t+1]
    - 70/30 IS/OOS time split
    - IC (fwd1/fwd5), OOS Sharpe, permutation vs SAME-EXPOSURE random
    - momentum partial-out (residualise vol_z on mom20) — the核心 co-linearity test
    - regime-conditioning (index > upward MA200)
    - Deflated-Sharpe on the best breakout combo
    - correlation of daily returns vs champion (diversification / confirmation test)

DATA SOURCE
  LOCAL, no external sync needed:
    - data/research/chip_macro/panel.parquet: ix_open/high/low/close + ix_money
      (TAIEX 成交值, daily 2018-06 -> 2026-07), zero-values treated as NaN (data gaps).
  Optional individual-stock breakout scan (also LOCAL, runnable):
    - data/stocks.db table stock_daily_bars (stock_id, trade_date, close, volume,
      amount, adj_close), 2010->2026, 2504 ids. NOTE survivorship + adjustment caveats.
  Turnover-ratio / VCP extensions need shares-outstanding -> see SCAFFOLD note at bottom
  (FinMind TaiwanStockInfo / TaiwanStockPrice Trading_Volume; TWSE FMTQIK/STOCK_DAY).

HOW TO RUN
    cd /Users/jackm4/Documents/ETF/股票研究 && source .venv/bin/activate
    python scripts/research/dashboard/price_volume_study.py            # market-level (fast)
    python scripts/research/dashboard/price_volume_study.py --stocks   # + individual-stock breakout scan (slower)

  Writes a machine-readable summary to
  reports/research/dashboard-completeness/price_volume_metrics.csv
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from scipy.stats import norm
except Exception:  # scipy optional; DSR block skipped if unavailable
    norm = None

ROOT = Path(__file__).resolve().parents[3]
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
DB = ROOT / "data" / "stocks.db"
OUT = ROOT / "reports" / "research" / "dashboard-completeness"
COST_BPS = 4.0
ANN = np.sqrt(252)
RNG = np.random.default_rng(7)
GAMMA = 0.5772156649  # Euler-Mascheroni, for E[max SR]


# ---------- shared helpers (house style) ----------
def rz(s: pd.Series, w: int) -> pd.Series:
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def sharpe(x) -> float:
    x = pd.Series(x).dropna()
    return x.mean() / x.std() * ANN if len(x) > 20 and x.std() > 0 else np.nan


def lf(bull, day_ret, cost=COST_BPS) -> pd.Series:
    """Long/flat strategy return net of turnover cost."""
    pos = pd.Series(bull, index=day_ret.index).astype(float).fillna(0.0)
    return pos * day_ret - pos.diff().abs().fillna(pos.abs()) * (cost / 1e4)


def perm_p(bull, day_ret, mask, n=2000):
    """Permutation vs SAME-EXPOSURE random: hold the same # of long days at random."""
    dr = day_ret[mask].reset_index(drop=True)
    pos = pd.Series(bull, index=day_ret.index)[mask].astype(float).reset_index(drop=True)
    v = dr.notna()
    dr, pos = dr[v], pos[v]
    actual = sharpe(pos.values * dr.values)
    k, N = int(pos.sum()), len(pos)
    if k == 0 or k == N:
        return actual, np.nan
    drv = dr.values
    null = np.array([
        sharpe(np.isin(np.arange(N), RNG.choice(N, k, False)).astype(float) * drv)
        for _ in range(n)
    ])
    return actual, float((null >= actual).mean())


def deflated_sharpe(ret: pd.Series, n_trials: int) -> dict:
    """DSR: PSR of the strategy vs the expected max Sharpe of n_trials null strategies."""
    if norm is None:
        return {}
    r = pd.Series(ret).dropna().values
    n = len(r)
    if n < 30 or r.std() == 0:
        return {}
    sr = r.mean() / r.std()  # daily
    g3 = pd.Series(r).skew()
    g4 = pd.Series(r).kurtosis() + 3.0
    # crude per-trial SR variance proxy under iid null ~ 1/n (Bailey-Lopez de Prado)
    var_trials = 1.0 / n
    e = np.e
    sr_star = np.sqrt(var_trials) * (
        (1 - GAMMA) * norm.ppf(1 - 1.0 / n_trials) + GAMMA * norm.ppf(1 - 1.0 / (n_trials * e))
    )
    denom = np.sqrt(1 - g3 * sr + (g4 - 1) / 4.0 * sr**2)
    dsr = float(norm.cdf((sr - sr_star) * np.sqrt(n - 1) / denom))
    return {"daily_SR": sr, "ann_SR": sr * ANN, "skew": g3, "kurt": g4,
            "SR_star_ann": sr_star * ANN, "DSR": dsr}


# ---------- market-level study ----------
def market_study(rows: list) -> pd.DataFrame:
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    # de-trend / clean: ix_money==0 are data gaps -> NaN
    p["ix_money"] = p["ix_money"].replace(0.0, np.nan)

    day_ret = (p["ix_close"] / p["ix_open"] - 1.0).shift(-1)      # trade t+1 open->close
    fwd1 = (p["ix_close"].shift(-1) / p["ix_open"].shift(-1) - 1.0)
    fwd5 = (p["ix_close"].shift(-5) / p["ix_open"].shift(-1) - 1.0)
    close = p["ix_close"]
    ret1 = close.pct_change()
    mom20 = close.pct_change(20)

    n = len(p)
    is_cut = int(n * 0.7)
    ismask = pd.Series(p.index < is_cut, index=p.index)
    oos = pd.Series(p.index >= is_cut, index=p.index)

    # regime: index above an UPWARD 200d MA (house L0 gate)
    ma200 = close.rolling(200, min_periods=200).mean()
    bull_regime = (close > ma200) & (ma200 > ma200.shift(20))

    # champion reference
    champ_bull = rz(p["fut_foreign_oi"], 60) > 0
    champ_ret = lf(champ_bull, day_ret)

    # --- volume factor family ---
    vol_z20 = rz(p["ix_money"], 20)
    vol_z60 = rz(p["ix_money"], 60)
    vol_ratio20 = p["ix_money"] / p["ix_money"].rolling(20, min_periods=20).mean()  # 量比
    # OBV-style directional cumulative money, then z of its 20d slope
    obv = (np.sign(ret1).fillna(0) * p["ix_money"]).cumsum()
    obv_slope = obv.diff(20)
    obv_slope_z = rz(obv_slope, 60)
    # PVT (price-volume trend)
    pvt = (ret1.fillna(0) * p["ix_money"]).cumsum()
    pvt_slope_z = rz(pvt.diff(20), 60)
    # volume-price divergence: price up but volume shrinking (bearish precursor).
    # continuous form = sign(mom5) * (-vol_z20)  -> high when price rises on falling volume
    mom5 = close.pct_change(5)
    vp_diverg = np.sign(mom5).fillna(0) * (-vol_z20)

    cand_vals = {
        "vol_z20":        vol_z20,
        "vol_z60":        vol_z60,
        "vol_ratio20":    vol_ratio20,
        "obv_slope_z60":  obv_slope_z,
        "pvt_slope_z60":  pvt_slope_z,
        "vp_divergence":  vp_diverg,
    }

    scan = []
    for name, v in cand_vals.items():
        v = pd.Series(v, index=p.index)
        d = pd.DataFrame({"v": v, "f": day_ret}).dropna()
        if len(d) < 100:
            continue
        ism = ismask.reindex(d.index).fillna(False)
        ic_is = d[ism]["v"].corr(d[ism]["f"])
        # extra descriptive ICs
        ic_fwd1 = pd.DataFrame({"v": v, "f": fwd1}).dropna().corr().iloc[0, 1]
        ic_fwd5 = pd.DataFrame({"v": v, "f": fwd5}).dropna().corr().iloc[0, 1]
        corr_mom = pd.DataFrame({"v": v, "m": mom20}).dropna().corr().iloc[0, 1]
        direction = np.sign(ic_is) if pd.notna(ic_is) and ic_is != 0 else 1.0
        directed = direction * v
        bull = directed > 0
        r = lf(bull, day_ret)
        oos_shp = sharpe(r[oos])
        corr_champ = pd.DataFrame({"a": r, "b": champ_ret}).dropna().corr().iloc[0, 1]
        scan.append({
            "signal": name, "dir": int(direction),
            "IC_IS": ic_is, "IC_fwd1": ic_fwd1, "IC_fwd5": ic_fwd5,
            "corr_mom20": corr_mom, "OOS_Sharpe": oos_shp,
            "corr_vs_champ": corr_champ,
            "exposure": bull.reindex(day_ret.index).fillna(False).mean(),
        })
    scan_df = pd.DataFrame(scan).sort_values("OOS_Sharpe", ascending=False)

    bh_is = sharpe(day_ret[ismask])
    bh_oos = sharpe(day_ret[oos])
    champ_oos = sharpe(champ_ret[oos])
    print(f"B&H  IS Sharpe={bh_is:+.2f}  OOS={bh_oos:+.2f}   |   champion OOS={champ_oos:+.2f}\n")
    pd.set_option("display.float_format", lambda x: f"{x:+.3f}" if isinstance(x, float) else str(x))
    print("=== volume factor scan (direction fixed from IS IC) ===")
    print(scan_df.to_string(index=False))

    for _, rr in scan_df.iterrows():
        rows.append({"section": "market_scan", **rr.to_dict(),
                     "bh_oos": bh_oos, "champ_oos": champ_oos})

    # --- breakout + volume confirmation (the real use of volume) ---
    print("\n=== 帶量突破 breakout(20d high) x volume confirmation ===")
    hi20 = close.rolling(20, min_periods=20).max()
    at_high = close >= hi20 * 0.999
    fwd5_full = fwd5.mean()
    for thr in [0.0, 0.5, 1.0]:
        bo_novol = at_high
        bo_vol = at_high & (vol_z20 > thr)
        m_all = fwd5[at_high.reindex(fwd5.index).fillna(False)].mean()
        m_vol = fwd5[bo_vol.reindex(fwd5.index).fillna(False)].mean()
        # co-linearity control: same breakout but LOW volume
        bo_lowvol = at_high & (vol_z20 <= thr)
        m_low = fwd5[bo_lowvol.reindex(fwd5.index).fillna(False)].mean()
        print(f"  vol_z>{thr:>3}: breakout n={int(bo_vol.sum())} fwd5={m_vol:+.3%} | "
              f"low-vol n={int(bo_lowvol.sum())} fwd5={m_low:+.3%} | "
              f"any-breakout={m_all:+.3%} | sample={fwd5_full:+.3%}")
        rows.append({"section": "breakout", "signal": f"breakout_volz>{thr}",
                     "n_vol": int(bo_vol.sum()), "fwd5_vol": m_vol,
                     "n_lowvol": int(bo_lowvol.sum()), "fwd5_lowvol": m_low,
                     "fwd5_anybreakout": m_all, "fwd5_sample": fwd5_full})

    # --- momentum partial-out: does vol_z20 add anything beyond mom20? ---
    print("\n=== momentum co-linearity: residualise vol_z20 on mom20 ===")
    dd = pd.DataFrame({"v": vol_z20, "m": mom20, "f": day_ret}).dropna()
    beta = np.polyfit(dd["m"], dd["v"], 1)[0]
    resid = dd["v"] - beta * dd["m"]
    ic_raw = dd["v"].corr(dd["f"])
    ic_resid = resid.corr(dd["f"])
    print(f"  raw   corr(vol_z20, day_ret)      = {ic_raw:+.4f}")
    print(f"  resid corr(vol_z20|mom20, day_ret) = {ic_resid:+.4f}  "
          f"(if ~0, volume is momentum in disguise)")
    rows.append({"section": "momentum_control", "ic_raw": ic_raw,
                 "ic_resid_on_mom": ic_resid, "beta_vol_on_mom": beta})

    # --- regime conditioning + confirmation combo with champion ---
    print("\n=== regime-conditioned volume confirmation & champion integration ===")
    # best single confirmation candidate: breakout AND vol_z20>0, only in bull regime
    conf = at_high & (vol_z20 > 0) & bull_regime
    conf_ret = lf(conf, day_ret)
    print(f"  bull-regime 帶量突破 long/flat: OOS Sharpe={sharpe(conf_ret[oos]):+.2f} "
          f"exposure={conf.mean():.2%}")
    # champion GATED by volume confirmation (use vol as a filter on champion longs)
    champ_volgate = champ_bull & (vol_z20 > -1.0)   # veto only on truly dead-volume days
    cg_ret = lf(champ_volgate, day_ret)
    print(f"  champion alone OOS={sharpe(champ_ret[oos]):+.2f} | "
          f"champion x vol-not-dead OOS={sharpe(cg_ret[oos]):+.2f}")
    a, pv = perm_p(conf, day_ret, oos)
    print(f"  bull-regime breakout+vol OOS perm p={pv} (vs same-exposure random)")
    rows.append({"section": "regime_integration",
                 "bullregime_breakout_vol_oos": sharpe(conf_ret[oos]),
                 "champ_oos": sharpe(champ_ret[oos]),
                 "champ_volgate_oos": sharpe(cg_ret[oos]),
                 "breakout_vol_oos_perm_p": pv})

    # --- Deflated Sharpe on the best volume-based strategy (full sample) ---
    best_name = scan_df.iloc[0]["signal"]
    best_v = pd.Series(cand_vals[best_name], index=p.index)
    best_bull = (scan_df.iloc[0]["dir"] * best_v) > 0
    best_ret = lf(best_bull, day_ret)
    n_trials = len(cand_vals) + 3  # scan + breakout thresholds ~ search size
    dsr = deflated_sharpe(best_ret.dropna(), n_trials)
    if dsr:
        print(f"\n=== Deflated-Sharpe (best volume strat '{best_name}', n_trials={n_trials}) ===")
        print(f"  ann.Sharpe={dsr['ann_SR']:+.2f}  skew={dsr['skew']:+.2f} kurt={dsr['kurt']:.1f}")
        print(f"  SR*_expected_max(ann)={dsr['SR_star_ann']:+.2f}")
        print(f"  DEFLATED SR={dsr['DSR']:.3f} -> "
              f"{'SURVIVES' if dsr['DSR'] > 0.95 else 'FAILS'} 5% multiple-testing")
        rows.append({"section": "deflated_sharpe", "signal": best_name,
                     "n_trials": n_trials, **dsr})

    return scan_df


# ---------- optional individual-stock breakout scan (LOCAL, runnable) ----------
def stock_breakout_scan(rows: list, min_amount=5e7, start="2019-01-01"):
    """帶量突破 at the single-stock level using stock_daily_bars.amount.
    CAVEAT: survivorship (delisted names absent) + adjustment (uses adj_close if present).
    This is exploratory evidence, NOT a clean backtest.
    """
    print("\n" + "=" * 60)
    print("INDIVIDUAL-STOCK 帶量突破 scan (exploratory; survivorship caveat)")
    con = sqlite3.connect(DB)
    # liquid universe: ids with decent median amount in the window
    liquid = pd.read_sql(
        "SELECT stock_id, COUNT(*) n, AVG(amount) avg_amt FROM stock_daily_bars "
        "WHERE trade_date>=? AND amount IS NOT NULL GROUP BY stock_id "
        "HAVING n>500 AND avg_amt>? ",
        con, params=[start, min_amount])
    ids = liquid["stock_id"].tolist()
    print(f"  liquid universe: {len(ids)} ids (avg amount>{min_amount:.0e}, since {start})")
    # sample to keep runtime reasonable
    if len(ids) > 400:
        ids = list(pd.Series(ids).sample(400, random_state=7))
    ph = ",".join("?" * len(ids))
    df = pd.read_sql(
        f"SELECT stock_id, trade_date, COALESCE(adj_close,close) px, amount "
        f"FROM stock_daily_bars WHERE stock_id IN ({ph}) AND trade_date>=? "
        f"AND amount IS NOT NULL ORDER BY stock_id, trade_date",
        con, params=ids + [start])
    con.close()

    recs = []
    for sid, g in df.groupby("stock_id"):
        g = g.reset_index(drop=True)
        if len(g) < 260:
            continue
        px = g["px"]
        amt = g["amount"]
        hi60 = px.rolling(60, min_periods=60).max()
        vz = (amt - amt.rolling(20, min_periods=20).mean()) / amt.rolling(20, min_periods=20).std()
        fwd10 = px.shift(-10) / px - 1.0
        at_high = px >= hi60 * 0.999
        bo_vol = at_high & (vz > 1.0)
        bo_low = at_high & (vz <= 1.0)
        for label, mask in [("vol", bo_vol), ("lowvol", bo_low)]:
            m = mask.fillna(False)
            if m.sum() >= 3:
                recs.append({"stock_id": sid, "kind": label,
                             "n": int(m.sum()), "fwd10": fwd10[m].mean()})
    r = pd.DataFrame(recs)
    if r.empty:
        print("  no data")
        return
    agg = r.groupby("kind").apply(
        lambda x: pd.Series({"events": int(x["n"].sum()),
                             "mean_fwd10": np.average(x["fwd10"], weights=x["n"]),
                             "n_stocks": x["stock_id"].nunique()}),
        include_groups=False).reset_index()
    print(agg.to_string(index=False))
    print("  -> compare 'vol' vs 'lowvol' breakout fwd10; a real edge needs vol > lowvol")
    for _, a in agg.iterrows():
        rows.append({"section": "stock_breakout", "kind": a["kind"],
                     "events": a["events"], "mean_fwd10": a["mean_fwd10"],
                     "n_stocks": a["n_stocks"]})


# ---------------------------------------------------------------------------
# SCAFFOLD: turnover-ratio (換手率) & VCP extensions need shares-outstanding.
#   FinMind:  TaiwanStockInfo (股本) or compute from TaiwanStockPrice; project uses
#             src/chip_data.py conventions (.cursor/rules/finmind.mdc).
#   TWSE:     FMTQIK (大盤成交量值 for cross-check), STOCK_DAY (個股日成交), MI_INDEX.
#   TPEX:     對應櫃買成交統計 endpoint.
#   turnover = Trading_Volume / shares_outstanding ; low-turnover -> higher expected
#   return is a MONTHLY CROSS-SECTIONAL factor (Datar-Naik-Radcliffe 1998), NOT the
#   daily index timing tested above — keep the horizons separate.
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", action="store_true",
                    help="also run the individual-stock breakout scan (slower)")
    args = ap.parse_args()

    rows: list = []
    market_study(rows)
    if args.stocks:
        stock_breakout_scan(rows)

    OUT.mkdir(parents=True, exist_ok=True)
    out_csv = OUT / "price_volume_metrics.csv"
    pd.DataFrame(rows).to_csv(out_csv, index=False)
    print(f"\n-> wrote {out_csv}")


if __name__ == "__main__":
    main()
