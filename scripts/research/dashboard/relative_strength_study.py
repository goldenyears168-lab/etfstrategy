"""相對強度 RS(個股 vs 大盤 TAIEX)—falsification-first cross-sectional study.

WHAT
    Tests whether "relative strength of a stock vs the market index" carries any
    tradeable cross-sectional edge in Taiwan, or whether it is (as the literature
    warns) merely price-momentum in disguise — and Taiwan is a famous momentum
    *reversal* market, so the null (no edge / negative edge) is the prior.

    Four textbook operationalizations of RS are built and each is run through the
    same project-standard falsification gauntlet:
      1. Mansfield RS oscillator (Weinstein Stage Analysis):
           RS_line = adj_close / ix_close
           RSM     = (RS_line / SMA(RS_line, n) - 1) * 100      (n = 50 trading d)
         -> the form that plugs into the project's existing Weinstein Stage-2 gate.
      2. RS 12-1 momentum:  relative return over ~252d skipping the last ~21d
         (Jegadeesh-Titman formation, avoids the 1-month short-term reversal).
      3. George-Hwang 52-week-high nearness:  price / rolling_252d_max
         (the most reversal-resistant RS form; strongest in the academic record).
      4. IBD-style cross-sectional RS percentile (quarter-weighted return rank).

DATA (all LOCAL — no external sync needed)
    - Stock prices:  data/stocks.db  ->  stock_daily_bars
         (source='finmind', adj_close; ~99% populated for the liquid universe.
          NOTE: adj_close is only ~10% populated across the *full* 2504-name table
          because thousands of illiquid/delisted names + yfinance rows lack it;
          restricting to liquid finmind names gives near-complete adjusted prices.)
    - Market TAIEX:  data/stocks.db  ->  daily_bars where code='IX0001'
         (close, 2015-01 -> today; the finmind TAIEX line, PIT-clean.
          panel.parquet ix_close only starts 2018-06 and IR0002/TWII are proxies.)
    - Champion reference (market timing, for cross-layer decorrelation):
         data/research/chip_macro/panel.parquet -> fut_foreign_oi z60 > 0.

METHODOLOGY (project standard — see reports/research/chip-macro/LAYERED_DESIGN.md)
    - Signal is CROSS-SECTIONAL: each day rank the liquid universe by the RS signal,
      go long top quintile / short bottom quintile, equal-weight, weekly rebalance
      (t computed on close, traded t+1, no look-ahead). Cost applied on turnover.
    - IS/OOS 70/30 chronological split.
    - Rank IC (Spearman) of signal_t vs next-period stock return.
    - Permutation test: shuffle the stock->signal assignment within each day
      (same exposure / same #long/#short), 1000 draws, one-sided p on OOS Sharpe.
    - Regime conditioning: split by index > rising MA200 (bull) vs not (bear).
    - Decorrelation (the anti-"momentum-in-disguise" check):
        corr( RS long-short daily return , 12-1 own-momentum long-short return )
        corr( RS long-short daily return , champion market-timing daily return )

HOW TO RUN
    cd /Users/jackm4/goldenstocks && source .venv/bin/activate
    python scripts/research/dashboard/relative_strength_study.py
    (writes summary CSV to reports/research/dashboard-completeness/)

    Non-investment-advice: research code only; not a recommendation to trade.
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

START = "2018-06-01"          # align with panel / champion availability
MIN_OBS = 1800                # ~7y of trading days
MIN_AVG_AMOUNT = 1.5e8        # liquid filter (NT$ per day)
REBAL = 5                     # weekly rebalance (trading days)
COST_BPS = 4.0               # per side, on turnover
QUANTILE = 0.20               # top/bottom quintile long-short
ANN = np.sqrt(252)
RNG = np.random.default_rng(7)
N_PERM = 1000


def sharpe(x: pd.Series) -> float:
    x = pd.Series(x).dropna()
    return float(x.mean() / x.std() * ANN) if len(x) > 20 and x.std() > 0 else np.nan


def load_prices() -> pd.DataFrame:
    """Return wide adj_close panel (index=date, cols=stock_id) for liquid universe."""
    con = sqlite3.connect(DB)
    uni = pd.read_sql_query(
        """SELECT stock_id FROM stock_daily_bars
           WHERE source='finmind' AND trade_date>=? AND LENGTH(stock_id)=4
             AND adj_close IS NOT NULL
           GROUP BY stock_id
           HAVING COUNT(*)>? AND AVG(amount)>?""",
        con, params=(START, MIN_OBS, MIN_AVG_AMOUNT))["stock_id"].tolist()
    ph = ",".join("?" * len(uni))
    px = pd.read_sql_query(
        f"""SELECT trade_date AS date, stock_id,
                   COALESCE(adj_close, close) AS px
            FROM stock_daily_bars
            WHERE source='finmind' AND trade_date>=? AND stock_id IN ({ph})""",
        con, params=[START, *uni])
    con.close()
    wide = px.pivot_table(index="date", columns="stock_id", values="px").sort_index()
    return wide


def load_index() -> pd.Series:
    con = sqlite3.connect(DB)
    ix = pd.read_sql_query(
        "SELECT date, close FROM daily_bars WHERE code='IX0001' AND date>=? ORDER BY date",
        con, params=(START,))
    con.close()
    ix = ix.drop_duplicates(subset="date", keep="last")
    return ix.set_index("date")["close"]


def champion_returns() -> pd.Series:
    """fut_foreign_oi z60 > 0 -> long TAIEX next-day open->close. Daily strat return."""
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    z = (p["fut_foreign_oi"] - p["fut_foreign_oi"].rolling(60).mean()) / p["fut_foreign_oi"].rolling(60).std()
    bull = (z > 0).astype(float)
    day_ret = (p["ix_close"] / p["ix_open"] - 1.0).shift(-1)
    r = bull * day_ret - bull.diff().abs().fillna(bull.abs()) * COST_BPS / 1e4
    return pd.Series(r.values, index=p["date"].astype(str))


# ---- RS signal constructions (all PIT: use info up to and including day t) -------
def build_signals(px: pd.DataFrame, ix: pd.Series) -> dict[str, pd.DataFrame]:
    ix = ix.reindex(px.index).ffill()
    rs_line = px.div(ix, axis=0)                       # Dorsey relative-strength line
    sig = {}
    # 1) Mansfield RS oscillator (Weinstein), n=50 trading days
    sma = rs_line.rolling(50, min_periods=50).mean()
    sig["mansfield_rsm50"] = (rs_line / sma - 1.0) * 100.0
    # 2) RS 12-1 momentum: relative-line return 252d skip last 21d
    sig["rs_mom_252_21"] = rs_line.shift(21) / rs_line.shift(252) - 1.0
    # 3) George-Hwang 52-week-high nearness (raw price, reversal-resistant)
    roll_max = px.rolling(252, min_periods=200).max()
    sig["gh_52w_high"] = px / roll_max
    # 4) IBD-style quarter-weighted return, then cross-sectional percentile
    q = 63
    r1 = px / px.shift(q) - 1.0
    r2 = px.shift(q) / px.shift(2 * q) - 1.0
    r3 = px.shift(2 * q) / px.shift(3 * q) - 1.0
    r4 = px.shift(3 * q) / px.shift(4 * q) - 1.0
    ibd = 2 * r1 + r2 + r3 + r4               # most-recent quarter double weight
    sig["ibd_rs_rank"] = ibd.rank(axis=1, pct=True)   # daily cross-sectional pct
    return sig


def longshort_returns(signal: pd.DataFrame, fwd: pd.DataFrame,
                      rebal: int = REBAL) -> pd.Series:
    """Weekly-rebalanced quintile long-short, equal weight, turnover-costed.

    signal_t (close of t) -> hold from t+1. fwd is 1-day fwd stock return
    (close_t -> close_{t+1}). We rebalance every `rebal` days.
    """
    dates = signal.index
    rebal_days = set(dates[::rebal])
    weights = pd.DataFrame(0.0, index=dates, columns=signal.columns)
    cur = pd.Series(0.0, index=signal.columns)
    for d in dates:
        if d in rebal_days:
            row = signal.loc[d].dropna()
            if len(row) >= 10:
                lo = row.quantile(QUANTILE)
                hi = row.quantile(1 - QUANTILE)
                longs = row[row >= hi].index
                shorts = row[row <= lo].index
                cur = pd.Series(0.0, index=signal.columns)
                if len(longs):
                    cur[longs] = 1.0 / len(longs)
                if len(shorts):
                    cur[shorts] = -1.0 / len(shorts)
        weights.loc[d] = cur
    # position on day t earns fwd return t (close t -> close t+1); trade at t+1 open
    # approximate: shift weights by 1 so signal formed at t is applied to next return
    w = weights.shift(1).fillna(0.0)
    gross = (w * fwd).sum(axis=1)
    turnover = w.diff().abs().sum(axis=1).fillna(0.0)
    net = gross - turnover * COST_BPS / 1e4
    return net


def perm_pvalue(signal: pd.DataFrame, fwd: pd.DataFrame, mask: pd.Series,
                actual_sharpe: float, n: int = N_PERM) -> float:
    """Shuffle each day's signal across stocks (preserve #long/#short), recompute."""
    if not np.isfinite(actual_sharpe):
        return np.nan
    cols = signal.columns
    null = np.empty(n)
    sig_vals = signal.values
    for i in range(n):
        permuted = np.empty_like(sig_vals)
        for r in range(sig_vals.shape[0]):
            row = sig_vals[r]
            finite = np.isfinite(row)
            perm = row.copy()
            idx = np.where(finite)[0]
            perm[idx] = row[idx][RNG.permutation(idx.size)]
            permuted[r] = perm
        ps = pd.DataFrame(permuted, index=signal.index, columns=cols)
        r_perm = longshort_returns(ps, fwd)
        null[i] = sharpe(r_perm[mask])
    return float((np.nan_to_num(null, nan=-np.inf) >= actual_sharpe).mean())


