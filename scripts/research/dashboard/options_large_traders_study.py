"""Options LARGE-TRADERS chip signal — gap-fill for options_micro dimension.

S5  大額交易人買賣權淨偏多度 (TXO top-10 large traders' bullish OI bias)
    FinMind dataset: TaiwanOptionOpenInterestLargeTraders  data_id=TXO
    Per (date, contract_type in {week, <YYYYMM>, all}, put_call in {call, put}) we get
    buy/sell top5 & top10 large-trader OI + market_open_interest + specific(法人).

    Signal (contract_type='all', top10):
      net_call = buy_top10_call_oi - sell_top10_call_oi
      net_put  = buy_top10_put_oi  - sell_top10_put_oi
      lt_bull  = (net_call - net_put) / market_open_interest      # bullish bias, OI-normalised
                 long calls & short puts = bullish; long puts & short calls = bearish
    Normalise: z60. Direction FIXED from IN-SAMPLE IC sign (no OOS peek).
    Prior: LEADING (purest big-trader positioning; Pan-Poteshman leverage argument).

    Variants tried (DSR trial accounting): top10 & top5 net-bias, z60 & z20.  N_TRIALS=4.

WHY: options_micro report §8 step 3 flagged this as "the only possible overturn" —
is the large-trader option bias a CLEANER leading signal than S1 (外資選擇權淨)?
This gap-fill fetches the real data and answers it.

METHODOLOGY (project standard, falsification-first) — identical to options_micro_study:
  fwd(t)=ix_close[t+1]/ix_open[t+1]-1 (signal known post-close), cost 4bps/turn.
  IS/OOS 70/30 chronological; direction from IS IC only.
  permutation vs same-exposure random timing; Deflated-Sharpe (trial count honest);
  regime-conditioning (bull = ix>MA200 & MA200 up); collinearity vs champion
  (fut_foreign_oi_z60) AND vs S1 (foreign_opt_net_z60); residual-IC controlling
  champion + 20d momentum + S1 (does anything survive beyond the option echo?).

非投資建議 — research scaffold only.
"""
from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
CACHE = ROOT / "data" / "research" / "dashboard"
CACHE.mkdir(parents=True, exist_ok=True)

COST_BPS = 4.0
ANN = np.sqrt(252)
RNG = np.random.default_rng(7)
START = date(2018, 6, 1)
END = date.today()


def rz(s, w):
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def sharpe(x):
    x = pd.Series(x).dropna()
    return x.mean() / x.std() * ANN if len(x) > 20 and x.std() > 0 else np.nan


def per_period_sharpe(x):
    x = pd.Series(x).dropna()
    return x.mean() / x.std() if len(x) > 20 and x.std() > 0 else np.nan


def lf(bull, day_ret, cost=COST_BPS):
    pos = pd.Series(bull, index=day_ret.index).astype(float).fillna(0.0)
    return pos * day_ret - pos.diff().abs().fillna(pos.abs()) * (cost / 1e4)


def bull_regime(p):
    ma = p["ix_close"].rolling(200, min_periods=200).mean()
    return (p["ix_close"] > ma) & (ma > ma.shift(20))


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


def deflated_sharpe(strat_ret, n_trials, sr_trials):
    from scipy import stats
    r = pd.Series(strat_ret).dropna()
    T = len(r)
    if T < 30 or r.std() == 0:
        return np.nan, np.nan, T
    sr = r.mean() / r.std()
    sk = float(stats.skew(r)); ku = float(stats.kurtosis(r, fisher=False))
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


def residual_ic(raw_sig, p, day_ret, mask, extra=None):
    """IS IC after regressing out champion (fut_foreign_oi_z60) + 20d momentum
    (+ optional extra controls, e.g. S1 foreign_opt_net_z60)."""
    mom = p["ix_close"] / p["ix_close"].shift(20) - 1.0
    champ = rz(p["fut_foreign_oi"], 60)
    cols = {"sig": raw_sig, "f": day_ret, "champ": champ, "mom": mom}
    if extra is not None:
        for k, v in extra.items():
            cols[k] = v
    df = pd.DataFrame(cols).dropna()
    df = df[mask.reindex(df.index).fillna(False)]
    if len(df) < 100:
        return np.nan, np.nan
    raw_ic = df["sig"].corr(df["f"])
    ctrl = ["champ", "mom"] + ([k for k in (extra or {})])
    X = np.column_stack([np.ones(len(df))] + [df[c].values for c in ctrl])
    bs = np.linalg.lstsq(X, df["sig"].values, rcond=None)[0]
    bf = np.linalg.lstsq(X, df["f"].values, rcond=None)[0]
    rs = df["sig"].values - X @ bs
    rf = df["f"].values - X @ bf
    return round(float(raw_ic), 4), round(float(np.corrcoef(rs, rf)[0, 1]), 4)


