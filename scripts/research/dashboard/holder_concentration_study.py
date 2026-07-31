"""大戶持股集中度維度研究 — 千張大戶 / 集保戶數(REAL DATA, Phase-2).

用途
====
把「大戶持股集中度」這一格 (L2 個股籌碼核心) 補進 dashboard,用真資料實跑。
本維度是【個股橫斷面選股/風控】層,不是大盤擇時 (千萬別拿去當 champion=外資台指期
positioning 的替身)。子訊號:
  1) 千張大戶持股比 big_pct   — TDCC 集保「>1,000,000 股 (>1,000 張)」級距 percent
  2) 集保戶數 holders         — 各實際級距 people 合計 (breadth of ownership)
  3) 合成集中因子             — z(Δbig_pct) − z(Δholders) = 張數集中且人數收斂 = 偏多

資料源 (REAL, 已落地)
=====================
  - data/research/dashboard/holder_concentration_data.parquet
      FinMind TaiwanStockHoldingSharesPer, 164 檔大型股 (stock_market_value_daily 宇宙),
      週頻, 2023-01-07 → 2026-07-24 (183 週), 逐級距 people+percent。共 506,702 列。
  - data/stocks.db stock_close_adjusted (adj_close_v2, 還原股價) → forward return / 動能
  - data/stocks.db stock_shareholding_daily (foreign_remaining_ratio) → 外資託管汙染 control
  - data/research/chip_macro/panel.parquet (ix_close→regime; fut_foreign_oi→champion 共線)

方法論骨幹 (對齊 chip_macro/eval_stage4_newhypotheses.py):
  - 方向由 IN-SAMPLE IC 正負固定,不偷看 OOS。
  - IS/OOS 時間切分 + permutation (同橫斷面隨機) + 多頭子樣本 regime-conditioning。
  - 與價格動能共線 + 外資託管汙染是頭號陷阱 → partial IC 控制兩者。
  - 對 champion(fut_foreign_oi) 時序共線檢定 + long-short 組合 Deflated-Sharpe。

如何跑
======
  cd /Users/jackm4/goldenstocks
  .venv/bin/python scripts/research/dashboard/holder_concentration_study.py

非投資建議。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
DB = ROOT / "data" / "stocks.db"
DATA = ROOT / "data" / "research" / "dashboard" / "holder_concentration_data.parquet"
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
RNG = np.random.default_rng(11)

LEVEL_1000 = "more than 1,000,001"          # 嚴格千張大戶
DROP_LEVELS = {"total", "差異數調整（說明4）"}  # 非真實級距,不計入 holders


# --------------------------------------------------------------------------- #
# 1. 訊號建構 (逐級距長表 → 每股-週 big_pct / holders / 合成因子)
# --------------------------------------------------------------------------- #
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
    # holders 用對數變動 (人數量級跨股差百倍)
    wide["d_holders"] = wide.groupby("stock_id")["holders"].apply(
        lambda s: np.log(s).diff()).reset_index(drop=True)

    def _z(s):
        return (s - s.rolling(26, min_periods=8).mean()) / s.rolling(26, min_periods=8).std()

    wide["z_big"] = wide.groupby("stock_id")["d_big"].transform(_z)
    wide["z_holders"] = wide.groupby("stock_id")["d_holders"].transform(_z)
    wide["concentration"] = wide["z_big"] - wide["z_holders"]
    return wide


# --------------------------------------------------------------------------- #
# 2. 價格 / 動能 / 外資汙染 control / regime
# --------------------------------------------------------------------------- #
def _load_adj_prices(sids: list[str]) -> pd.DataFrame:
    ph = ",".join("?" * len(sids))
    q = (f"SELECT stock_id, trade_date AS date, adj_close_v2 AS px "
         f"FROM stock_close_adjusted WHERE stock_id IN ({ph}) AND trade_date>='2022-06-01'")
    with sqlite3.connect(DB) as c:
        px = pd.read_sql(q, c, params=sids)
    px["date"] = pd.to_datetime(px["date"])
    px["stock_id"] = px["stock_id"].astype(str)
    return px.sort_values(["stock_id", "date"]).reset_index(drop=True)


def _load_foreign(sids: list[str]) -> pd.DataFrame:
    ph = ",".join("?" * len(sids))
    q = (f"SELECT stock_id, trade_date AS date, foreign_remaining_ratio AS frr "
         f"FROM stock_shareholding_daily WHERE stock_id IN ({ph}) AND trade_date>='2022-06-01'")
    with sqlite3.connect(DB) as c:
        f = pd.read_sql(q, c, params=sids)
    f["date"] = pd.to_datetime(f["date"])
    f["stock_id"] = f["stock_id"].astype(str)
    return f.sort_values(["stock_id", "date"]).reset_index(drop=True)


def _regime() -> pd.DataFrame:
    p = pd.read_parquet(PANEL)
    p["date"] = pd.to_datetime(p["date"])
    p = p.sort_values("date")
    p["ma200"] = p["ix_close"].rolling(200, min_periods=200).mean()
    p["bull"] = (p["ix_close"] > p["ma200"]) & (p["ma200"].diff(25) > 0)
    return p[["date", "bull"]]


def build_panel(publish_lag_days: int = 5, fwd_days: int = 5) -> pd.DataFrame:
    """point-in-time: 集保資料日 d (週最後交易日) 於次週公布 → entry = 第一個 >= d+lag 的交易日。
    fwd_ret = px[entry+fwd_days]/px[entry]-1 (下一週報酬,還原股價)。mom20 = 進場前 20 日報酬。
    frr_chg = 進場前 ~1 週外資可投資餘額比變動 (汙染 control,-Δfrr≈外資買超)。
    """
    sig = build_signals()
    sids = sorted(sig["stock_id"].unique())
    px = _load_adj_prices(sids)
    frn = _load_foreign(sids)
    reg = _regime()

    px_by = {s: g.reset_index(drop=True) for s, g in px.groupby("stock_id")}
    frn_by = {s: g.reset_index(drop=True) for s, g in frn.groupby("stock_id")}

    out = []
    for sid, g in sig.groupby("stock_id"):
        p = px_by.get(sid)
        if p is None or len(p) < 60:
            continue
        pdt = p["date"].values
        fr = frn_by.get(sid)
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
            entry_date = p["date"].iloc[j]
            frr_chg = np.nan
            if fr is not None:
                fj = int(np.searchsorted(fr["date"].values, np.datetime64(entry_date), side="right")) - 1
                if fj >= 6:
                    a = fr["frr"].iloc[fj]; b = fr["frr"].iloc[fj - 5]
                    if pd.notna(a) and pd.notna(b):
                        frr_chg = a - b
            out.append({
                "stock_id": sid, "date": entry_date,
                "d_big": row["d_big"], "z_big": row["z_big"],
                "z_holders": row["z_holders"], "concentration": row["concentration"],
                "big_pct": row["big_pct"],
                "fwd_ret": p1 / p0 - 1.0, "mom20": p0 / pm - 1.0, "frr_chg": frr_chg,
            })
    panel = pd.DataFrame(out)
    panel = panel.merge(reg, on="date", how="left")
    panel["bull"] = panel["bull"].fillna(False)
    return panel.sort_values("date").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 3. 統計工具
# --------------------------------------------------------------------------- #
def _spear(a, b) -> float:
    d = pd.DataFrame({"a": a, "b": b}).dropna()
    return d["a"].corr(d["b"], method="spearman") if len(d) > 30 else np.nan


def partial_ic(panel: pd.DataFrame, sig: str, ctrls: list[str]) -> float:
    """rank 殘差化: signal 與 fwd_ret 各自對 controls 迴歸取殘差後相關 (去共線)."""
    cols = [sig, "fwd_ret"] + ctrls
    d = panel[cols].dropna()
    if len(d) < 80:
        return np.nan
    r = d.rank()
    X = np.column_stack([np.ones(len(r))] + [r[c].values for c in ctrls])
    def resid(y):
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        return y - X @ beta
    x = resid(r[sig].values); y = resid(r["fwd_ret"].values)
    return float(pd.Series(x).corr(pd.Series(y)))


def perm_ic_p(panel: pd.DataFrame, sig: str, actual: float, n: int = 2000) -> float:
    """每個 date 橫斷面內打亂 signal → null IC 分布, 雙尾 p."""
    d = panel[[sig, "fwd_ret", "date"]].dropna()
    dates = d["date"].values; vals = d[sig].values.copy(); fwd = d["fwd_ret"].values
    idx_by_date = [np.where(dates == u)[0] for u in np.unique(dates)]
    null = np.empty(n)
    for k in range(n):
        shuf = vals.copy()
        for ix in idx_by_date:
            if len(ix) > 1:
                shuf[ix] = RNG.permutation(shuf[ix])
        dd = pd.DataFrame({"a": shuf, "b": fwd})
        null[k] = dd["a"].corr(dd["b"], method="spearman")
    return float((np.abs(null) >= abs(actual)).mean())


def ls_portfolio(panel: pd.DataFrame, sig: str, sign: float, q: float = 0.2) -> pd.Series:
    """每週依 signal 分位, top-quantile 多 / bottom-quantile 空 (依 IS 方向 sign), 等權週報酬."""
    rets = {}
    for dt, g in panel.dropna(subset=[sig, "fwd_ret"]).groupby("date"):
        if len(g) < 10:
            continue
        s = g[sig] * sign
        hi = s.quantile(1 - q); lo = s.quantile(q)
        longs = g[s >= hi]["fwd_ret"]; shorts = g[s <= lo]["fwd_ret"]
        if len(longs) and len(shorts):
            rets[dt] = longs.mean() - shorts.mean()
    return pd.Series(rets).sort_index()


def ann_sharpe(weekly: pd.Series) -> float:
    if len(weekly) < 10 or weekly.std() == 0:
        return np.nan
    return float(weekly.mean() / weekly.std() * np.sqrt(52))


def deflated_sharpe(weekly: pd.Series, n_trials: int) -> float:
    """Bailey & Lopez de Prado DSR: P(true SR>0 | observed), 對 n_trials 搜尋 + 偏態/峰度折減."""
    r = weekly.dropna().values
    T = len(r)
    if T < 20:
        return np.nan
    sr = r.mean() / r.std(ddof=1)
    from scipy import stats
    g3 = stats.skew(r); g4 = stats.kurtosis(r, fisher=False)
    # 期望最大 Sharpe (n_trials 獨立試驗, SR~N(0,1/T)) — Bailey-LdP 近似
    e = 0.5772156649
    emax = np.sqrt(1.0 / T) * ((1 - e) * stats.norm.ppf(1 - 1.0 / n_trials)
                               + e * stats.norm.ppf(1 - 1.0 / (n_trials * np.e)))
    denom = np.sqrt(1 - g3 * sr + (g4 - 1) / 4.0 * sr ** 2)
    if denom <= 0:
        return np.nan
    dsr = stats.norm.cdf(((sr - emax) * np.sqrt(T - 1)) / denom)
    return float(dsr)


# --------------------------------------------------------------------------- #
# 4. 正式研究
# --------------------------------------------------------------------------- #
SIGNALS = {"d_big": "千張大戶Δ", "z_big": "千張大戶z", "concentration": "合成集中(z_big-z_holders)"}


def run_study() -> dict:
    panel = build_panel()
    res = {"n_obs": int(len(panel)), "n_stocks": int(panel["stock_id"].nunique()),
           "date_min": str(panel["date"].min().date()), "date_max": str(panel["date"].max().date()),
           "bull_frac": round(float(panel["bull"].mean()), 3),
           "avg_names_per_week": round(panel.groupby("date").size().mean(), 1)}

    cut = panel["date"].quantile(0.7)
    is_p = panel[panel["date"] <= cut]; oos_p = panel[panel["date"] > cut]
    res["is_dates"] = f"{is_p['date'].min().date()}→{is_p['date'].max().date()}"
    res["oos_dates"] = f"{oos_p['date'].min().date()}→{oos_p['date'].max().date()}"

    # champion 時序共線: 每週橫斷面平均 concentration vs fut_foreign_oi 變動
    pan = pd.read_parquet(PANEL); pan["date"] = pd.to_datetime(pan["date"])
    wk_sig = panel.groupby("date")["concentration"].mean().rename("agg_conc").reset_index()
    ch = pan[["date", "fut_foreign_oi"]].copy()
    ch["d_champ"] = ch["fut_foreign_oi"].diff()
    m = wk_sig.merge(ch, on="date", how="left")
    res["champ_collinear_corr"] = round(_spear(m["agg_conc"], m["d_champ"]), 4)

    per = {}
    n_trials = len(SIGNALS)
    for sig, name in SIGNALS.items():
        ic_all = _spear(panel[sig], panel["fwd_ret"])
        ic_is = _spear(is_p[sig], is_p["fwd_ret"])
        ic_oos = _spear(oos_p[sig], oos_p["fwd_ret"])
        sign = float(np.sign(ic_is)) if np.isfinite(ic_is) and ic_is != 0 else 1.0
        pic_mom = partial_ic(panel, sig, ["mom20"])
        pic_both = partial_ic(panel, sig, ["mom20", "frr_chg"])
        ic_bull = _spear(panel[panel["bull"]][sig], panel[panel["bull"]]["fwd_ret"])
        ic_bear = _spear(panel[~panel["bull"]][sig], panel[~panel["bull"]]["fwd_ret"])
        pp = perm_ic_p(panel, sig, ic_all, n=2000)
        # long-short 組合 (方向由 IS 定)
        ls_all = ls_portfolio(panel, sig, sign)
        ls_oos = ls_portfolio(oos_p, sig, sign)
        sh_all = ann_sharpe(ls_all); sh_oos = ann_sharpe(ls_oos)
        dsr = deflated_sharpe(ls_all, n_trials)
        per[sig] = {
            "name": name, "IC_all": _r(ic_all), "IC_IS": _r(ic_is), "IC_OOS": _r(ic_oos),
            "IS_sign": sign, "partial_IC_mom": _r(pic_mom), "partial_IC_mom+foreign": _r(pic_both),
            "IC_bull": _r(ic_bull), "IC_bear": _r(ic_bear), "perm_p": _r(pp),
            "LS_sharpe_all": _r(sh_all), "LS_sharpe_OOS": _r(sh_oos), "DSR": _r(dsr),
            "LS_mean_wk_bps": _r(ls_all.mean() * 1e4, 1), "n_weeks": len(ls_all),
        }
    # momentum benchmark
    per["_mom20_benchmark"] = {"IC_all": _r(_spear(panel["mom20"], panel["fwd_ret"])),
                               "corr_conc_mom": _r(_spear(panel["concentration"], panel["mom20"])),
                               "corr_dbig_mom": _r(_spear(panel["d_big"], panel["mom20"]))}
    res["signals"] = per
    return res


def _r(x, nd=4):
    return round(float(x), nd) if x is not None and np.isfinite(x) else None


if __name__ == "__main__":
    import json
    r = run_study()
    print(json.dumps(r, ensure_ascii=False, indent=2))
