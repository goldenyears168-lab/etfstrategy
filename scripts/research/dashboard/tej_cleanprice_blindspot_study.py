"""TEJ clean-price blind-spot study — attack the project's dividend-adjustment /
survivorship blind spot with TEJ EWPRCD fully back-adjusted (還原) prices.

TWO QUESTIONS
  Q1 (blind-spot quantification): How large is the dividend-adjustment gap, and is
     stocks.db's finmind `adj_close` actually complete? Compare, per stock:
       - TEJ close_adj daily returns  vs  stocks.db adj_close daily returns
       - TEJ close_adj daily returns  vs  stocks.db RAW close daily returns
       - the cumulative adjustment magnitude (adjfac 2021 -> 2026), esp. financials.
     If finmind adj_close matches TEJ close_adj, the blind spot is ALREADY closed for
     the liquid universe; if it matches RAW close, the RS study was on unadjusted data.

  Q2 (does the RS conclusion change on clean prices?): Rebuild the 4 RS
     operationalizations from the relative_strength study on TEJ close_adj and rerun
     the same cross-sectional quintile long-short + IS/OOS + permutation + regime +
     champion-decorrelation gauntlet. Compare to the finmind-based numbers.

DATA
  - TEJ adjusted:  data/research/dashboard/tej_cleanprice_data.parquet (2021+, 90 stk)
  - finmind:       data/stocks.db -> stock_daily_bars (close, adj_close)
  - TAIEX:         data/stocks.db -> daily_bars where code='IX0001'
  - champion:      data/research/chip_macro/panel.parquet (fut_foreign_oi z60>0)

HONEST COVERAGE: TEJ plan data starts 2021-01, so the clean-price RS rerun window is
2021-01 -> 2026-07 (a shorter, mostly-bull window) vs the finmind study's 2018-06 start.
Non-investment-advice: research code only.
"""
from __future__ import annotations
import sqlite3
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
DB = ROOT / "data" / "stocks.db"
TEJ = ROOT / "data" / "research" / "dashboard" / "tej_cleanprice_data.parquet"
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
OUTDIR = ROOT / "reports" / "research" / "dashboard-completeness"

START = "2021-01-04"
REBAL = 5
COST_BPS = 4.0
QUANTILE = 0.20
ANN = np.sqrt(252)
RNG = np.random.default_rng(7)
N_PERM = 1000

FINANCIALS = {'2882','2881','2891','2884','2886','2883','1216'}  # note 1216 not fin; set below
FIN = {'2882','2881','2891','2884','2886','2883','2887','2892','2890','2880',
       '2801','5871','5880','5876','2885','2834'}


def sharpe(x):
    x = pd.Series(x).dropna()
    return float(x.mean()/x.std()*ANN) if len(x) > 20 and x.std() > 0 else np.nan


# ----------------------------- data loaders --------------------------------------
def load_tej():
    t = pd.read_parquet(TEJ)
    adj = t.pivot_table(index="date", columns="stock_id", values="close_adj").sort_index()
    raw = t.pivot_table(index="date", columns="stock_id", values="close_d").sort_index()
    return adj, raw


def load_finmind(cols, start=START):
    con = sqlite3.connect(DB)
    ph = ",".join("?"*len(cols))
    df = pd.read_sql_query(
        f"""SELECT trade_date AS date, stock_id, close, adj_close
            FROM stock_daily_bars
            WHERE source='finmind' AND trade_date>=? AND stock_id IN ({ph})""",
        con, params=[start, *cols])
    con.close()
    fadj = df.pivot_table(index="date", columns="stock_id", values="adj_close").sort_index()
    fraw = df.pivot_table(index="date", columns="stock_id", values="close").sort_index()
    return fadj, fraw


def load_index():
    con = sqlite3.connect(DB)
    ix = pd.read_sql_query(
        "SELECT date, close FROM daily_bars WHERE code='IX0001' AND date>=? ORDER BY date",
        con, params=(START,))
    con.close()
    return ix.drop_duplicates("date", keep="last").set_index("date")["close"]