# --------------------------------------------------------------- fetch --------
def fetch_large_traders():
    """TaiwanOptionOpenInterestLargeTraders TXO, contract_type='all', top5+top10."""
    from finmind_client import fetch_finmind
    frames = []
    for yr in range(START.year, END.year + 1):
        s = max(START, date(yr, 1, 1)); e = min(END, date(yr, 12, 31))
        raw = fetch_finmind("TaiwanOptionOpenInterestLargeTraders", "TXO", s, e, timeout=180)
        if raw:
            frames.append(pd.DataFrame(raw))
        time.sleep(0.3)
    d = pd.concat(frames, ignore_index=True)
    d = d[d["contract_type"] == "all"].copy()
    for c in ["buy_top5_trader_open_interest", "sell_top5_trader_open_interest",
              "buy_top10_trader_open_interest", "sell_top10_trader_open_interest",
              "market_open_interest"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    rows = []
    for dt, g in d.groupby("date"):
        c = g[g["put_call"] == "call"]; pu = g[g["put_call"] == "put"]
        if c.empty or pu.empty:
            continue
        c = c.iloc[0]; pu = pu.iloc[0]
        mkt = c["market_open_interest"] + pu["market_open_interest"]
        if not mkt or mkt <= 0:
            continue
        net_call10 = c["buy_top10_trader_open_interest"] - c["sell_top10_trader_open_interest"]
        net_put10 = pu["buy_top10_trader_open_interest"] - pu["sell_top10_trader_open_interest"]
        net_call5 = c["buy_top5_trader_open_interest"] - c["sell_top5_trader_open_interest"]
        net_put5 = pu["buy_top5_trader_open_interest"] - pu["sell_top5_trader_open_interest"]
        rows.append({
            "date": str(dt)[:10],
            "lt_bull_top10": (net_call10 - net_put10) / mkt,
            "lt_bull_top5": (net_call5 - net_put5) / mkt,
        })
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def _cached(name, builder):
    fp = CACHE / f"{name}.parquet"
    if fp.exists():
        return pd.read_parquet(fp)
    df = builder()
    df.to_parquet(fp, index=False)
    return df


def eval_signal(name, raw_val, p, day_ret, ismask, oos, champ_bull, champ_ret):
    v = pd.Series(raw_val, index=p.index)
    d = pd.DataFrame({"v": v, "f": day_ret}).dropna()
    if len(d) < 100:
        return None
    im = ismask.reindex(d.index).fillna(False)
    ic_is = d[im]["v"].corr(d[im]["f"])
    direction = np.sign(ic_is) if pd.notna(ic_is) and ic_is != 0 else 1.0
    bull = (direction * v) > 0
    r = lf(bull, day_ret)
    breg = bull_regime(p)
    r_bull = lf(bull & breg, day_ret)
    _, pv = perm_p(bull, day_ret, oos)
    corr_champ = pd.DataFrame({"a": r, "b": champ_ret}).dropna().corr().iloc[0, 1]
    comb = (champ_bull.astype(float) + bull.astype(float)) / 2
    cr = comb * day_ret - comb.diff().abs().fillna(comb.abs()) * COST_BPS / 1e4
    return {
        "signal": name, "dir_from_IS": int(direction),
        "IC_IS": round(float(ic_is), 4), "OOS_Sharpe": round(float(sharpe(r[oos])), 3),
        "OOS_bull_only": round(float(sharpe(r_bull[oos])), 3),
        "OOS_perm_p": None if pd.isna(pv) else round(pv, 3),
        "corr_vs_champ": round(float(corr_champ), 3),
        "combo50_OOS": round(float(sharpe(cr[oos])), 3),
        "exposure": round(float(bull.reindex(day_ret.index).fillna(False).mean()), 3),
        "n": int(len(d)),
    }


def main():
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    p["date"] = p["date"].astype(str).str[:10]
    day_ret = (p["ix_close"] / p["ix_open"] - 1.0).shift(-1)
    n = len(p); is_cut = int(n * 0.7)
    ismask = pd.Series(p.index < is_cut, index=p.index)
    oos = pd.Series(p.index >= is_cut, index=p.index)
    champ_bull = rz(p["fut_foreign_oi"], 60) > 0
    champ_ret = lf(champ_bull, day_ret)

    print(f"panel {p['date'].iloc[0]}..{p['date'].iloc[-1]} n={n}  IS/OOS cut @ {p['date'].iloc[is_cut]}")
    print(f"B&H OOS Sharpe={sharpe(day_ret[oos]):+.2f} ; champion OOS={sharpe(champ_ret[oos]):+.2f}\n")

    lt = _cached("opt_large_traders", fetch_large_traders)
    s1 = pd.read_parquet(CACHE / "opt_foreign_net.parquet")
    print(f"large-traders rows={len(lt)} span {lt['date'].min()}..{lt['date'].max()}")
    p = p.merge(lt, on="date", how="left").merge(s1, on="date", how="left")
    cov = p["lt_bull_top10"].notna().sum()
    print(f"coverage on panel: {cov}/{len(p)} days ({100*cov/len(p):.1f}%)\n")

    s1z = rz(p["foreign_opt_net"], 60)
    sig_defs = {
        "S5 lt_bull_top10_z60": rz(p["lt_bull_top10"], 60),
        "S5b lt_bull_top5_z60": rz(p["lt_bull_top5"], 60),
        "S5c lt_bull_top10_z20": rz(p["lt_bull_top10"], 20),
        "S5d lt_bull_top5_z20": rz(p["lt_bull_top5"], 20),
    }
    rows = []
    for nm, v in sig_defs.items():
        r = eval_signal(nm, v, p, day_ret, ismask, oos, champ_bull, champ_ret)
        if r:
            rows.append(r)
    res = pd.DataFrame(rows)
    with pd.option_context("display.width", 220, "display.max_columns", 30):
        print("=== large-traders signal scan (direction fixed from IS IC) ===")
        print(res.to_string(index=False))
    res.to_csv(CACHE / "options_large_traders_results.csv", index=False)

    # collinearity: is S5 just champion or just S1?
    coll = pd.DataFrame({
        "champ_fut_foreign_z60": rz(p["fut_foreign_oi"], 60),
        "S1_foreign_opt_z60": s1z,
        "S5_lt_bull_top10_z60": rz(p["lt_bull_top10"], 60),
        "S5_lt_bull_top5_z60": rz(p["lt_bull_top5"], 60),
    }).corr()
    print("\n=== raw-signal collinearity (Pearson) ===")
    print(coll.round(3).to_string())

    # DSR + residual-IC (control champ+mom, then also +S1)
    N_TRIALS = 4
    sr_trials, oos_ret = [], {}
    for nm, v in sig_defs.items():
        r = eval_signal_dir(v, p, day_ret, ismask)
        oos_ret[nm] = r[oos]
        sr_trials.append(per_period_sharpe(r[oos]))
    print("\n=== Deflated-Sharpe (OOS) + residual-IC ===")
    print(f"{'signal':24s} {'OOS_SR_pp':>9s} {'DSR':>6s} {'SR0':>6s} "
          f"{'IS_IC':>7s} {'rIC(+mom+champ)':>15s} {'rIC(+S1)':>9s}")
    for nm, v in sig_defs.items():
        dsr, sr0, T = deflated_sharpe(oos_ret[nm], N_TRIALS, sr_trials)
        ic, ric = residual_ic(v, p, day_ret, ismask)
        _, ric_s1 = residual_ic(v, p, day_ret, ismask, extra={"s1": s1z})
        srpp = per_period_sharpe(oos_ret[nm])
        print(f"{nm:24s} {srpp:9.3f} {dsr:6.3f} {sr0:6.3f} {ic:7.4f} {ric:15.4f} {ric_s1:9.4f}")
    print(f"(DSR trial count N={N_TRIALS}; DSR>0.95 = survives search deflation)")


def eval_signal_dir(v, p, day_ret, ismask):
    d = pd.DataFrame({"v": v, "f": day_ret}).dropna()
    im = ismask.reindex(d.index).fillna(False)
    ic = d[im]["v"].corr(d[im]["f"])
    direction = np.sign(ic) if pd.notna(ic) and ic != 0 else 1.0
    return lf((direction * v) > 0, day_ret)


if __name__ == "__main__":
    main()