def rank_ic(signal: pd.DataFrame, fwd_period: pd.DataFrame, mask: pd.Series) -> float:
    """Mean daily Spearman IC of signal_t vs next-period stock return, on mask days."""
    ics = []
    for d in signal.index[mask.values]:
        s = signal.loc[d]
        f = fwd_period.loc[d]
        df = pd.concat([s, f], axis=1).dropna()
        if len(df) >= 10:
            ics.append(df.iloc[:, 0].corr(df.iloc[:, 1], method="spearman"))
    return float(np.nanmean(ics)) if ics else np.nan


def main():
    OUTDIR.mkdir(parents=True, exist_ok=True)
    px = load_prices()
    ix = load_index()
    common = px.index.intersection(ix.index)
    px, ix = px.loc[common], ix.loc[common]
    print(f"universe: {px.shape[1]} liquid stocks, {px.shape[0]} days "
          f"{px.index.min()} -> {px.index.max()}")

    fwd = px.pct_change(fill_method=None).shift(-1)          # close_t -> close_{t+1}
    fwd5 = px.pct_change(REBAL, fill_method=None).shift(-REBAL)  # weekly fwd for IC

    signals = build_signals(px, ix)

    # regime: index > rising MA200
    ma200 = ix.rolling(200, min_periods=200).mean()
    bull = (ix > ma200) & (ma200 > ma200.shift(20))
    bull = bull.reindex(px.index).fillna(False)

    # chronological IS/OOS
    n = len(px.index)
    cut = int(n * 0.7)
    is_mask = pd.Series(np.arange(n) < cut, index=px.index)
    oos_mask = pd.Series(np.arange(n) >= cut, index=px.index)

    champ = champion_returns().reindex(px.index)

    # own-momentum long-short (12-1 on absolute price) for decorrelation
    own_mom = px.shift(21) / px.shift(252) - 1.0
    mom_ret = longshort_returns(own_mom, fwd)

    rows = []
    ls_returns = {}
    for name, sig in signals.items():
        ret = longshort_returns(sig, fwd)
        ls_returns[name] = ret
        is_s = sharpe(ret[is_mask.values])
        oos_s = sharpe(ret[oos_mask.values])
        bull_s = sharpe(ret[bull.values])
        bear_s = sharpe(ret[(~bull).values])
        ic_is = rank_ic(sig, fwd5, is_mask)
        ic_oos = rank_ic(sig, fwd5, oos_mask)
        c_mom = pd.concat([ret, mom_ret], axis=1).dropna().corr().iloc[0, 1]
        c_champ = pd.concat([ret, champ], axis=1).dropna().corr().iloc[0, 1]
        rows.append(dict(signal=name, IC_IS=ic_is, IC_OOS=ic_oos,
                         Sharpe_IS=is_s, Sharpe_OOS=oos_s,
                         Sharpe_bull=bull_s, Sharpe_bear=bear_s,
                         corr_own_mom=c_mom, corr_champion=c_champ))

    res = pd.DataFrame(rows).sort_values("Sharpe_OOS", ascending=False)
    bh = sharpe(fwd.mean(axis=1))               # equal-weight universe B&H
    print(f"\nEqual-weight universe B&H Sharpe (full) = {bh:+.2f}")
    print(f"own 12-1 momentum L/S  OOS Sharpe = {sharpe(mom_ret[oos_mask.values]):+.2f}")
    pd.set_option("display.float_format", lambda x: f"{x:+.3f}" if isinstance(x, float) else str(x))
    print("\n=== RS operationalizations (cross-sectional quintile L/S) ===")
    print(res.to_string(index=False))

    # permutation only on the best OOS signal (expensive)
    best = res.iloc[0]["signal"]
    actual = res.iloc[0]["Sharpe_OOS"]
    print(f"\npermutation test on best OOS signal '{best}' "
          f"(OOS Sharpe {actual:+.2f}, {N_PERM} shuffles)...")
    pv = perm_pvalue(signals[best], fwd, oos_mask, actual)
    print(f"  OOS permutation p = {pv:.3f}  "
          f"({'NOT ' if pd.isna(pv) or pv > 0.05 else ''}significant at 0.05)")
    res.loc[res["signal"] == best, "OOS_perm_p"] = pv

    out = OUTDIR / "relative_strength_metrics.csv"
    res.to_csv(out, index=False)
    print(f"\n-> {out}")
    return res


if __name__ == "__main__":
    main()