def champion_returns():
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    z = (p["fut_foreign_oi"]-p["fut_foreign_oi"].rolling(60).mean())/p["fut_foreign_oi"].rolling(60).std()
    bull = (z > 0).astype(float)
    day_ret = (p["ix_close"]/p["ix_open"]-1.0).shift(-1)
    r = bull*day_ret - bull.diff().abs().fillna(bull.abs())*COST_BPS/1e4
    return pd.Series(r.values, index=p["date"].astype(str))


# ----------------------------- RS signals (identical to finmind study) -----------
def build_signals(px, ix):
    ix = ix.reindex(px.index).ffill()
    rs_line = px.div(ix, axis=0)
    sig = {}
    sma = rs_line.rolling(50, min_periods=50).mean()
    sig["mansfield_rsm50"] = (rs_line/sma-1.0)*100.0
    sig["rs_mom_252_21"] = rs_line.shift(21)/rs_line.shift(252)-1.0
    roll_max = px.rolling(252, min_periods=200).max()
    sig["gh_52w_high"] = px/roll_max
    q = 63
    r1 = px/px.shift(q)-1.0; r2 = px.shift(q)/px.shift(2*q)-1.0
    r3 = px.shift(2*q)/px.shift(3*q)-1.0; r4 = px.shift(3*q)/px.shift(4*q)-1.0
    sig["ibd_rs_rank"] = (2*r1+r2+r3+r4).rank(axis=1, pct=True)
    return sig


def longshort_returns(signal, fwd, rebal=REBAL):
    dates = signal.index
    rebal_days = set(dates[::rebal])
    weights = pd.DataFrame(0.0, index=dates, columns=signal.columns)
    cur = pd.Series(0.0, index=signal.columns)
    for d in dates:
        if d in rebal_days:
            row = signal.loc[d].dropna()
            if len(row) >= 10:
                lo = row.quantile(QUANTILE); hi = row.quantile(1-QUANTILE)
                longs = row[row >= hi].index; shorts = row[row <= lo].index
                cur = pd.Series(0.0, index=signal.columns)
                if len(longs): cur[longs] = 1.0/len(longs)
                if len(shorts): cur[shorts] = -1.0/len(shorts)
        weights.loc[d] = cur
    w = weights.shift(1).fillna(0.0)
    gross = (w*fwd).sum(axis=1)
    turnover = w.diff().abs().sum(axis=1).fillna(0.0)
    return gross - turnover*COST_BPS/1e4


def perm_pvalue(signal, fwd, mask, actual, n=N_PERM):
    if not np.isfinite(actual): return np.nan
    cols = signal.columns; null = np.empty(n); sv = signal.values
    for i in range(n):
        pm = np.empty_like(sv)
        for r in range(sv.shape[0]):
            row = sv[r]; idx = np.where(np.isfinite(row))[0]; perm = row.copy()
            perm[idx] = row[idx][RNG.permutation(idx.size)]; pm[r] = perm
        ps = pd.DataFrame(pm, index=signal.index, columns=cols)
        null[i] = sharpe(longshort_returns(ps, fwd)[mask])
    return float((np.nan_to_num(null, nan=-np.inf) >= actual).mean())


def rank_ic(signal, fwd_p, mask):
    ics = []
    for d in signal.index[mask.values]:
        df = pd.concat([signal.loc[d], fwd_p.loc[d]], axis=1).dropna()
        if len(df) >= 10:
            ics.append(df.iloc[:, 0].corr(df.iloc[:, 1], method="spearman"))
    return float(np.nanmean(ics)) if ics else np.nan


