"""大戶持股集中度 — 中小型股延伸 (gap-fill, REAL DATA).

Phase-2 只在 164 檔大型股上跑, 千張大戶比中位數 59.5% (被外資保管行/ETF 稀釋),
結論 null 但無法外推中小股。本 study 把宇宙換成 80 檔【中小型股】(日均成交金額
0.85-9.8 億, 排除大型股, 千張大戶比中位數僅 39.6% → 千張≈真主力/內部人),
在同期 (2023-01→2026-07, 183 週) 真資料上重測相同三子訊號, 並新增
【champion 綠燈日 × concentration top-decile】條件式選股測試 (任務要求)。

方法論與 holder_concentration_study.py 完全一致 (IS/OOS 70/30 + 橫斷面 permutation
+ 去動能/外資 partial IC + regime + champion 共線 + Deflated-Sharpe)。

跑: .venv/bin/python scripts/research/dashboard/holder_concentration_midsmall_study.py
非投資建議。
"""
from __future__ import annotations
import sqlite3
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
DB = ROOT / "data" / "stocks.db"
DATA = ROOT / "data" / "research" / "dashboard" / "holder_concentration_midsmall_data.parquet"
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
RNG = np.random.default_rng(11)

LEVEL_1000 = "more than 1,000,001"
DROP_LEVELS = {"total", "差異數調整（說明4）"}


def build_signals() -> pd.DataFrame:
    disp = pd.read_parquet(DATA)
    disp["stock_id"] = disp["stock_id"].astype(str)
    disp["date"] = pd.to_datetime(disp["date"].str[:10])
    disp["people"] = pd.to_numeric(disp["people"], errors="coerce")
    disp["percent"] = pd.to_numeric(disp["percent"], errors="coerce")
    real = disp[~disp["HoldingSharesLevel"].isin(DROP_LEVELS)]
    big = (disp[disp["HoldingSharesLevel"] == LEVEL_1000]
           .groupby(["stock_id", "date"])["percent"].sum().rename("big_pct"))
    holders = (real.groupby(["stock_id", "date"])["people"].sum().rename("holders"))
    wide = pd.concat([big, holders], axis=1).reset_index().sort_values(["stock_id", "date"])
    wide["d_big"] = wide.groupby("stock_id")["big_pct"].diff()
    wide["d_holders"] = wide.groupby("stock_id")["holders"].apply(
        lambda s: np.log(s).diff()).reset_index(drop=True)

    def _z(s):
        return (s - s.rolling(26, min_periods=8).mean()) / s.rolling(26, min_periods=8).std()
    wide["z_big"] = wide.groupby("stock_id")["d_big"].transform(_z)
    wide["z_holders"] = wide.groupby("stock_id")["d_holders"].transform(_z)
    wide["concentration"] = wide["z_big"] - wide["z_holders"]
    return wide


def _load_adj_prices(sids):
    ph = ",".join("?" * len(sids))
    q = (f"SELECT stock_id, trade_date AS date, adj_close_v2 AS px FROM stock_close_adjusted "
         f"WHERE stock_id IN ({ph}) AND trade_date>='2022-06-01'")
    with sqlite3.connect(DB) as c:
        px = pd.read_sql(q, c, params=sids)
    px["date"] = pd.to_datetime(px["date"]); px["stock_id"] = px["stock_id"].astype(str)
    return px.sort_values(["stock_id", "date"]).reset_index(drop=True)


def _load_foreign(sids):
    ph = ",".join("?" * len(sids))
    q = (f"SELECT stock_id, trade_date AS date, foreign_remaining_ratio AS frr FROM stock_shareholding_daily "
         f"WHERE stock_id IN ({ph}) AND trade_date>='2022-06-01'")
    with sqlite3.connect(DB) as c:
        f = pd.read_sql(q, c, params=sids)
    f["date"] = pd.to_datetime(f["date"]); f["stock_id"] = f["stock_id"].astype(str)
    return f.sort_values(["stock_id", "date"]).reset_index(drop=True)


