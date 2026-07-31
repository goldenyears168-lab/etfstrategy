"""Options-microstructure chip dimension — IS/OOS falsification study + scaffold.

Dimension (選擇權籌碼): four candidate market-timing signals for the TAIEX
observation system, benchmarked against the project champion (foreign TAIEX-
futures positioning `fut_foreign_oi_z60 > 0`, chip×bull OOS +1.79/+2.49):

  S1  外資選擇權淨部位   foreign_opt_net  (LEADING candidate — like champion)
        = (call long_oi - call short_oi) - (put long_oi - put short_oi)
          for institutional_investors == 外資 on TXO.  z60. bullish when > 0.
  S2  小台散戶多空比     mtx_retail_ratio (反指標, LAGGING/擁擠度前兆)
        = -1 * (法人MTX淨OI) / (MTX 全市場未平倉)   ; positive = 散戶偏多.
          contrarian: extreme-high z -> de-risk.  direction fixed from IS IC.
  S3  P/C ratio (OI)     pcr_oi           (同步偏落後 情緒反指標)  [REAL, full history]
        = sum(put OI) / sum(call OI) on TXO.  contrarian at extremes.
  S4  Max Pain 偏離      maxpain_dev      (L3 到期週 微結構)        [REAL, full history]
        = (close - MaxPain)/MaxPain , mean-reversion, pin effect.

DATA SOURCE (all four are NOT in local stocks.db / panel.parquet — options are
absent from the local DB; this account's FinMind DOES return them, verified):
  - TaiwanOptionInstitutionalInvestors  data_id=TXO   -> S1  (cheap, ~1.5k rows/yr)
  - TaiwanFuturesInstitutionalInvestors data_id=MTX   -> S2 numerator (cheap)
  - TaiwanFuturesDaily                  data_id=MTX   -> S2 denominator (cheap)
  - TaiwanOptionDaily                   data_id=TXO   -> S3/S4 (HEAVY ~12k rows/day)
Champion / regime / forward-return substrate:
  - data/research/chip_macro/panel.parquet  (ix_open/close, fut_foreign_oi, ...)

HOW TO RUN
  cd /Users/jackm4/goldenstocks && source .venv/bin/activate
  python scripts/research/dashboard/options_micro_study.py            # S1+S2 real study (fetches+caches)
  python scripts/research/dashboard/options_micro_study.py --pcr-demo # + prove S3/S4 fetch on a few days
Caches raw fetches to data/research/dashboard/*.parquet so re-runs are offline.

METHODOLOGY (project standard, falsification-first)
  - forward return: fwd(t) = ix_close[t+1]/ix_open[t+1] - 1   (signal known post-close,
    earliest tradable = next open).  cost 4 bps per position change.
  - direction of each signal FIXED from IN-SAMPLE IC sign only (no OOS peek).
  - IS/OOS 70/30 chronological split.  permutation vs same-exposure random timing.
  - regime-conditioning: also report edge restricted to bull (ix_close>MA200, MA200 up).
  - champion overlay: corr of daily strat returns vs champion; 50/50 combo OOS Sharpe.
Deflated-Sharpe: report the trial count so DSR can be applied (kept honest — a single
market series + few signals is thin OOS; treat any positive as a weak prior only).

非投資建議 — research scaffold only.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
CACHE = ROOT / "data" / "research" / "dashboard"
CACHE.mkdir(parents=True, exist_ok=True)
OUT = ROOT / "reports" / "research" / "dashboard-completeness"

COST_BPS = 4.0
ANN = np.sqrt(252)
RNG = np.random.default_rng(7)
START = date(2018, 6, 1)
END = date.today()


# ---------------------------------------------------------------- helpers ----
def rz(s: pd.Series, w: int) -> pd.Series:
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def sharpe(x) -> float:
    x = pd.Series(x).dropna()
    return x.mean() / x.std() * ANN if len(x) > 20 and x.std() > 0 else np.nan


def lf(bull, day_ret, cost=COST_BPS):
    pos = pd.Series(bull, index=day_ret.index).astype(float).fillna(0.0)
    return pos * day_ret - pos.diff().abs().fillna(pos.abs()) * (cost / 1e4)


def per_period_sharpe(x) -> float:
    x = pd.Series(x).dropna()
    return x.mean() / x.std() if len(x) > 20 and x.std() > 0 else np.nan


def deflated_sharpe(strat_ret, n_trials, sr_trials):
    """Deflated Sharpe (Bailey & Lopez de Prado). Non-annualised, per-period.

    strat_ret : the (OOS) strategy return series to test.
    n_trials  : number of trials in the search (haircut grows with N).
    sr_trials : per-period Sharpe of every trial (for cross-trial variance).
    Returns (DSR prob >0, SR0 haircut, T).
    """
    from scipy import stats

    r = pd.Series(strat_ret).dropna()
    T = len(r)
    if T < 30 or r.std() == 0:
        return np.nan, np.nan, T
    sr = r.mean() / r.std()
    sk = float(stats.skew(r)); ku = float(stats.kurtosis(r, fisher=False))  # Pearson kurt
    sr_arr = np.array([s for s in sr_trials if pd.notna(s)])
    sig_sr = sr_arr.std(ddof=1) if len(sr_arr) > 1 else abs(sr) * 0.5
    g = 0.5772156649
    N = max(int(n_trials), 2)
    z1 = stats.norm.ppf(1 - 1.0 / N)
    z2 = stats.norm.ppf(1 - 1.0 / (N * np.e))
    sr0 = sig_sr * ((1 - g) * z1 + g * z2)
    denom = np.sqrt(max(1 - sk * sr + (ku - 1) / 4.0 * sr * sr, 1e-9))
    dsr = float(stats.norm.cdf((sr - sr0) * np.sqrt(T - 1) / denom))
    return dsr, float(sr0), T


def residual_ic(raw_sig, p, day_ret, mask):
    """IS IC of the signal on forward return AFTER regressing out champion
    (fut_foreign_oi_z60) and 20-day price momentum. Tests 'momentum disguise'."""
    mom = p["ix_close"] / p["ix_close"].shift(20) - 1.0
    champ = rz(p["fut_foreign_oi"], 60)
    df = pd.DataFrame({"sig": raw_sig, "f": day_ret, "champ": champ, "mom": mom}).dropna()
    df = df[mask.reindex(df.index).fillna(False)]
    if len(df) < 100:
        return np.nan, np.nan
    raw_ic = df["sig"].corr(df["f"])
    X = np.column_stack([np.ones(len(df)), df["champ"].values, df["mom"].values])
    # residualise both signal and forward return on [champ, mom]
    bs = np.linalg.lstsq(X, df["sig"].values, rcond=None)[0]
    bf = np.linalg.lstsq(X, df["f"].values, rcond=None)[0]
    rs = df["sig"].values - X @ bs
    rf = df["f"].values - X @ bf
    res_ic = float(np.corrcoef(rs, rf)[0, 1])
    return round(float(raw_ic), 4), round(res_ic, 4)


def perm_p(bull, day_ret, mask, n=2000):
    dr = day_ret[mask].reset_index(drop=True)
    pos = pd.Series(bull, index=day_ret.index)[mask].astype(float).reset_index(drop=True)
    v = dr.notna(); dr, pos = dr[v], pos[v]
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


# --------------------------------------------------------------- fetchers ----
def _cached(name: str, builder):
    fp = CACHE / f"{name}.parquet"
    if fp.exists():
        return pd.read_parquet(fp)
    df = builder()
    df.to_parquet(fp, index=False)
    return df


def fetch_foreign_opt_net() -> pd.DataFrame:
    """S1: TaiwanOptionInstitutionalInvestors TXO -> foreign net bullish OI per day."""
    from finmind_client import fetch_finmind

    frames = []
    for yr in range(START.year, END.year + 1):
        s = max(START, date(yr, 1, 1)); e = min(END, date(yr, 12, 31))
        raw = fetch_finmind("TaiwanOptionInstitutionalInvestors", "TXO", s, e, timeout=120)
        if raw:
            frames.append(pd.DataFrame(raw))
    d = pd.concat(frames, ignore_index=True)
    d = d[d["institutional_investors"] == "外資"].copy()
    d["net_oi"] = (d["long_open_interest_balance_volume"]
                   - d["short_open_interest_balance_volume"])
    piv = d.pivot_table(index="date", columns="call_put", values="net_oi", aggfunc="sum")
    # bullish structure = net long calls  -  net long puts
    call = piv.get("買權", piv.get("call", 0))
    put = piv.get("賣權", piv.get("put", 0))
    out = pd.DataFrame({"date": piv.index, "foreign_opt_net": (call - put).values})
    out["date"] = out["date"].astype(str).str[:10]
    return out.reset_index(drop=True)


def _max_pain_for(sub: pd.DataFrame) -> float:
    """Max-pain strike for one date's most-liquid contract (by total OI)."""
    if sub.empty:
        return np.nan
    tot = sub.groupby("contract_date")["open_interest"].sum()
    if tot.empty or tot.max() == 0:
        return np.nan
    d = sub[sub["contract_date"] == tot.idxmax()]
    calls = d[d["call_put"].isin(["買權", "call"])]
    puts = d[d["call_put"].isin(["賣權", "put"])]
    strikes = np.sort(d["strike_price"].astype(float).unique())
    if len(strikes) == 0:
        return np.nan
    ck = calls["strike_price"].astype(float).values; coi = calls["open_interest"].astype(float).values
    pk = puts["strike_price"].astype(float).values; poi = puts["open_interest"].astype(float).values
    pains = [(np.maximum(K - ck, 0) * coi).sum() + (np.maximum(pk - K, 0) * poi).sum()
             for K in strikes]
    return float(strikes[int(np.argmin(pains))])


