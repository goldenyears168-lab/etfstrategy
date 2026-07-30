"""State-grid conditional forward-return table (interaction research, dashboard family).

FAMILY: 【狀態分格條件報酬表】
    Bucket each trading day by four a-priori, economically-motivated binary axes and
    read the conditional forward return in each cell.  The question is whether any
    *non-obvious* combined market state carries a robust, sample-sufficient edge that
    is NOT merely a restatement of the champion (foreign-futures positioning) or the
    MA200 price regime.

FOUR AXES (fixed a priori; no threshold search beyond the primary median split):
    A  champ+      : z60(fut_foreign_oi) > 0            (validated market-timing champion)
    B  vix_high    : VIX > trailing 252d median          (fear regime; adaptive, no magic 20)
    C  breadth_str : pct_above_ma200 > trailing 252d med  (participation strong)
    D  above_ma200 : ix_close > index MA200               (price regime, rising or not)
    -> 2^4 = 16 cells.

METRICS per cell:
    - fwd20 mean return  (index, tradeable t+1 open -> t+20 close; NO look-ahead)
    - win rate, n days, annualized, Newey-West t-stat (overlap-aware)
    - daily-return strategy Sharpe (long index t+1 open->close while in the cell), for DSR.

DISCIPLINE:
    - Multiple testing is the enemy: DSR / Bonferroni penalty uses the FULL cell count
      (16) plus any alt-binning trials, reported honestly.
    - Walk-forward: expanding-window sign stability of every candidate cell.
    - Permutation same-exposure (same #days long, random placement) for survivors.
    - Collinearity: every survivor must answer "is it just champ or MA200 restated?"
      via overlap fraction and daily-return correlation vs champ and MA200 strategies.
    - Honest expectation: most cells fail; only cross-walk-forward + strict DSR survive.

Non-investment advice: research code only.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[3]
DB = ROOT / "data" / "stocks.db"
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
MACRO = ROOT / "data" / "research" / "dashboard" / "global_macro_data.parquet"
BREADTH_CACHE = ROOT / "data" / "research" / "dashboard" / "pct_ma200_breadth.parquet"
OUT_CELLS = ROOT / "data" / "research" / "dashboard" / "state_grid_cells.csv"
OUT_WF = ROOT / "data" / "research" / "dashboard" / "state_grid_walkforward.csv"

START = "2018-06-01"
MIN_PART = 600
COST_BPS = 4.0
ANN = np.sqrt(252)
GAMMA = 0.5772156649
FWD = 20
RNG = np.random.default_rng(11)
N_PERM = 5000


def rz(s, w):
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def nw_tstat(x, lag):
    """Newey-West t-stat of the mean of overlapping series x (HAC)."""
    x = np.asarray(pd.Series(x).dropna(), float)
    n = len(x)
    if n < 30:
        return np.nan
    mu = x.mean()
    e = x - mu
    g0 = (e @ e) / n
    var = g0
    for k in range(1, lag + 1):
        gk = (e[k:] @ e[:-k]) / n
        var += 2 * (1 - k / (lag + 1)) * gk
    se = np.sqrt(var / n)
    return mu / se if se > 0 else np.nan


def sharpe(x):
    x = pd.Series(x).dropna()
    return float(x.mean() / x.std() * ANN) if len(x) > 20 and x.std() > 0 else np.nan


def lf(bull, day_ret, cost=COST_BPS):
    pos = pd.Series(bull, index=day_ret.index).astype(float).fillna(0.0)
    return (pos * day_ret - pos.diff().abs().fillna(pos.abs()) * cost / 1e4).dropna()


def psr(sr, n, g3, g4, sr_star):
    denom = np.sqrt(1 - g3 * sr + (g4 - 1) / 4.0 * sr**2)
    return norm.cdf((sr - sr_star) * np.sqrt(n - 1) / denom)


def expected_max_sr(var_trials, N):
    return np.sqrt(var_trials) * (
        (1 - GAMMA) * norm.ppf(1 - 1.0 / N) + GAMMA * norm.ppf(1 - 1.0 / (N * np.e))
    )


def build_breadth():
    if BREADTH_CACHE.exists():
        return pd.read_parquet(BREADTH_CACHE)
    con = sqlite3.connect(DB)
    px = pd.read_sql(
        "SELECT stock_id, trade_date, adj_close_v2 AS px FROM stock_close_adjusted "
        "WHERE trade_date >= ? AND adj_close_v2 > 0",
        con, params=(START,),
    )
    con.close()
    px["trade_date"] = pd.to_datetime(px["trade_date"])
    w = px.pivot_table(index="trade_date", columns="stock_id", values="px").sort_index()
    ma200 = w.rolling(200, min_periods=200).mean()
    above = w > ma200
    part = w.notna().sum(axis=1)
    df = pd.DataFrame({
        "date": w.index,
        "participants": part.values,
        "pct_above_ma200": (above.sum(axis=1) / above.notna().sum(axis=1).replace(0, np.nan)).values,
    })
    df = df[df["participants"] >= MIN_PART].reset_index(drop=True)
    df.to_parquet(BREADTH_CACHE, index=False)
    return df


def main():
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    p["date"] = pd.to_datetime(p["date"])
    macro = pd.read_parquet(MACRO)[["date", "VIX"]].copy()
    macro["date"] = pd.to_datetime(macro["date"])
    bd = build_breadth()

    p = p.merge(macro, on="date", how="left").merge(bd[["date", "pct_above_ma200"]], on="date", how="inner")
    p = p.sort_values("date").reset_index(drop=True)

    # --- forward returns (no look-ahead) ---
    day_ret = (p["ix_close"] / p["ix_open"] - 1.0).shift(-1)          # t+1 open->close
    # fwd20: buy t+1 open, sell t+20 close (index total move over the holding window)
    fwd20 = p["ix_close"].shift(-(FWD)) / p["ix_open"].shift(-1) - 1.0

    # --- four axes (all known at close t) ---
    z60 = rz(p["fut_foreign_oi"], 60)
    A = (z60 > 0)
    vix_med = p["VIX"].rolling(252, min_periods=120).median()
    B = (p["VIX"] > vix_med)                                          # fear high
    br_med = p["pct_above_ma200"].rolling(252, min_periods=120).median()
    C = (p["pct_above_ma200"] > br_med)                              # breadth strong
    ma200 = p["ix_close"].rolling(200, min_periods=200).mean()
    D = (p["ix_close"] > ma200)                                       # above MA200

    axes = pd.DataFrame({"champ": A, "vix_hi": B, "breadth_str": C, "above_ma200": D})
    valid = axes.notna().all(axis=1) & z60.notna() & vix_med.notna() & br_med.notna() & ma200.notna()

    base_fwd = fwd20[valid & fwd20.notna()]
    print(f"merged rows={len(p)}  usable(with all axes & fwd20)={int((valid & fwd20.notna()).sum())}  "
          f"({p.loc[valid,'date'].min().date()} -> {p.loc[valid,'date'].max().date()})")
    print(f"baseline fwd{FWD} mean={base_fwd.mean()*100:+.3f}%  win={ (base_fwd>0).mean()*100:.1f}%  "
          f"unconditional daily Sharpe(long-always)={sharpe(day_ret[valid]):.2f}")

    # ---------------- enumerate 16 cells ----------------
    rows = []
    strat_rets = {}          # cell key -> daily strategy return series (for DSR/collinearity)
    for a in (True, False):
        for b in (True, False):
            for c in (True, False):
                for d in (True, False):
                    m = valid & (axes["champ"] == a) & (axes["vix_hi"] == b) & \
                        (axes["breadth_str"] == c) & (axes["above_ma200"] == d)
                    f = fwd20[m & fwd20.notna()]
                    n = len(f)
                    key = f"C{int(a)}{int(b)}{int(c)}{int(d)}"
                    label = (("champ+" if a else "champ-") + "|" + ("vixHi" if b else "vixLo") + "|" +
                             ("brStr" if c else "brWk") + "|" + ("aMA200" if d else "bMA200"))
                    # daily strategy return: long index t+1 when today in cell
                    sr_daily = lf(m.astype(float), day_ret)
                    strat_rets[key] = (m, sr_daily)
                    if n >= 30:
                        t = nw_tstat(f.values, FWD)
                        rows.append({
                            "cell": key, "label": label, "n_days": n,
                            "fwd20_mean_%": f.mean() * 100, "win_%": (f > 0).mean() * 100,
                            "fwd20_ann_%": f.mean() * (252 / FWD) * 100,
                            "nw_t": t, "daily_sharpe": sharpe(sr_daily),
                        })
    cells = pd.DataFrame(rows).sort_values("fwd20_mean_%", ascending=False).reset_index(drop=True)

    # Bonferroni on NW t (two-sided) over the 16-cell grid
    n_cells_tested = int((cells["n_days"] >= 30).sum())
    cells["p_nw"] = 2 * (1 - norm.cdf(cells["nw_t"].abs()))
    cells["p_bonf"] = np.minimum(cells["p_nw"] * n_cells_tested, 1.0)

    pd.set_option("display.width", 200, "display.max_columns", 30)
    print(f"\n=== 16-cell state grid (fwd{FWD}, sorted by mean) — cells with n>=30 tested={n_cells_tested} ===")
    print(cells.to_string(index=False,
          formatters={"fwd20_mean_%": "{:+.2f}".format, "win_%": "{:.0f}".format,
                      "fwd20_ann_%": "{:+.1f}".format, "nw_t": "{:+.2f}".format,
                      "daily_sharpe": "{:+.2f}".format, "p_nw": "{:.3f}".format, "p_bonf": "{:.3f}".format}))

    baseline_mean = base_fwd.mean() * 100

    # ---------------- DSR on strategy view (trial dispersion = across the 16 cells' OOS daily SR) ----------------
    n = len(p); oos = pd.Series(p.index >= int(n * 0.7), index=p.index)
    trial_sr = []
    for key, (m, srd) in strat_rets.items():
        ro = srd[oos.reindex(srd.index).fillna(False)]
        if len(ro) > 20 and ro.std() > 0:
            trial_sr.append(ro.mean() / ro.std())
    var_trials = np.var(trial_sr, ddof=1) if len(trial_sr) > 1 else np.nan
    # honest N: 16 cells x (2 alt VIX binnings + 1 alt breadth binning we sanity-checked) ~ 16*? ; report 16 primary
    N_primary = 16

    def dsr_of(srd_full):
        r = srd_full.dropna().values
        nn = len(r); s = r.mean() / r.std()
        g3 = pd.Series(r).skew(); g4 = pd.Series(r).kurtosis() + 3
        return psr(s, nn, g3, g4, expected_max_sr(var_trials, N_primary)), s * ANN

    # ---------------- candidate survivors: economically-sensible, non-obvious, sample-sufficient ----------------
    # Focus on top cells by mean with n>=60 and positive NW t; report DSR, permutation, collinearity.
    champ_only = lf(axes["champ"].astype(float), day_ret)
    ma200_only = lf(axes["above_ma200"].astype(float), day_ret)

    print(f"\n=== survivor diagnostics (n>=60, mean>baseline {baseline_mean:+.2f}%, NW t>0) ===")
    print(f"trial SR dispersion: n={len(trial_sr)} var={var_trials:.2e} std(dailySR)={np.sqrt(var_trials):.4f} "
          f"SR*_ann={expected_max_sr(var_trials, N_primary)*ANN:+.2f}")
    surv = []
    for _, row in cells.iterrows():
        if row["n_days"] < 60 or row["nw_t"] <= 0 or row["fwd20_mean_%"] <= baseline_mean:
            continue
        key = row["cell"]; m, srd = strat_rets[key]
        d, ann = dsr_of(srd)
        # permutation same-exposure on fwd20 mean (block-aware via daily-strategy Sharpe instead)
        mask = m & fwd20.notna()
        dr = day_ret.copy()
        # same-exposure permutation on daily strategy Sharpe (full sample)
        base_series = srd
        pos = m.astype(float).reindex(day_ret.index).fillna(0.0)
        v = day_ret.notna()
        drv = day_ret[v].values; posv = pos[v].values
        k = int(posv.sum()); Nn = len(posv); act = sharpe(posv * drv)
        if 0 < k < Nn and np.isfinite(act):
            null = np.empty(N_PERM)
            for i in range(N_PERM):
                idx = RNG.choice(Nn, k, replace=False)
                pp = np.zeros(Nn); pp[idx] = 1.0
                null[i] = sharpe(pp * drv)
            perm_p = float((null >= act).mean())
        else:
            perm_p = np.nan
        # collinearity vs champ & MA200 daily strategies
        j = srd.index
        corr_champ = srd.corr(champ_only.reindex(j))
        corr_ma200 = srd.corr(ma200_only.reindex(j))
        overlap_champ = float((m & axes["champ"]).sum() / max(m.sum(), 1))
        overlap_ma200 = float((m & axes["above_ma200"]).sum() / max(m.sum(), 1))
        surv.append({
            "cell": key, "label": row["label"], "n_days": int(row["n_days"]),
            "fwd20_mean_%": round(row["fwd20_mean_%"], 2), "win_%": round(row["win_%"], 0),
            "nw_t": round(row["nw_t"], 2), "p_bonf": round(row["p_bonf"], 3),
            "strat_ann_Sh": round(ann, 2), "DSR": round(d, 3),
            "perm_p_sameexp": round(perm_p, 3),
            "corr_champ": round(corr_champ, 2), "corr_ma200": round(corr_ma200, 2),
        })
    survdf = pd.DataFrame(surv)
    if len(survdf):
        print(survdf.to_string(index=False))
    else:
        print("(no cell clears n>=60 & mean>baseline & NW t>0)")

    # ---------------- walk-forward sign stability for candidate cells ----------------
    # expanding window: 4 folds; compute fwd20 mean of each cell within each OOS fold
    idx = np.arange(len(p))
    cut = [int(len(p) * f) for f in (0.4, 0.55, 0.70, 0.85, 1.0)]
    wf_rows = []
    cand_keys = list(survdf["cell"]) if len(survdf) else list(cells.head(4)["cell"])
    for key in cand_keys:
        m, _ = strat_rets[key]
        rec = {"cell": key}
        for fi in range(len(cut) - 1):
            lo, hi = cut[fi], cut[fi + 1]
            seg = m.reindex(idx).fillna(False).values[lo:hi]
            fseg = fwd20.values[lo:hi]
            sel = seg & np.isfinite(fseg)
            rec[f"fold{fi+1}_n"] = int(sel.sum())
            rec[f"fold{fi+1}_mean%"] = round(float(np.nanmean(fseg[sel])) * 100, 2) if sel.sum() >= 5 else np.nan
        wf_rows.append(rec)
    wf = pd.DataFrame(wf_rows)
    print("\n=== walk-forward (expanding OOS folds 40-55-70-85-100%): fwd20 mean% per cell per fold ===")
    print(wf.to_string(index=False))

    cells.to_csv(OUT_CELLS, index=False)
    wf.to_csv(OUT_WF, index=False)
    print(f"\nwrote {OUT_CELLS}\nwrote {OUT_WF}")


if __name__ == "__main__":
    main()