def _regime() -> pd.DataFrame:
    p = pd.read_parquet(PANEL); p["date"] = pd.to_datetime(p["date"]); p = p.sort_values("date")
    p["ma200"] = p["ix_close"].rolling(200, min_periods=200).mean()
    p["bull"] = (p["ix_close"] > p["ma200"]) & (p["ma200"].diff(25) > 0)
    # champion 綠燈 = 外資台指期 OI 週變動 > 0 (positioning risk-on)
    p["champ_up"] = p["fut_foreign_oi"].diff(5) > 0
    return p[["date", "bull", "champ_up"]]


def build_panel(publish_lag_days=5, fwd_days=5) -> pd.DataFrame:
    sig = build_signals()
    sids = sorted(sig["stock_id"].unique())
    px = _load_adj_prices(sids); frn = _load_foreign(sids); reg = _regime()
    px_by = {s: g.reset_index(drop=True) for s, g in px.groupby("stock_id")}
    frn_by = {s: g.reset_index(drop=True) for s, g in frn.groupby("stock_id")}
    out = []
    for sid, g in sig.groupby("stock_id"):
        p = px_by.get(sid)
        if p is None or len(p) < 60:
            continue
        pdt = p["date"].values; fr = frn_by.get(sid)
        for _, row in g.iterrows():
            if not np.isfinite(row["concentration"]) and not np.isfinite(row["d_big"]):
                continue
            entry_cut = row["date"] + pd.Timedelta(days=publish_lag_days)
            j = int(np.searchsorted(pdt, np.datetime64(entry_cut), side="left"))
            if j < 20 or j + fwd_days >= len(p):
                continue
            p0 = p["px"].iloc[j]; p1 = p["px"].iloc[j + fwd_days]; pm = p["px"].iloc[j - 20]
            if not (p0 > 0 and pm > 0 and p1 > 0):
                continue
            entry_date = p["date"].iloc[j]; frr_chg = np.nan
            if fr is not None:
                fj = int(np.searchsorted(fr["date"].values, np.datetime64(entry_date), side="right")) - 1
                if fj >= 6:
                    a = fr["frr"].iloc[fj]; b = fr["frr"].iloc[fj - 5]
                    if pd.notna(a) and pd.notna(b):
                        frr_chg = a - b
            out.append({"stock_id": sid, "date": entry_date, "d_big": row["d_big"],
                        "z_big": row["z_big"], "z_holders": row["z_holders"],
                        "concentration": row["concentration"], "big_pct": row["big_pct"],
                        "fwd_ret": p1 / p0 - 1.0, "mom20": p0 / pm - 1.0, "frr_chg": frr_chg})
    panel = pd.DataFrame(out).merge(reg, on="date", how="left")
    panel["bull"] = panel["bull"].fillna(False)
    panel["champ_up"] = panel["champ_up"].fillna(False)
    return panel.sort_values("date").reset_index(drop=True)


def _spear(a, b):
    d = pd.DataFrame({"a": a, "b": b}).dropna()
    return d["a"].corr(d["b"], method="spearman") if len(d) > 30 else np.nan


def partial_ic(panel, sig, ctrls):
    cols = [sig, "fwd_ret"] + ctrls
    d = panel[cols].dropna()
    if len(d) < 80:
        return np.nan
    r = d.rank()
    X = np.column_stack([np.ones(len(r))] + [r[c].values for c in ctrls])
    def resid(y):
        beta, *_ = np.linalg.lstsq(X, y, rcond=None); return y - X @ beta
    return float(pd.Series(resid(r[sig].values)).corr(pd.Series(resid(r["fwd_ret"].values))))