def fetch_pcr_maxpain() -> pd.DataFrame:
    """S3/S4: TaiwanOptionDaily TXO -> daily PCR_OI, PCR_vol, MaxPain (year loop)."""
    from finmind_client import fetch_finmind

    frames = []
    for yr in range(START.year, END.year + 1):
        s = max(START, date(yr, 1, 1)); e = min(END, date(yr, 12, 31))
        raw = fetch_finmind("TaiwanOptionDaily", "TXO", s, e, timeout=600)
        if not raw:
            continue
        df = pd.DataFrame(raw)
        df = df[df["trading_session"] == "position"].copy()
        df["open_interest"] = pd.to_numeric(df["open_interest"], errors="coerce").fillna(0)
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
        g = df.groupby(["date", "call_put"]).agg(
            oi=("open_interest", "sum"), vol=("volume", "sum")).reset_index()
        po = g.pivot(index="date", columns="call_put", values="oi")
        pv = g.pivot(index="date", columns="call_put", values="vol")
        call_oi = po.get("買權", po.get("call")); put_oi = po.get("賣權", po.get("put"))
        call_v = pv.get("買權", pv.get("call")); put_v = pv.get("賣權", pv.get("put"))
        day = pd.DataFrame(index=po.index)
        day["pcr_oi"] = put_oi / call_oi.replace(0, np.nan)
        day["pcr_vol"] = put_v / call_v.replace(0, np.nan)
        day["maxpain"] = df.groupby("date").apply(_max_pain_for, include_groups=False)
        frames.append(day.reset_index())
    out = pd.concat(frames, ignore_index=True)
    out["date"] = out["date"].astype(str).str[:10]
    return out.drop_duplicates("date").sort_values("date").reset_index(drop=True)