# ----------------------------- Q1: blind-spot quantification ----------------------
def q1_blindspot(tej_adj, tej_raw, fin_adj, fin_raw):
    rows = []
    for s in tej_adj.columns:
        if s not in fin_adj.columns: continue
        common = tej_adj.index.intersection(fin_adj.index)
        ta = tej_adj[s].reindex(common); tr = tej_raw[s].reindex(common)
        fa = fin_adj[s].reindex(common); fr = fin_raw[s].reindex(common)
        # cumulative dividend adjustment magnitude from TEJ
        af = (ta/tr).dropna()
        cum_adj = float(af.iloc[-1]/af.iloc[0]-1.0) if len(af) > 20 else np.nan
        # daily returns
        tar = ta.pct_change(); tarr = tr.pct_change()
        far = fa.pct_change(); frr = fr.pct_change()
        d = pd.DataFrame({'ta':tar,'tr':tarr,'fa':far,'fr':frr}).dropna()
        if len(d) < 50: continue
        rows.append(dict(
            stock_id=s, is_fin=s in FIN, n=len(d),
            cum_div_adj_pct=cum_adj*100 if cum_adj==cum_adj else np.nan,
            # does finmind adj match TEJ adj (returns)?  1.0 = perfectly adjusted
            corr_finadj_tejadj=d['fa'].corr(d['ta']),
            # does finmind adj instead look like RAW (unadjusted)?
            corr_finadj_tejraw=d['fa'].corr(d['tr']),
            # mean abs daily-return diff finmind-adj vs TEJ-adj (bps)
            mad_finadj_vs_tejadj_bps=(d['fa']-d['ta']).abs().mean()*1e4,
            # raw-price sanity: finmind raw vs TEJ raw
            corr_finraw_tejraw=d['fr'].corr(d['tr']),
        ))
    return pd.DataFrame(rows)


def run_rs(px, ix, tag):
    common = px.index.intersection(ix.index)
    px, ix = px.loc[common], ix.loc[common]
    fwd = px.pct_change(fill_method=None).shift(-1)
    fwd5 = px.pct_change(REBAL, fill_method=None).shift(-REBAL)
    signals = build_signals(px, ix)
    ma200 = ix.rolling(200, min_periods=200).mean()
    bull = ((ix > ma200) & (ma200 > ma200.shift(20))).reindex(px.index).fillna(False)
    n = len(px.index); cut = int(n*0.7)
    is_m = pd.Series(np.arange(n) < cut, index=px.index)
    oos_m = pd.Series(np.arange(n) >= cut, index=px.index)
    champ = champion_returns().reindex(px.index)
    own_mom = px.shift(21)/px.shift(252)-1.0
    mom_ret = longshort_returns(own_mom, fwd)
    rows = []; lsr = {}
    for name, sig in signals.items():
        ret = longshort_returns(sig, fwd); lsr[name] = ret
        rows.append(dict(signal=name, tag=tag,
            IC_IS=rank_ic(sig, fwd5, is_m), IC_OOS=rank_ic(sig, fwd5, oos_m),
            Sharpe_IS=sharpe(ret[is_m.values]), Sharpe_OOS=sharpe(ret[oos_m.values]),
            Sharpe_bull=sharpe(ret[bull.values]), Sharpe_bear=sharpe(ret[(~bull).values]),
            corr_own_mom=pd.concat([ret,mom_ret],axis=1).dropna().corr().iloc[0,1],
            corr_champion=pd.concat([ret,champ],axis=1).dropna().corr().iloc[0,1]))
    res = pd.DataFrame(rows).sort_values("Sharpe_OOS", ascending=False)
    best = res.iloc[0]["signal"]; actual = res.iloc[0]["Sharpe_OOS"]
    pv = perm_pvalue(signals[best], fwd, oos_m, actual)
    res.loc[res["signal"] == best, "OOS_perm_p"] = pv
    bh = sharpe(fwd.mean(axis=1))
    return res, bh, best, pv


