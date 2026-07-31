"""個股「近斷頭部位廣度」(near-call breadth)— falsification-first study.

GAP-FILL for margin_maintenance dimension. The market-level ground-truth line
(TaiwanTotalExchangeMarginMaintenance) is a single weighted average and CANNOT
answer the one genuinely-precursor form flagged in the report: the *breadth* of
per-stock margin positions sitting near the 130% 追繳線 — the precondition for a
forced-sell cascade.

METHOD
------
Per-stock maintenance is reconstructed from REAL chip flows (not price alone):
we run weighted-average-cost accounting over each stock's daily 融資餘額(張)
changes valued at that day's close:

    ΔBal>0 (new margin buys): qty+=Δ ; cost += Δ*close
    ΔBal<0 (repay/sell)     : cost *= (qty+Δ)/qty ; qty += Δ      (avg-cost)
    cost_basis = cost / qty
    maintenance% = close / (0.6 * cost_basis) * 100        (60% 融資成數, 上市)

Near-call breadth_t = share of margin-carrying stocks with maintenance < CALL
(130/140/150%). This uses the ACTUAL entry prices of margin buyers, so it is
distinguishable from pure price breadth (% below MA60) — the collinearity control.

DATA (HONEST COVERAGE)
----------------------
Local replica `data/replica/stocks.db` — the SAME TWSE margin data FinMind
serves, free & quota-safe. `stock_margin_daily` (融資餘額 張, margin_change) +
`stock_daily_bars` (raw close). Universe = only ~152 large/mid-cap names that
carry margin locally (0050+0051-tier), margin ends 2026-07-07. This UNDER-samples
small-cap retail margin stress (full universe ~1000+ names) — the core caveat.
Warmup accounting from 2019; breadth series evaluated 2020-01 -> 2026-07-07.

Market fwd returns / regime / champion from chip_macro/panel.parquet.

  cd /Users/jackm4/goldenstocks
  .venv/bin/python scripts/research/dashboard/margin_nearcall_breadth_study.py

lead_lag: PRECURSOR candidate (event-level). layer: L2 chip-core, L0-gated.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
DB = ROOT / "data" / "replica" / "stocks.db"
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
CACHE = ROOT / "data" / "research" / "dashboard" / "margin_nearcall_breadth_data.parquet"
OUT = ROOT / "reports" / "research" / "dashboard-completeness"
MARGIN_RATE = 0.60          # 上市融資成數 -> initial maintenance 1/0.6 = 166.7%
WARMUP_START = "2019-01-01"
EVAL_START = "2020-01-01"
ANN = np.sqrt(252)
RNG = np.random.default_rng(11)


def build_breadth(refresh: bool = False) -> pd.DataFrame:
    if CACHE.exists() and not refresh:
        return pd.read_parquet(CACHE)

    con = sqlite3.connect(DB)
    mg = pd.read_sql(
        "select stock_id, trade_date as date, margin_balance as bal, margin_change as chg "
        "from stock_margin_daily where trade_date >= ?",
        con, params=[WARMUP_START])
    px = pd.read_sql(
        "select stock_id, trade_date as date, close from stock_daily_bars "
        "where trade_date >= ? and close is not null and close > 0",
        con, params=[WARMUP_START])
    con.close()

    df = mg.merge(px, on=["stock_id", "date"], how="inner")
    df = df.sort_values(["stock_id", "date"]).reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])

    rows = []
    for sid, g in df.groupby("stock_id", sort=False):
        g = g.reset_index(drop=True)
        # delta: prefer given margin_change, else diff of balance
        d = g["chg"].copy()
        d = d.where(d.notna(), g["bal"].diff())
        qty = 0.0
        cost = 0.0
        maint = np.full(len(g), np.nan)
        for i in range(len(g)):
            close = g.at[i, "close"]
            delta = d.iat[i]
            if not np.isfinite(delta):
                # first obs: seed at current balance & price (maintenance ~166%)
                qty = float(g.at[i, "bal"])
                cost = qty * close
            elif delta > 0:
                qty += delta
                cost += delta * close
            elif delta < 0 and qty > 0:
                new_qty = max(qty + delta, 0.0)
                cost *= (new_qty / qty) if qty > 0 else 0.0
                qty = new_qty
            if qty > 5 and cost > 0:  # need a live margin book
                cb = cost / qty
                maint[i] = close / (MARGIN_RATE * cb) * 100.0
        g["maint"] = maint
        g["stock_id"] = sid
        rows.append(g[["date", "stock_id", "maint", "bal"]])

    allm = pd.concat(rows, ignore_index=True)
    allm = allm[allm["maint"].notna() & (allm["bal"] > 0)]

    def agg(day: pd.DataFrame) -> pd.Series:
        m = day["maint"].values
        n = len(m)
        return pd.Series({
            "n_stocks": n,
            "br_130": np.mean(m < 130.0),
            "br_140": np.mean(m < 140.0),
            "br_150": np.mean(m < 150.0),
            "med_maint": np.median(m),
        })

    breadth = allm.groupby("date").apply(agg, include_groups=False).reset_index()
    breadth = breadth[breadth["date"] >= EVAL_START].reset_index(drop=True)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    breadth.to_parquet(CACHE, index=False)
    print(f"[build] {len(breadth)} days, median universe {breadth['n_stocks'].median():.0f} stocks -> {CACHE}")
    return breadth


def rz(s, w):
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def sharpe(x):
    x = pd.Series(x).dropna()
    return x.mean() / x.std() * ANN if len(x) > 20 and x.std() > 0 else np.nan


def lf(pos, day_ret, cost=4.0):
    pos = pd.Series(pos, index=day_ret.index).astype(float).fillna(0.0)
    return pos * day_ret - pos.diff().abs().fillna(pos.abs()) * (cost / 1e4)


def perm_p(pos, day_ret, mask, n=3000):
    dr = day_ret[mask].reset_index(drop=True)
    ps = pd.Series(pos, index=day_ret.index)[mask].astype(float).reset_index(drop=True)
    v = dr.notna()
    dr, ps = dr[v], ps[v]
    actual = sharpe(ps.values * dr.values)
    k, N = int(ps.sum()), len(ps)
    if k == 0 or k == N:
        return actual, np.nan
    drv = dr.values
    null = np.array([sharpe(np.isin(np.arange(N), RNG.choice(N, k, False)).astype(float) * drv)
                     for _ in range(n)])
    return actual, float((null >= actual).mean())


def deflated_sharpe(ret, n_trials):
    from scipy import stats as st
    x = pd.Series(ret).dropna()
    T = len(x)
    if T < 30 or x.std() == 0:
        return np.nan, np.nan, np.nan
    sr = x.mean() / x.std()
    sk = st.skew(x); ku = st.kurtosis(x, fisher=False)
    sr_std = np.sqrt((1 - sk * sr + (ku - 1) / 4 * sr ** 2) / (T - 1))
    emc = 0.5772
    z = st.norm.ppf(1 - 1.0 / n_trials) + emc * (
        st.norm.ppf(1 - 1.0 / (n_trials * np.e)) - st.norm.ppf(1 - 1.0 / n_trials))
    sr0 = sr_std * z
    return float(st.norm.cdf((sr - sr0) / sr_std)), float(sr * ANN), float(sr0 * ANN)


def main(refresh=False):
    br = build_breadth(refresh)
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    p["date"] = pd.to_datetime(p["date"])
    df = p.merge(br, on="date", how="inner").sort_values("date").reset_index(drop=True)

    day_ret = (df["ix_close"] / df["ix_open"] - 1.0).shift(-1)
    fwd10 = df["ix_close"].shift(-10) / df["ix_close"] - 1.0
    fwd20 = df["ix_close"].shift(-20) / df["ix_close"] - 1.0
    n = len(df)
    cut = int(n * 0.7)
    ismask = pd.Series(df.index < cut, index=df.index)
    oos = pd.Series(df.index >= cut, index=df.index)

    # signals: rising near-call breadth = deteriorating leverage -> bearish precursor
    b140 = df["br_140"]
    b140_z = rz(b140, 60)
    # price-breadth control: share of the SAME market below its own MA60 (pure momentum)
    ma200 = df["ix_close"].rolling(200, min_periods=200).mean()
    bull = (df["ix_close"] > ma200) & (ma200 > ma200.shift(20))
    champ = rz(df["fut_foreign_oi"], 60) > 0
    champ_ret = lf(champ, day_ret)

    print(f"merged n={n}  {df.date.min().date()} -> {df.date.max().date()}  IS/OOS cut@{cut}")
    print(f"universe median {df['n_stocks'].median():.0f} stocks  med_maint {df['med_maint'].median():.1f}%")
    print(f"br_140 mean {b140.mean():.3f} range [{b140.min():.3f},{b140.max():.3f}]  br_130 mean {df['br_130'].mean():.3f}\n")

    def ic(sig, mask, fwd=day_ret):
        d = pd.DataFrame({"s": sig, "r": fwd})[mask].dropna()
        return (d["s"].corr(d["r"]), len(d)) if len(d) > 20 else (np.nan, len(d))

    # ---- IC next-day (level & z) both windows ----
    print("=== IC: near-call breadth -> next-day mkt (higher breadth = more stress) ===")
    for name, sig in [("br_140", b140), ("br_140_z60", b140_z), ("br_130", df["br_130"]), ("br_150", df["br_150"])]:
        ii, ni = ic(sig, ismask); io, no = ic(sig, oos)
        print(f"  {name:12s} IC_IS={ii:+.4f}(n={ni})  IC_OOS={io:+.4f}(n={no})")
    ic_is, _ = ic(b140_z, ismask)
    direction = np.sign(ic_is) if pd.notna(ic_is) and ic_is != 0 else -1.0

    # ---- collinearity vs price momentum & champion ----
    mom = df["ix_close"] / df["ix_close"].rolling(60, min_periods=60).mean()
    mom_z = rz(mom, 60)
    dd = pd.DataFrame({"b": b140_z, "mom": mom_z, "r": day_ret}).dropna()
    beta = np.polyfit(dd["mom"], dd["b"], 1)[0]
    ic_raw = dd["b"].corr(dd["r"])
    ic_res = (dd["b"] - beta * dd["mom"]).corr(dd["r"])
    print("\n=== collinearity: is near-call breadth just inverse price momentum? ===")
    print(f"  corr(br_140_z, priceMom_z60) = {b140_z.corr(mom_z):+.3f}")
    print(f"  corr(br_140_z, champion_z60) = {b140_z.corr(rz(df['fut_foreign_oi'],60)):+.3f}")
    print(f"  IC raw = {ic_raw:+.4f}  |  IC | price-mom out = {ic_res:+.4f}"
          f"  (shrink {100*(1-abs(ic_res)/abs(ic_raw)):.0f}%)")

    # ---- long/flat: go flat when breadth-z high (stress), long otherwise ----
    directed = direction * b140_z
    pos = (directed > 0)  # direction<0 => long when breadth LOW
    r = lf(pos, day_ret)
    _, p_oos = perm_p(pos, day_ret, oos)
    print("\n=== long/flat (direction from IS IC) ===")
    print(f"  IS Sharpe={sharpe(r[ismask]):+.2f}  OOS Sharpe={sharpe(r[oos]):+.2f}"
          f"  (B&H OOS {sharpe(day_ret[oos]):+.2f}, champion OOS {sharpe(champ_ret[oos]):+.2f})")
    print(f"  OOS permutation p = {p_oos:.4f}")

    # ---- regime split ----
    print("\n=== regime conditioning ===")
    for lbl, m in [("BULL", bull), ("BEAR/side", ~bull)]:
        rr = r[m.reindex(r.index).fillna(False)]
        print(f"  {lbl:9s} Sharpe={sharpe(rr):+.2f} n={int(m.sum())}")

    # ---- PRECURSOR event test: spikes in near-call breadth -> forward flush/bounce ----
    print("\n=== precursor event test: high near-call breadth -> fwd mkt ===")
    for q in (0.90, 0.95, 0.99):
        thr = b140.quantile(q)
        hi = b140 >= thr
        print(f"  br_140>=p{int(q*100)} ({int(hi.sum())}d thr={thr:.3f}): "
              f"fwd10 {fwd10[hi].mean():+.3%}(lift {fwd10[hi].mean()-fwd10.mean():+.3%}) | "
              f"fwd20 {fwd20[hi].mean():+.3%}(lift {fwd20[hi].mean()-fwd20.mean():+.3%})")
    print("  -- absolute breadth thresholds (fwd20) --")
    for th in (0.15, 0.25, 0.40):
        hi = b140 >= th
        if hi.sum() < 10:
            print(f"     br_140>={th:.0%}: only {int(hi.sum())}d (skip)"); continue
        print(f"     br_140>={th:.0%} ({int(hi.sum())}d) fwd20 {fwd20[hi].mean():+.3%}"
              f" (lift {fwd20[hi].mean()-fwd20.mean():+.3%})")

    # ---- deep-tail contrarian (deep near-call breadth = post-flush bottom?) ----
    print("\n=== deep near-call breadth -> mean-reversion bounce (contrarian) ===")
    for q in (0.95, 0.99):
        thr = df["br_130"].quantile(q)
        hi = df["br_130"] >= thr
        if hi.sum() >= 10:
            print(f"  br_130>=p{int(q*100)} ({int(hi.sum())}d): fwd20 lift {fwd20[hi].mean()-fwd20.mean():+.3%}")

    dsr, asr, asr0 = deflated_sharpe(r[oos], n_trials=10)
    print("\n=== Deflated-Sharpe (OOS long/flat, ~10 forms searched) ===")
    print(f"  DSR={dsr:.3f} (obs annSR {asr:+.2f}, null-max {asr0:+.2f}) => {'PASS' if dsr>0.95 else 'FAIL'} @0.95")

    OUT.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{
        "n": n, "start": str(df.date.min().date()), "end": str(df.date.max().date()),
        "univ_median": df["n_stocks"].median(), "br140_mean": b140.mean(),
        "IC_IS_z": ic_is, "IC_OOS_z": ic(b140_z, oos)[0],
        "corr_priceMom": b140_z.corr(mom_z), "corr_champ": b140_z.corr(rz(df["fut_foreign_oi"], 60)),
        "IC_raw": ic_raw, "IC_resid_mom": ic_res,
        "OOS_Sharpe": sharpe(r[oos]), "BH_OOS": sharpe(day_ret[oos]), "champ_OOS": sharpe(champ_ret[oos]),
        "OOS_perm_p": p_oos, "DSR": dsr,
        "evt_p95_fwd20_lift": fwd20[b140 >= b140.quantile(0.95)].mean() - fwd20.mean(),
    }]).to_csv(OUT / "margin_nearcall_breadth_metrics.csv", index=False)
    print(f"\n-> {OUT/'margin_nearcall_breadth_metrics.csv'}")


if __name__ == "__main__":
    main(refresh="--refresh" in sys.argv)