def fetch_mtx_retail_ratio() -> pd.DataFrame:
    """S2: 小台散戶多空比 = -(法人MTX淨OI)/(MTX 全市場未平倉)."""
    from finmind_client import fetch_finmind

    inst_f, day_f = [], []
    for yr in range(START.year, END.year + 1):
        s = max(START, date(yr, 1, 1)); e = min(END, date(yr, 12, 31))
        ri = fetch_finmind("TaiwanFuturesInstitutionalInvestors", "MTX", s, e, timeout=120)
        rd = fetch_finmind("TaiwanFuturesDaily", "MTX", s, e, timeout=180)
        if ri:
            inst_f.append(pd.DataFrame(ri))
        if rd:
            day_f.append(pd.DataFrame(rd))
    di = pd.concat(inst_f, ignore_index=True)
    di["net_oi"] = (di["long_open_interest_balance_volume"]
                    - di["short_open_interest_balance_volume"])
    inst_net = di.groupby("date")["net_oi"].sum().rename("inst_net_oi")

    dd = pd.concat(day_f, ignore_index=True)
    dd = dd[dd["trading_session"] == "position"].copy()
    # single-month contracts only (exclude calendar spreads like '202401/202402')
    dd = dd[~dd["contract_date"].astype(str).str.contains("/")]
    tot = dd.groupby("date")["open_interest"].sum().rename("mkt_oi")

    m = pd.concat([inst_net, tot], axis=1).dropna()
    m["mtx_retail_ratio"] = -1.0 * m["inst_net_oi"] / m["mkt_oi"].replace(0, np.nan)
    out = m.reset_index()[["date", "mtx_retail_ratio"]]
    out["date"] = out["date"].astype(str).str[:10]
    return out