def main():
    OUTDIR.mkdir(parents=True, exist_ok=True)
    tej_adj, tej_raw = load_tej()
    ix = load_index()
    fin_adj, fin_raw = load_finmind(list(tej_adj.columns))

    print("="*70); print("Q1  DIVIDEND-ADJUSTMENT BLIND-SPOT QUANTIFICATION"); print("="*70)
    q1 = q1_blindspot(tej_adj, tej_raw, fin_adj, fin_raw)
    q1.to_csv(OUTDIR/"tej_cleanprice_q1_metrics.csv", index=False)
    pd.set_option("display.float_format", lambda x: f"{x:+.4f}" if isinstance(x,float) else str(x))
    print(f"\nstocks compared: {len(q1)}  (financials: {q1['is_fin'].sum()})")
    print(f"cumulative div-adjustment 2021->2026 (%): median={q1['cum_div_adj_pct'].median():.1f} "
          f"mean={q1['cum_div_adj_pct'].mean():.1f} max={q1['cum_div_adj_pct'].max():.1f}")
    print(f"  financials  median={q1[q1.is_fin]['cum_div_adj_pct'].median():.1f}")
    print(f"  non-fin     median={q1[~q1.is_fin]['cum_div_adj_pct'].median():.1f}")
    print(f"\nfinmind adj_close vs TEJ adj return corr:  median={q1['corr_finadj_tejadj'].median():.5f}")
    print(f"finmind adj_close vs TEJ RAW return corr:  median={q1['corr_finadj_tejraw'].median():.5f}")
    print(f"MAD(finmind-adj , TEJ-adj) daily-ret bps:  median={q1['mad_finadj_vs_tejadj_bps'].median():.3f} "
          f"max={q1['mad_finadj_vs_tejadj_bps'].max():.3f}")
    print(f"finmind RAW vs TEJ RAW return corr (sanity): median={q1['corr_finraw_tejraw'].median():.5f}")
    print("\nTop cumulative-adjustment names (dividend gap most distorting):")
    print(q1.nlargest(8,'cum_div_adj_pct')[['stock_id','is_fin','cum_div_adj_pct',
          'corr_finadj_tejadj','mad_finadj_vs_tejadj_bps']].to_string(index=False))
    print("\nWorst finmind-adj mismatch names (largest MAD vs TEJ adj):")
    print(q1.nlargest(8,'mad_finadj_vs_tejadj_bps')[['stock_id','is_fin','cum_div_adj_pct',
          'corr_finadj_tejadj','corr_finadj_tejraw','mad_finadj_vs_tejadj_bps']].to_string(index=False))

    print("\n"+"="*70); print("Q2  RS RERUN ON CLEAN TEJ ADJUSTED PRICES (2021+)"); print("="*70)
    res_tej, bh_tej, best_t, pv_t = run_rs(tej_adj, ix, "TEJ_adj")
    print(f"\n[TEJ close_adj] universe {tej_adj.shape[1]} stk, B&H Sharpe(full)={bh_tej:+.2f}")
    print(res_tej.to_string(index=False))
    print(f"best OOS signal '{best_t}' permutation p = {pv_t:.3f}")

    # same window on finmind adj + finmind raw, to isolate the price-source effect
    res_fadj, bh_fa, _, _ = run_rs(fin_adj.reindex(columns=tej_adj.columns), ix, "finmind_adj")
    res_fraw, bh_fr, _, _ = run_rs(fin_raw.reindex(columns=tej_adj.columns), ix, "finmind_raw")
    print(f"\n[finmind adj_close, SAME 2021+ window] B&H={bh_fa:+.2f}")
    print(res_fadj.to_string(index=False))
    print(f"\n[finmind RAW close, SAME 2021+ window] B&H={bh_fr:+.2f}")
    print(res_fraw.to_string(index=False))

    allres = pd.concat([res_tej, res_fadj, res_fraw], ignore_index=True)
    allres.to_csv(OUTDIR/"tej_cleanprice_rs_metrics.csv", index=False)
    print(f"\n-> {OUTDIR/'tej_cleanprice_q1_metrics.csv'}")
    print(f"-> {OUTDIR/'tej_cleanprice_rs_metrics.csv'}")


if __name__ == "__main__":
    main()