def perm_ic_p(panel, sig, actual, n=2000):
    d = panel[[sig, "fwd_ret", "date"]].dropna()
    dates = d["date"].values; vals = d[sig].values.copy(); fwd = d["fwd_ret"].values
    idx_by_date = [np.where(dates == u)[0] for u in np.unique(dates)]
    null = np.empty(n)
    for k in range(n):
        shuf = vals.copy()
        for ix in idx_by_date:
            if len(ix) > 1:
                shuf[ix] = RNG.permutation(shuf[ix])
        null[k] = pd.DataFrame({"a": shuf, "b": fwd})["a"].corr(pd.DataFrame({"a": shuf, "b": fwd})["b"], method="spearman")
    return float((np.abs(null) >= abs(actual)).mean())


def ls_portfolio(panel, sig, sign, q=0.2):
    rets = {}
    for dt, g in panel.dropna(subset=[sig, "fwd_ret"]).groupby("date"):
        if len(g) < 8:
            continue
        s = g[sig] * sign; hi = s.quantile(1 - q); lo = s.quantile(q)
        longs = g[s >= hi]["fwd_ret"]; shorts = g[s <= lo]["fwd_ret"]
        if len(longs) and len(shorts):
            rets[dt] = longs.mean() - shorts.mean()
    return pd.Series(rets).sort_index()


def ann_sharpe(w):
    if len(w) < 10 or w.std() == 0:
        return np.nan
    return float(w.mean() / w.std() * np.sqrt(52))


def deflated_sharpe(w, n_trials):
    r = w.dropna().values; T = len(r)
    if T < 20:
        return np.nan
    sr = r.mean() / r.std(ddof=1)
    from scipy import stats
    g3 = stats.skew(r); g4 = stats.kurtosis(r, fisher=False); e = 0.5772156649
    emax = np.sqrt(1.0 / T) * ((1 - e) * stats.norm.ppf(1 - 1.0 / n_trials)
                               + e * stats.norm.ppf(1 - 1.0 / (n_trials * np.e)))
    denom = np.sqrt(1 - g3 * sr + (g4 - 1) / 4.0 * sr ** 2)
    if denom <= 0:
        return np.nan
    return float(stats.norm.cdf(((sr - emax) * np.sqrt(T - 1)) / denom))


def topdecile_greenlight(panel, sig, sign):
    """任務要求: champion 綠燈日 × concentration top-decile 條件式 long-only 選股。
    對照三組: (a) 綠燈日 top-decile, (b) 全部日 top-decile, (c) 綠燈日全體均值 baseline。
    回傳每組平均週報酬(bps) 與 t-stat。"""
    def _longonly(sub):
        rets = []
        for dt, g in sub.dropna(subset=[sig, "fwd_ret"]).groupby("date"):
            if len(g) < 8:
                continue
            s = g[sig] * sign
            longs = g[s >= s.quantile(0.9)]["fwd_ret"]  # top-decile
            if len(longs):
                rets.append(longs.mean())
        return np.array(rets)
    green = panel[panel["champ_up"]]
    r_green_top = _longonly(green)
    r_all_top = _longonly(panel)
    # baseline: 綠燈日全體(不選集中度) 均值
    base = green.dropna(subset=["fwd_ret"]).groupby("date")["fwd_ret"].mean().values
    def _stat(a):
        if len(a) < 10:
            return (None, None, len(a))
        t = a.mean() / (a.std(ddof=1) / np.sqrt(len(a)))
        return (round(a.mean() * 1e4, 1), round(float(t), 2), len(a))
    return {"green_topdecile": _stat(r_green_top), "all_topdecile": _stat(r_all_top),
            "green_baseline": _stat(base),
            "excess_green_top_vs_base_bps": (round((r_green_top.mean() - base.mean()) * 1e4, 1)
                                             if len(r_green_top) > 5 and len(base) > 5 else None)}


SIGNALS = {"d_big": "千張大戶Δ", "z_big": "千張大戶z", "concentration": "合成集中(z_big-z_holders)"}