# ------------------------------------------- SCAFFOLD: heavy S3/S4 (TXO) ------
def fetch_option_daily_day(d: date):
    """One trading day of strike-level TXO (SCAFFOLD: heavy ~12k rows/day)."""
    from finmind_client import fetch_finmind

    return pd.DataFrame(fetch_finmind("TaiwanOptionDaily", "TXO", d, d, timeout=180))


def pcr_oi_from_day(df: pd.DataFrame) -> float:
    """S3: P/C ratio (open interest) = sum(put OI)/sum(call OI) for the day."""
    if df.empty:
        return np.nan
    g = df.groupby("call_put")["open_interest"].sum()
    put = g.get("賣權", g.get("put", np.nan)); call = g.get("買權", g.get("call", np.nan))
    return float(put / call) if call else np.nan


def max_pain_from_day(df: pd.DataFrame, contract: str | None = None) -> float:
    """S4: Max Pain strike = K minimising total writer payoff over OI.

    payoff(K) = sum_calls OI*max(K-strike,0) + sum_puts OI*max(strike-K,0).
    """
    if df.empty:
        return np.nan
    d = df.copy()
    if contract is None:  # nearest-expiry front month = most rows / liquidity
        contract = d["contract_date"].value_counts().idxmax()
    d = d[d["contract_date"] == contract]
    calls = d[d["call_put"].isin(["買權", "call"])]
    puts = d[d["call_put"].isin(["賣權", "put"])]
    strikes = np.sort(d["strike_price"].astype(float).unique())
    if len(strikes) == 0:
        return np.nan
    ck = calls["strike_price"].astype(float).values; coi = calls["open_interest"].values
    pk = puts["strike_price"].astype(float).values; poi = puts["open_interest"].values
    pains = [
        (np.maximum(K - ck, 0) * coi).sum() + (np.maximum(pk - K, 0) * poi).sum()
        for K in strikes
    ]
    return float(strikes[int(np.argmin(pains))])


# ------------------------------------------------------------------ study ----
def bull_regime(p: pd.DataFrame) -> pd.Series:
    ma = p["ix_close"].rolling(200, min_periods=200).mean()
    return (p["ix_close"] > ma) & (ma > ma.shift(20))


