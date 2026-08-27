#!/usr/bin/env python3
"""chip-orthogonal-rebuild 對抗覆核：第三路徑重算 F1 / F6 / F1xF6 AND 格。

獨立於 A（panel.pkl 長表 + lstsq 全回歸）與 B（pivot + qcut）：
  - 自建 pivot 面板，FM 斜率用 Frisch-Waugh 殘差化（數值路徑不同）
  - NW t 用 statsmodels OLS HAC(maxlags=5)（第三個 NW 實作）
  - AND 格另算股票層集中度（top-k 貢獻占比）與日集中度
附帶仲裁：F7 twse_mi_margn 全市場段 neutral_t（報告 +2.77 vs B +3.29）、
F5 單控 gap 的 t（報告 −2.30，硬編碼 note 驗證）。
只讀 DB / panel.pkl；輸出 stdout。
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd
import statsmodels.api as sm

from stock_db import DEFAULT_DB_PATH

WIN_START, WIN_END = "2024-07-01", "2026-08-26"
LOAD_START = "2023-12-01"
MIN_N = 30


def nw_t_sm(x):
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    res = sm.OLS(x, np.ones(len(x))).fit(cov_type="HAC",
                                         cov_kwds={"maxlags": 5, "kernel": "bartlett"})
    return float(res.params[0]), float(res.tvalues[0])


def zs(v):
    v = np.asarray(v, float)
    sd = np.nanstd(v)
    return np.zeros_like(v) if (not np.isfinite(sd) or sd == 0) else (v - np.nanmean(v)) / sd


def fw_slope(g, col):
    """Frisch-Waugh：r 與 frank 各自對 [1, z(vol60), z(gap), z(turnover)] 殘差化。"""
    n = len(g)
    fr = (g[col].rank(method="average").to_numpy() - 1) / (n - 1) - 0.5
    C = np.column_stack([np.ones(n), zs(g.vol60), zs(g.gap), zs(g.turnover)])
    y = g.r_oc.to_numpy(float)
    Pc = C @ np.linalg.pinv(C)
    ry = y - Pc @ y
    rf = fr - Pc @ fr
    denom = rf @ rf
    return float(rf @ ry / denom) if denom > 0 else np.nan


def main():
    c = sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)
    cal = pd.read_sql_query(
        """SELECT trade_date, COUNT(*) n FROM stock_daily_bars
            WHERE source IN ('twse_mi_index','tpex_daily','finmind')
              AND trade_date BETWEEN ? AND ?
            GROUP BY trade_date HAVING n > 500 ORDER BY trade_date""",
        c, params=(LOAD_START, WIN_END)).trade_date.tolist()
    assert "2026-07-10" not in cal

    bars = pd.read_sql_query(
        """SELECT stock_id, trade_date, open, close, volume, source
             FROM stock_daily_bars WHERE trade_date BETWEEN ? AND ?""",
        c, params=(LOAD_START, WIN_END))
    bars = bars[bars.trade_date.isin(set(cal))]
    prio = {"twse_mi_index": 0, "tpex_daily": 1, "finmind": 2, "yfinance": 3}
    bars["p"] = bars.source.map(prio).fillna(9)
    b0 = len(bars)
    bars = (bars.sort_values(["stock_id", "trade_date", "p"])
                .drop_duplicates(["stock_id", "trade_date"], keep="first"))
    print(f"bars dedup: {b0} -> {len(bars)}")

    piv = lambda v: bars.pivot(index="trade_date", columns="stock_id", values=v).reindex(cal)
    O, X, V = piv("open"), piv("close"), piv("volume")
    r_oc = X.shift(-1) / O.shift(-1) - 1
    gap = O.shift(-1) / X - 1
    vol60 = (X / X.shift(1) - 1).rolling(60, min_periods=40).std()
    vol20 = V.rolling(20, min_periods=20).mean()
    universe = (X >= 10) & (vol20 > 300_000)

    # F1 z1（SBL）＋ shares（short_limit×4）
    h = pd.read_sql_query(
        """SELECT stock_id, trade_date, sbl_balance, short_limit
             FROM stock_short_interest_daily WHERE trade_date BETWEEN ? AND ?""",
        c, params=(LOAD_START, WIN_END))
    h = h[h.trade_date.isin(set(cal))].drop_duplicates(["stock_id", "trade_date"])
    bal = h.pivot(index="trade_date", columns="stock_id", values="sbl_balance").reindex(cal)
    shl = h.pivot(index="trade_date", columns="stock_id", values="short_limit").reindex(cal)
    shares = (shl * 4).where(shl * 4 > 0)
    d = bal.diff()
    z1 = (d - d.rolling(60, min_periods=30).mean()) / d.rolling(60, min_periods=30).std()

    # turnover（A 規格：vol/股本，缺→vol/vol20）
    sh_al, V_al = shares.align(V, join="right")
    turnover = (V_al / sh_al).where(sh_al.notna(), V_al / vol20.where(vol20 > 0))

    # F6 retail（自寫 PIT：as_of → 首個 >as_of 的交易日掛因子，ffill 10 交易日）
    w = pd.read_sql_query(
        """SELECT stock_id, as_of_date, source, level, percent
             FROM stock_holding_dispersion_weekly WHERE as_of_date >= '2024-05-01'""", c)
    c.close()
    w = w[w.level.isin({str(i) for i in range(1, 9)})]
    agg = (w.groupby(["stock_id", "as_of_date", "source"], as_index=False).percent.sum())
    agg["p"] = np.where(agg.source == "tdcc", 0, 1)
    agg = (agg.sort_values(["stock_id", "as_of_date", "p"])
              .drop_duplicates(["stock_id", "as_of_date"], keep="first"))
    cal_a = np.array(cal)
    pos = np.searchsorted(cal_a, agg.as_of_date.values, side="right")
    ok = pos < len(cal_a)
    agg = agg[ok].copy()
    agg["sig"] = cal_a[pos[ok]]
    agg = agg.sort_values("as_of_date").drop_duplicates(["stock_id", "sig"], keep="last")
    retail = (agg.pivot(index="sig", columns="stock_id", values="percent")
                 .reindex(cal).ffill(limit=10))

    # 長表
    def melt(M, name):
        s = M.stack()
        s.name = name
        return s
    frames = [melt(r_oc, "r_oc"), melt(gap, "gap"), melt(vol60, "vol60"),
              melt(turnover, "turnover"), melt(universe, "universe"),
              melt(z1, "z1"), melt(retail, "retail")]
    df = pd.concat(frames, axis=1).reset_index()
    df.columns = ["trade_date", "stock_id", *[f.name for f in frames]]
    df = df[(df.trade_date >= WIN_START) & (df.trade_date <= WIN_END)]
    df = df[df.universe.astype(bool)].dropna(subset=["r_oc", "gap", "vol60", "turnover"])

    for col, ref_t, ref_m in (("z1", -5.20, -0.0831), ("retail", -4.56, -0.1683)):
        slopes = []
        for _, g in df.dropna(subset=[col]).groupby("trade_date", sort=True):
            if len(g) >= MIN_N:
                slopes.append(fw_slope(g, col))
        m, t = nw_t_sm(slopes)
        print(f"[third-path] {col}: n_days={len(slopes)} slope={m*100:+.4f}%/d "
              f"t={t:+.3f}  (A: {ref_m:+.4f}% / {ref_t:+.2f}) "
              f"Δt={abs(t-ref_t)/abs(ref_t):.1%}")

    # ---- F1xF6 AND 格 + 集中度 ----
    g2 = df.dropna(subset=["z1", "retail"])
    sp, dates = [], []
    contrib = {}
    for dt, g in g2.groupby("trade_date", sort=True):
        if len(g) < MIN_N:
            continue
        ra = 1.0 - g.z1.rank(method="first", pct=True)     # 負向：低值=偏多
        rb = 1.0 - g.retail.rank(method="first", pct=True)
        L = g[(ra > 0.8) & (rb > 0.8)]
        S = g[(ra <= 0.2) & (rb <= 0.2)]
        if len(L) < 3 or len(S) < 3:
            continue
        sp.append(float(L.r_oc.mean() - S.r_oc.mean()))
        dates.append(dt)
        for sid, r in zip(L.stock_id, L.r_oc):
            contrib[sid] = contrib.get(sid, 0.0) + r / len(L)
        for sid, r in zip(S.stock_id, S.r_oc):
            contrib[sid] = contrib.get(sid, 0.0) - r / len(S)
    m, t = nw_t_sm(sp)
    print(f"[third-path] F1xF6 AND: n_days={len(sp)} spread={m*100:+.4f}%/d t={t:+.3f} "
          f"(report +0.2348 / 4.88)")
    tot = sum(sp)
    cs = pd.Series(contrib).sort_values(ascending=False)
    print(f"  股票集中度: n_stocks={len(cs)}; top1={cs.iloc[0]/tot:.1%} "
          f"top5={cs.iloc[:5].sum()/tot:.1%} top10={cs.iloc[:10].sum()/tot:.1%} "
          f"top20={cs.iloc[:20].sum()/tot:.1%}")
    ds = pd.Series(sp, index=dates).sort_values(ascending=False)
    print(f"  日集中度: top5日={ds.iloc[:5].sum()/tot:.1%} top20日={ds.iloc[:20].sum()/tot:.1%}; "
          f"日勝率={float((ds>0).mean()):.1%}")
    # 去掉貢獻最大的 10 檔重算
    drop = set(cs.index[:10])
    sp2 = []
    for dt, g in g2.groupby("trade_date", sort=True):
        if len(g) < MIN_N:
            continue
        g = g[~g.stock_id.isin(drop)]
        ra = 1.0 - g.z1.rank(method="first", pct=True)
        rb = 1.0 - g.retail.rank(method="first", pct=True)
        L, S = g[(ra > 0.8) & (rb > 0.8)], g[(ra <= 0.2) & (rb <= 0.2)]
        if len(L) < 3 or len(S) < 3:
            continue
        sp2.append(float(L.r_oc.mean() - S.r_oc.mean()))
    m2, t2 = nw_t_sm(sp2)
    print(f"  剔除top10貢獻股重算: spread={m2*100:+.4f}%/d t={t2:+.3f} (n_days={len(sp2)})")

    # ---- 仲裁：panel.pkl 上 F7 全市場段 & F5 單控 gap ----
    panel = pd.read_pickle("reports/research/chip-orthogonal-rebuild/panel.pkl")
    uni = panel[panel.in_universe].dropna(subset=["r_oc", "vol60", "gap", "turnover"])
    for lab, lo, hi in (("F7 finmind era", WIN_START, "2026-05-31"),
                        ("F7 twse_mi_margn era", "2026-06-01", WIN_END),
                        ("F7 twse era(~08-19)", "2026-06-01", "2026-08-19")):
        sub = uni[(uni.trade_date >= lo) & (uni.trade_date <= hi)].dropna(subset=["margin"])
        slopes = [fw_slope(g, "margin") for _, g in sub.groupby("trade_date") if len(g) >= MIN_N]
        m, t = nw_t_sm(slopes)
        print(f"[arb] {lab}: n_days={len(slopes)} slope={m*100:+.4f}%/d t={t:+.3f}")
    # F5 單控 gap
    sub = uni[uni.trade_date <= "2026-07-16"].dropna(subset=["z6"])
    slopes = []
    for _, g in sub.groupby("trade_date"):
        if len(g) < MIN_N:
            continue
        n = len(g)
        fr = (g.z6.rank(method="average").to_numpy() - 1) / (n - 1) - 0.5
        C = np.column_stack([np.ones(n), zs(g.gap)])
        y = g.r_oc.to_numpy(float)
        Pc = C @ np.linalg.pinv(C)
        ry, rf = y - Pc @ y, fr - Pc @ fr
        slopes.append(float(rf @ ry / (rf @ rf)))
    m, t = nw_t_sm(slopes)
    print(f"[arb] F5 z6 只控 gap: n_days={len(slopes)} t={t:+.3f} (note 稱 -2.30)")


if __name__ == "__main__":
    main()