def run_study():
    panel = build_panel()
    res = {"universe": "80 mid/small-cap (千張大戶比中位 39.6%)", "n_obs": int(len(panel)),
           "n_stocks": int(panel["stock_id"].nunique()),
           "date_min": str(panel["date"].min().date()), "date_max": str(panel["date"].max().date()),
           "bull_frac": round(float(panel["bull"].mean()), 3),
           "champ_up_frac": round(float(panel["champ_up"].mean()), 3),
           "avg_names_per_week": round(panel.groupby("date").size().mean(), 1)}
    cut = panel["date"].quantile(0.7)
    is_p = panel[panel["date"] <= cut]; oos_p = panel[panel["date"] > cut]
    res["is_dates"] = f"{is_p['date'].min().date()}→{is_p['date'].max().date()}"
    res["oos_dates"] = f"{oos_p['date'].min().date()}→{oos_p['date'].max().date()}"
    pan = pd.read_parquet(PANEL); pan["date"] = pd.to_datetime(pan["date"])
    wk_sig = panel.groupby("date")["concentration"].mean().rename("agg_conc").reset_index()
    ch = pan[["date", "fut_foreign_oi"]].copy(); ch["d_champ"] = ch["fut_foreign_oi"].diff()
    m = wk_sig.merge(ch, on="date", how="left")
    res["champ_collinear_corr"] = _r(_spear(m["agg_conc"], m["d_champ"]))
    per = {}; n_trials = len(SIGNALS)
    for sig, name in SIGNALS.items():
        ic_all = _spear(panel[sig], panel["fwd_ret"]); ic_is = _spear(is_p[sig], is_p["fwd_ret"])
        ic_oos = _spear(oos_p[sig], oos_p["fwd_ret"])
        sign = float(np.sign(ic_is)) if np.isfinite(ic_is) and ic_is != 0 else 1.0
        pic_mom = partial_ic(panel, sig, ["mom20"]); pic_both = partial_ic(panel, sig, ["mom20", "frr_chg"])
        ic_bull = _spear(panel[panel["bull"]][sig], panel[panel["bull"]]["fwd_ret"])
        ic_bear = _spear(panel[~panel["bull"]][sig], panel[~panel["bull"]]["fwd_ret"])
        pp = perm_ic_p(panel, sig, ic_all, n=2000)
        ls_all = ls_portfolio(panel, sig, sign); ls_oos = ls_portfolio(oos_p, sig, sign)
        per[sig] = {"name": name, "IC_all": _r(ic_all), "IC_IS": _r(ic_is), "IC_OOS": _r(ic_oos),
                    "IS_sign": sign, "partial_IC_mom": _r(pic_mom), "partial_IC_mom+foreign": _r(pic_both),
                    "IC_bull": _r(ic_bull), "IC_bear": _r(ic_bear), "perm_p": _r(pp),
                    "LS_sharpe_all": _r(ann_sharpe(ls_all)), "LS_sharpe_OOS": _r(ann_sharpe(ls_oos)),
                    "DSR": _r(deflated_sharpe(ls_all, n_trials)),
                    "LS_mean_wk_bps": _r(ls_all.mean() * 1e4, 1), "n_weeks": len(ls_all)}
    per["_mom20_benchmark"] = {"IC_all": _r(_spear(panel["mom20"], panel["fwd_ret"])),
                               "corr_conc_mom": _r(_spear(panel["concentration"], panel["mom20"])),
                               "corr_dbig_mom": _r(_spear(panel["d_big"], panel["mom20"]))}
    res["signals"] = per
    # 任務要求: champion 綠燈 × top-decile 選股
    res["greenlight_topdecile"] = {s: topdecile_greenlight(panel, s, per[s]["IS_sign"]) for s in SIGNALS}
    return res


def _r(x, nd=4):
    return round(float(x), nd) if x is not None and np.isfinite(x) else None


if __name__ == "__main__":
    import json
    print(json.dumps(run_study(), ensure_ascii=False, indent=2))