def eval_signal(name, raw_val, p, day_ret, ismask, oos, champ_bull, champ_ret, contrarian):
    v = pd.Series(raw_val, index=p.index)
    d = pd.DataFrame({"v": v, "f": day_ret}).dropna()
    if len(d) < 100:
        return None
    im = ismask.reindex(d.index).fillna(False)
    ic_is = d[im]["v"].corr(d[im]["f"])
    direction = np.sign(ic_is) if pd.notna(ic_is) and ic_is != 0 else 1.0
    bull = (direction * v) > 0
    r = lf(bull, day_ret)
    oos_shp = sharpe(r[oos])
    corr_champ = pd.DataFrame({"a": r, "b": champ_ret}).dropna().corr().iloc[0, 1]
    # regime-conditioned OOS
    breg = bull_regime(p)
    r_bull = lf(bull & breg, day_ret)
    _, pv = perm_p(bull, day_ret, oos)
    comb = (champ_bull.astype(float) + bull.astype(float)) / 2
    cr = comb * day_ret - comb.diff().abs().fillna(comb.abs()) * COST_BPS / 1e4
    return {
        "signal": name, "contrarian_prior": contrarian, "dir_from_IS": int(direction),
        "IC_IS": round(float(ic_is), 4), "OOS_Sharpe": round(float(oos_shp), 3),
        "OOS_bull_only": round(float(sharpe(r_bull[oos])), 3),
        "OOS_perm_p": None if pd.isna(pv) else round(pv, 3),
        "corr_vs_champ": round(float(corr_champ), 3),
        "combo50_OOS": round(float(sharpe(cr[oos])), 3),
        "exposure": round(float(bull.reindex(day_ret.index).fillna(False).mean()), 3),
        "n": int(len(d)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pcr-demo", action="store_true",
                    help="also fetch a few TXO days to prove S3/S4 compute")
    args = ap.parse_args()

    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    p["date"] = p["date"].astype(str).str[:10]
    day_ret = (p["ix_close"] / p["ix_open"] - 1.0).shift(-1)
    n = len(p); is_cut = int(n * 0.7)
    ismask = pd.Series(p.index < is_cut, index=p.index)
    oos = pd.Series(p.index >= is_cut, index=p.index)
    champ_bull = rz(p["fut_foreign_oi"], 60) > 0
    champ_ret = lf(champ_bull, day_ret)

    print(f"panel {p['date'].iloc[0]}..{p['date'].iloc[-1]} n={n}  IS/OOS cut @ {p['date'].iloc[is_cut]}")
    print(f"B&H OOS Sharpe = {sharpe(day_ret[oos]):+.2f} ; champion OOS = {sharpe(champ_ret[oos]):+.2f}\n")

    # --- fetch (cached) all four market-level signals ------------------------
    s1 = _cached("opt_foreign_net", fetch_foreign_opt_net)
    s2 = _cached("mtx_retail_ratio", fetch_mtx_retail_ratio)
    s34 = _cached("pcr_maxpain", fetch_pcr_maxpain)
    p = (p.merge(s1, on="date", how="left")
           .merge(s2, on="date", how="left")
           .merge(s34, on="date", how="left"))
    # S4 raw signal: index close vs pinned Max-Pain strike (mean-reversion / fade)
    p["maxpain_dev"] = (p["ix_close"] - p["maxpain"]) / p["maxpain"]

    # consolidated reusable data file (all raw market-level series + fwd ret)
    data_cols = ["date", "ix_open", "ix_close", "foreign_opt_net", "mtx_retail_ratio",
                 "pcr_oi", "pcr_vol", "maxpain", "maxpain_dev"]
    dat = p[data_cols].copy()
    dat["fwd_open_open"] = day_ret.values
    dat.to_parquet(CACHE / "options_micro_data.parquet", index=False)
    print(f"consolidated data -> {CACHE/'options_micro_data.parquet'}  "
          f"({len(dat)} rows, {dat['pcr_oi'].notna().sum()} with PCR)\n")

    rows = []
    r1 = eval_signal("S1 foreign_opt_net_z60", rz(p["foreign_opt_net"], 60), p, day_ret,
                     ismask, oos, champ_bull, champ_ret, contrarian=False)
    r2 = eval_signal("S2 mtx_retail_ratio_z60", rz(p["mtx_retail_ratio"], 60), p, day_ret,
                     ismask, oos, champ_bull, champ_ret, contrarian=True)
    # S3 PCR_OI z60 — sentiment mirror, contrarian at extremes (direction fixed from IS IC)
    r3 = eval_signal("S3 pcr_oi_z60", rz(p["pcr_oi"], 60), p, day_ret,
                     ismask, oos, champ_bull, champ_ret, contrarian=True)
    # S4 Max-Pain deviation z60 — mean-reversion / pin effect (contrarian)
    r4 = eval_signal("S4 maxpain_dev_z60", rz(p["maxpain_dev"], 60), p, day_ret,
                     ismask, oos, champ_bull, champ_ret, contrarian=True)
    for r in (r1, r2, r3, r4):
        if r:
            rows.append(r)
    res = pd.DataFrame(rows)
    with pd.option_context("display.width", 220, "display.max_columns", 30):
        print("=== options-micro signal scan (direction fixed from IS IC) ===")
        print(res.to_string(index=False))
    res.to_csv(CACHE / "options_micro_results.csv", index=False)
    print(f"\n-> {CACHE/'options_micro_results.csv'}")

    # champion collinearity: raw-signal corr matrix (are S1/S3 just fut_foreign_oi?)
    coll = pd.DataFrame({
        "champ_fut_foreign_z60": rz(p["fut_foreign_oi"], 60),
        "S1_foreign_opt_z60": rz(p["foreign_opt_net"], 60),
        "S3_pcr_oi_z60": rz(p["pcr_oi"], 60),
    }).corr()
    print("\n=== raw-signal collinearity vs champion (Pearson) ===")
    print(coll.round(3).to_string())

    # --- Deflated-Sharpe + residual-IC (momentum/champion control) -----------
    sig_defs = {
        "S1 foreign_opt_net_z60": (rz(p["foreign_opt_net"], 60), 1),
        "S2 mtx_retail_ratio_z60": (rz(p["mtx_retail_ratio"], 60), -1),
        "S3 pcr_oi_z60": (rz(p["pcr_oi"], 60), 1),
        "S4 maxpain_dev_z60": (rz(p["maxpain_dev"], 60), 1),
    }
    N_TRIALS = 8  # 4 signals x ~2 normalisations searched across this dimension
    # per-period OOS Sharpe of every trial (for cross-trial variance in DSR)
    sr_trials = []
    oos_ret = {}
    for nm, (v, d) in sig_defs.items():
        r = lf((d * v) > 0, day_ret)
        oos_ret[nm] = r[oos]
        sr_trials.append(per_period_sharpe(r[oos]))
    print("\n=== Deflated-Sharpe (OOS) + residual-IC (control champ+20d mom) ===")
    print(f"{'signal':26s} {'OOS_SR_pp':>9s} {'DSR':>6s} {'SR0':>6s} "
          f"{'IS_IC':>7s} {'resid_IC':>9s}")
    for nm, (v, d) in sig_defs.items():
        dsr, sr0, T = deflated_sharpe(oos_ret[nm], N_TRIALS, sr_trials)
        ric, rres = residual_ic(v, p, day_ret, ismask)
        srpp = per_period_sharpe(oos_ret[nm])
        print(f"{nm:26s} {srpp:9.3f} {dsr:6.3f} {sr0:6.3f} {ric:7.4f} {rres:9.4f}")
    print(f"(DSR trial count N={N_TRIALS}; DSR>0.95 = survives search deflation)")


if __name__ == "__main__":
    main()
