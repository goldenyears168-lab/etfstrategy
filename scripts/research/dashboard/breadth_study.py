"""市場廣度／騰落 (Market Breadth / Advance-Decline) — falsification-first study.

WHAT
    Builds the classic market-breadth family from the *adjusted-price* Taiwan
    universe and runs each variant through the project-standard falsification
    gauntlet, to answer one question honestly:

        Does breadth add anything OVER the price index itself and OVER the
        champion (foreign-futures positioning), or is it — as the literature
        and this project's Stage-4 margin_bal precedent both warn — merely
        price-momentum re-expressed (a coincident L0/L1 regime light, not
        independent lead alpha)?

    Signals built (all market-level daily series):
      - net_breadth      = (adv - dec) / (adv + dec)      Zaremba-style, ∈[-1,1]
      - adr              = adv / dec                       (advance-decline ratio)
      - adl              = Σ(adv - dec)                     (advance-decline line; detrended)
      - pct_above_ma50   = share of universe with adj_close > own SMA50
      - pct_above_ma200  = share of universe with adj_close > own SMA200  (L0 regime light)
      - nhnl             = (#52w-high - #52w-low) / participants   (New-High-New-Low)
      - mcclellan        = EMA19(adv_ratio) - EMA39(adv_ratio)     (ratio-adjusted McClellan Osc)
      - zbt (event)      = Zweig Breadth Thrust: 10d-EMA of adv/(adv+dec) rising
                           from <0.40 to >0.615 within <=10 sessions -> arm bull.

    Level series (adl, pct_above_ma*, nhnl) are z-scored (z60) because raw
    levels are non-stationary; ratios/net are also z-scored for comparability.
    Direction of every signal is fixed from the IN-SAMPLE IC sign only (no OOS peek).

DATA (all LOCAL — no external sync needed; implementable_now = True)
    - Adjusted stock prices:  data/stocks.db -> stock_close_adjusted.adj_close_v2
        (dividend+split adjusted, 2010-01 -> 2026-07-27, ~870 names/day in 2018
         growing to ~1100 in 2026; PIT via cum_factor rebuilt from event history.
         MUST use adjusted price: raw close manufactures fake "declines" on
         ex-dividend days -> corrupts A/D counts.)
    - Market TAIEX index:     data/stocks.db -> daily_bars where code='IX0001'
        (finmind TAIEX, 2015-01 -> today) — used only to cross-check; forward
        returns come from panel.parquet ix_open/ix_close for chip-macro parity.
    - Champion / forward ret: data/research/chip_macro/panel.parquet
        fut_foreign_oi z60 > 0 (the validated market-timing champion),
        forward index return fwd = ix_close[t]/ix_open[t+1]... (panel convention:
        signal known at close t, earliest tradeable t+1 open).

    OPTIONAL official cross-check (NOT required, not used here):
      TWSE rwd  https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=YYYYMMDD&type=MS&response=json
                (returns 漲/跌/平 證券數合計 — official advance/decline counts, TWSE only)
      TWSE OpenAPI https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX
      TPEX OpenAPI (上櫃) for OTC advance/decline — self-aggregation already covers both.

METHODOLOGY (project standard — reports/research/chip-macro/LAYERED_DESIGN.md)
    - Time-series market-timing test (NOT cross-sectional): breadth_t -> long
      index t+1 open->close when directed signal bullish; cost on turnover.
    - IS/OOS 70/30 chronological split; IC fixed on IS only.
    - Permutation vs same-exposure random (2000 draws) on OOS Sharpe.
    - Deflated-Sharpe-style haircut: N_trials = #signals scanned (multiple-testing).
    - Regime conditioning: bull = ix_close > rising MA200, else bear.
    - THE anti-"momentum-in-disguise" check (decisive here):
        * partial: corr(breadth signal-return, index price-momentum z) controlled
        * corr(breadth strat daily return, champion daily return) -> is it independent?
        * orthogonalize breadth level on price_momentum z, keep only the residual,
          re-test — if residual edge vanishes, it was momentum in disguise.
    - Zweig Breadth Thrust evaluated as an event study (fwd 20/60d after each arm).

HOW TO RUN
    cd /Users/jackm4/Documents/ETF/股票研究 && source .venv/bin/activate
    python scripts/research/dashboard/breadth_study.py
    (prints tables; writes reports/research/dashboard-completeness/breadth_metrics.csv)

    Non-investment advice: research code only, not a recommendation to trade.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
DB = ROOT / "data" / "stocks.db"
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
OUTDIR = ROOT / "reports" / "research" / "dashboard-completeness"

START = "2018-06-01"        # align with panel / champion availability
MIN_PARTICIPANTS = 600      # drop days with incomplete backfill (near-end lag)
COST_BPS = 4.0
ANN = np.sqrt(252)
RNG = np.random.default_rng(7)
N_PERM = 2000


# ----------------------------------------------------------------------------- helpers
def sharpe(x) -> float:
    x = pd.Series(x).dropna()
    return float(x.mean() / x.std() * ANN) if len(x) > 20 and x.std() > 0 else np.nan


def rz(s: pd.Series, w: int) -> pd.Series:
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def lf(bull, day_ret, cost=COST_BPS) -> pd.Series:
    pos = pd.Series(bull, index=day_ret.index).astype(float).fillna(0.0)
    return pos * day_ret - pos.diff().abs().fillna(pos.abs()) * (cost / 1e4)


def perm_p(bull, day_ret, mask, n=N_PERM):
    dr = day_ret[mask].reset_index(drop=True)
    pos = pd.Series(bull, index=day_ret.index)[mask].astype(float).reset_index(drop=True)
    v = dr.notna()
    dr, pos = dr[v], pos[v]
    actual = sharpe(pos.values * dr.values)
    k, N = int(pos.sum()), len(pos)
    if k == 0 or k == N or not np.isfinite(actual):
        return actual, np.nan
    drv = dr.values
    null = np.empty(n)
    for i in range(n):
        idx = RNG.choice(N, k, replace=False)
        p = np.zeros(N)
        p[idx] = 1.0
        null[i] = sharpe(p * drv)
    return actual, float((null >= actual).mean())


def deflated_haircut(oos_sharpe: float, n_trials: int, n_obs: int) -> float:
    """Very rough Deflated-Sharpe style probability that OOS Sharpe > 0 given the
    multiple-testing selection (n_trials variants) — expected max of N standard
    normals as the null threshold. Returns approx probability the observed OOS
    Sharpe beats the selection-inflated null. Conservative, honest flag only."""
    if not np.isfinite(oos_sharpe):
        return np.nan
    from math import log, sqrt
    # daily Sharpe (annualized -> per-obs) and its std ~ 1/sqrt(n_obs)
    sr_daily = oos_sharpe / ANN
    se = 1.0 / sqrt(max(n_obs, 2))
    # expected max of n_trials iid N(0,1), approx
    emax = sqrt(2 * log(max(n_trials, 2)))
    z = (sr_daily - emax * se) / se
    # standard normal cdf
    from math import erf
    return 0.5 * (1 + erf(z / sqrt(2)))


# ----------------------------------------------------------------------------- data
def load_breadth() -> pd.DataFrame:
    """Compute the daily market-breadth panel from adjusted stock prices."""
    con = sqlite3.connect(DB)
    px = pd.read_sql(
        "SELECT stock_id, trade_date, adj_close_v2 AS px "
        "FROM stock_close_adjusted WHERE trade_date >= ? AND adj_close_v2 > 0",
        con, params=(START,),
    )
    con.close()

    # wide matrix: rows=date, cols=stock
    px["trade_date"] = pd.to_datetime(px["trade_date"])
    w = px.pivot_table(index="trade_date", columns="stock_id", values="px").sort_index()

    # daily simple return per stock (adjusted -> ex-div safe)
    ret = w.pct_change(fill_method=None)
    adv = (ret > 0).sum(axis=1)
    dec = (ret < 0).sum(axis=1)
    part = (ret.notna()).sum(axis=1)               # participants that day

    # per-stock rolling MA and 52w extremes (min_periods to avoid look-back short)
    ma50 = w.rolling(50, min_periods=50).mean()
    ma200 = w.rolling(200, min_periods=200).mean()
    hi252 = w.rolling(252, min_periods=252).max()
    lo252 = w.rolling(252, min_periods=252).min()

    above50 = (w > ma50)
    above200 = (w > ma200)
    is_nh = (w >= hi252)
    is_nl = (w <= lo252)

    df = pd.DataFrame(index=w.index)
    df["participants"] = part
    df["adv"] = adv
    df["dec"] = dec
    df["net_breadth"] = (adv - dec) / (adv + dec).replace(0, np.nan)
    df["adr"] = adv / dec.replace(0, np.nan)
    df["adl"] = (adv - dec).cumsum()
    df["pct_above_ma50"] = above50.sum(axis=1) / above50.notna().sum(axis=1).replace(0, np.nan)
    df["pct_above_ma200"] = above200.sum(axis=1) / above200.notna().sum(axis=1).replace(0, np.nan)
    nh = is_nh.sum(axis=1)
    nl = is_nl.sum(axis=1)
    df["nhnl"] = (nh - nl) / part.replace(0, np.nan)
    adv_ratio = adv / (adv + dec).replace(0, np.nan)
    df["adv_ratio"] = adv_ratio
    df["mcclellan"] = adv_ratio.ewm(span=19).mean() - adv_ratio.ewm(span=39).mean()
    df["zbt_ema10"] = adv_ratio.ewm(span=10).mean()

    df = df.reset_index().rename(columns={"trade_date": "date"})
    # drop incomplete-backfill days (protects the recent-lag pitfall)
    df = df[df["participants"] >= MIN_PARTICIPANTS].reset_index(drop=True)
    return df


# ----------------------------------------------------------------------------- main
def main() -> None:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    bd = load_breadth()
    panel = pd.read_parquet(PANEL)
    panel["date"] = pd.to_datetime(panel["date"])

    p = panel.merge(bd, on="date", how="inner").sort_values("date").reset_index(drop=True)
    print(f"merged rows: {len(p)}  ({p['date'].min().date()} -> {p['date'].max().date()})  "
          f"median participants={int(p['participants'].median())}")

    # forward index return (panel convention: signal at close t, trade t+1 open->close)
    day_ret = (p["ix_close"] / p["ix_open"] - 1.0).shift(-1)

    n = len(p)
    is_cut = int(n * 0.7)
    ismask = pd.Series(p.index < is_cut, index=p.index)
    oos = pd.Series(p.index >= is_cut, index=p.index)

    # champion reference
    champ_bull = rz(p["fut_foreign_oi"], 60) > 0
    champ_ret = lf(champ_bull, day_ret)

    # price momentum control (for the anti-disguise test): index 60d z-score of returns
    px_mom = rz(p["ix_close"].pct_change(), 60)          # short momentum z
    px_trend = rz(p["ix_close"], 200)                    # slow level z (trend)

    # candidate breadth signals (level-type -> z-scored)
    cand = {
        "net_breadth_z60":  rz(p["net_breadth"].rolling(10, min_periods=10).mean(), 60),
        "adr_z60":          rz(p["adr"], 60),
        "adl_z60":          rz(p["adl"], 60),
        "pct_ma50_z60":     rz(p["pct_above_ma50"], 60),
        "pct_ma200_z60":    rz(p["pct_above_ma200"], 60),
        "pct_ma200_lvl":    p["pct_above_ma200"] - 0.5,      # raw L0 regime light (>50%)
        "nhnl_z60":         rz(p["nhnl"].rolling(10, min_periods=10).mean(), 60),
        "mcclellan":        p["mcclellan"],                  # already oscillator (mean ~0)
    }

    rows = []
    for name, v in cand.items():
        v = pd.Series(v, index=p.index)
        d = pd.DataFrame({"v": v, "f": day_ret}).dropna()
        if len(d) < 200:
            continue
        m_is = ismask.reindex(d.index).fillna(False)
        ic_is = d[m_is]["v"].corr(d[m_is]["f"])
        direction = np.sign(ic_is) if pd.notna(ic_is) and ic_is != 0 else 1.0
        directed = direction * v
        bull = directed > 0
        r = lf(bull, day_ret)
        oos_shp = sharpe(r[oos])
        is_shp = sharpe(r[ismask])
        corr_champ = pd.DataFrame({"a": r, "b": champ_ret}).dropna().corr().iloc[0, 1]
        # anti-disguise: partial correlation of the signal level with fwd, controlling px_mom
        dd = pd.DataFrame({"v": v, "f": day_ret, "m": px_mom, "t": px_trend}).dropna()
        raw_ic = dd["v"].corr(dd["f"])
        # residualize v on px_mom+px_trend, correlate residual with fwd
        X = np.c_[np.ones(len(dd)), dd["m"].values, dd["t"].values]
        beta, *_ = np.linalg.lstsq(X, dd["v"].values, rcond=None)
        resid = dd["v"].values - X @ beta
        resid_ic = pd.Series(resid).corr(dd["f"].reset_index(drop=True))
        # bull/bear regime
        bull_reg = (p["ix_close"] > p["ix_close"].rolling(200, min_periods=200).mean()) & \
                   (p["ix_close"].rolling(200, min_periods=200).mean().diff(25) > 0)
        r_bull = r[bull_reg.reindex(r.index).fillna(False) & oos]
        r_bear = r[(~bull_reg.reindex(r.index).fillna(True)) & oos]
        rows.append({
            "signal": name, "dir": int(direction), "IC_IS": ic_is,
            "IS_Sharpe": is_shp, "OOS_Sharpe": oos_shp,
            "corr_vs_champ": corr_champ,
            "raw_IC_full": raw_ic, "resid_IC_ctrl_mom": resid_ic,
            "OOS_bull_Sh": sharpe(r_bull), "OOS_bear_Sh": sharpe(r_bear),
            "exposure": bull.reindex(day_ret.index).fillna(False).mean(),
        })

    res = pd.DataFrame(rows).sort_values("OOS_Sharpe", ascending=False)
    bh_oos = sharpe(day_ret[oos])
    n_trials = len(res)
    res["deflated_p"] = res["OOS_Sharpe"].apply(
        lambda s: deflated_haircut(s, n_trials, int(oos.sum())))

    pd.set_option("display.width", 200)
    pd.set_option("display.float_format", lambda x: f"{x:+.3f}" if isinstance(x, float) else str(x))
    print(f"\nB&H OOS Sharpe = {bh_oos:+.2f} ; champion OOS = {sharpe(champ_ret[oos]):+.2f} "
          f"(n_trials={n_trials}, OOS n={int(oos.sum())})\n")
    print("=== breadth signal scan (direction fixed on IS IC) ===")
    show = ["signal", "dir", "IC_IS", "OOS_Sharpe", "corr_vs_champ",
            "raw_IC_full", "resid_IC_ctrl_mom", "OOS_bull_Sh", "OOS_bear_Sh",
            "deflated_p", "exposure"]
    print(res[show].to_string(index=False))

    # permutation on the best OOS survivor
    best = res.iloc[0]
    v = pd.Series(cand[best["signal"]], index=p.index)
    bull = (best["dir"] * v) > 0
    a, pv = perm_p(bull, day_ret, oos)
    print(f"\nBest OOS survivor = {best['signal']}: OOS Sharpe={a:+.2f}, "
          f"permutation p={pv:.3f}, deflated_p={best['deflated_p']:.3f}")

    # champion + best breadth 50/50 combo (does breadth ADD over champion?)
    comb = (champ_bull.astype(float) + bull.astype(float)) / 2
    cr = comb * day_ret - comb.diff().abs().fillna(comb.abs()) * COST_BPS / 1e4
    print(f"champ+{best['signal']} 50/50 OOS Sharpe={sharpe(cr[oos]):+.2f} "
          f"(champ alone {sharpe(champ_ret[oos]):+.2f})")

    # -------- Zweig Breadth Thrust event study (the one lead-flavored breadth use)
    print("\n=== Zweig Breadth Thrust event study ===")
    e = p["zbt_ema10"]
    arms = []
    for i in range(10, len(p)):
        window = e.iloc[i - 10:i + 1]
        if e.iloc[i] > 0.615 and (window.iloc[:-1] < 0.40).any() and e.iloc[i - 1] <= 0.615:
            arms.append(i)
    print(f"  ZBT triggers (2018-06 -> now, adj universe): {len(arms)}")
    for h in (20, 60):
        fwd = (p["ix_close"].shift(-h) / p["ix_close"] - 1.0)
        rr = [fwd.iloc[i] for i in arms if i + h < len(p) and pd.notna(fwd.iloc[i])]
        base = fwd.dropna()
        if rr:
            print(f"  fwd{h}d after ZBT: mean={np.mean(rr):+.2%} (n={len(rr)})  "
                  f"vs unconditional mean={base.mean():+.2%}  hit>{0}={np.mean(np.array(rr) > 0):.0%}")
        else:
            print(f"  fwd{h}d after ZBT: no events with full horizon")
    if arms:
        print("  trigger dates:", ", ".join(str(p['date'].iloc[i].date()) for i in arms))

    res.to_csv(OUTDIR / "breadth_metrics.csv", index=False)
    print(f"\n-> {OUTDIR / 'breadth_metrics.csv'}")


if __name__ == "__main__":
    main()
