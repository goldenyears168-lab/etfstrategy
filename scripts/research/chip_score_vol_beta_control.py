#!/usr/bin/env python3
"""名單是不是偽裝成籌碼訊號的高波動因子？

**動機**：景碩 Beta 1.81（優於 99.90% 企業）、漲跌幅標準差 5.2（98.58%）。
若 v4／zp 分數系統性地選到高波動股，名單的「超額報酬」就有一部分只是
beta 曝險而非 alpha —— 而且高波動股的個股離散度更大，正好對應
「單檔事件輾壓組平均」那個一直出現的現象。

**這件事必須先驗**：如果名單本質是高波動因子，加任何新因子都是白搭。

三個檢定：
  1. 橫斷面上，分數與波動／beta 的相關性有多高
  2. **雙重排序**：在同一個波動分層之內，分數的多空價差還在不在
  3. 迴歸：fwd_ret ~ score + vol + beta，看 score 係數還剩多少

口徑一律用**開→收**（可執行），並已扣掉當日橫斷面均值。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect_ro

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "reports" / "research" / "chip-signal-daily-horizon" / "turnover_panel.pkl"


def add_risk(d: pd.DataFrame) -> pd.DataFrame:
    """從完整日線算 60 日波動與 beta（不能只用面板列，面板已被流動性濾掉）。"""
    c = connect_ro()
    px = pd.read_sql_query(
        """SELECT stock_id, trade_date, source, close FROM stock_daily_bars
            WHERE trade_date >= '2022-06-01' AND close IS NOT NULL""", c)
    px["rk"] = px.source.map({"finmind": 0, "twse_mi_index": 1, "tpex_daily": 2}).fillna(9)
    px = (px.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
            .sort_values(["stock_id", "trade_date"]))
    g = px.groupby("stock_id", group_keys=False)
    px["ret"] = g.close.pct_change()
    px = px[px.ret.abs() < 0.5]                      # 擋掉未還原的分割
    mkt = px.groupby("trade_date").ret.mean().rename("mret")
    px = px.join(mkt, on="trade_date")
    g = px.groupby("stock_id", group_keys=False)
    px["vol60"] = g.ret.transform(lambda s: s.rolling(60, min_periods=30).std())
    cov = g.apply(lambda x: x.ret.rolling(60, min_periods=30).cov(x.mret))
    px["cov60"] = cov.reset_index(level=0, drop=True) if isinstance(cov.index, pd.MultiIndex) else cov
    px["mvar60"] = px.groupby("trade_date").mret.transform("first")
    mv = mkt.rolling(60, min_periods=30).var().rename("mvar")
    px = px.drop(columns=["mvar60"]).join(mv, on="trade_date")
    px["beta60"] = px.cov60 / px.mvar
    return d.merge(px[["stock_id", "trade_date", "vol60", "beta60"]],
                   on=["stock_id", "trade_date"], how="left")


def xs_corr(d: pd.DataFrame, a: str, b: str) -> float:
    """逐日算 Spearman 再平均（橫斷面相關，不是把所有 stock-day 混在一起）。"""
    r = d.groupby("trade_date").apply(
        lambda g: g[a].corr(g[b], method="spearman") if g[a].notna().sum() > 30 else np.nan,
        include_groups=False)
    return r.mean()


def spread(g: pd.DataFrame, col: str, n: int = 30) -> float:
    s = g.sort_values("score")
    return s[col].head(n).mean() - s[col].tail(n).mean()


def main() -> int:
    d = pd.read_pickle(PANEL)
    d = add_risk(d).dropna(subset=["vol60", "beta60"])
    def dead(s):
        return np.where(np.abs(s) >= 0.1, s, 0.0)
    d["v4"] = sum(dead(d[z]) for z in ("z1", "zp", "zu", "zf", "z6"))
    d["zp_only"] = dead(d.zp)
    print(f"面板 {len(d):,} stock-day · {d.trade_date.nunique()} 日 · "
          f"{d.trade_date.min()}~{d.trade_date.max()}\n")

    print("=== 1. 分數 vs 風險（逐日 Spearman 的平均）===")
    for sc in ("v4", "zp_only"):
        print(f"  {sc:<9} vs 60日波動 {xs_corr(d, sc, 'vol60'):+.3f}"
              f"   vs beta {xs_corr(d, sc, 'beta60'):+.3f}")
    print("  （分數越大＝越偏空。正相關代表偏空名單偏向高波動／高 beta）")

    print("\n=== 2. 雙重排序：波動分層內，分數的多空價差還在嗎（開→收）===")
    for sc in ("v4", "zp_only"):
        d["score"] = d[sc]
        d["vq"] = d.groupby("trade_date").vol60.transform(
            lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop"))
        print(f"\n  【{sc}】")
        print(f"    {'波動分層':<12}{'日數':>6}{'開→收價差':>12}{'t':>8}")
        allsp = []
        for q in range(5):
            sub = d[d.vq == q]
            sp = sub.groupby("trade_date").apply(
                lambda g: spread(g, "oc", 15) if len(g) >= 60 else np.nan,
                include_groups=False).dropna()
            if len(sp) < 30:
                continue
            t = sp.mean() / (sp.std(ddof=1) / np.sqrt(len(sp)))
            lab = f"Q{q+1}" + ("（最低波動）" if q == 0 else "（最高波動）" if q == 4 else "")
            print(f"    {lab:<12}{len(sp):>6}{sp.mean()*100:>+11.4f}%{t:>+8.2f}")
            allsp.append(sp.rename(q))
        if allsp:
            pooled = pd.concat(allsp, axis=1).mean(axis=1).dropna()
            t = pooled.mean() / (pooled.std(ddof=1) / np.sqrt(len(pooled)))
            print(f"    {'波動中性合計':<11}{len(pooled):>6}{pooled.mean()*100:>+11.4f}%{t:>+8.2f}")
        # 對照：不控制波動
        raw = d.groupby("trade_date").apply(
            lambda g: spread(g, "oc", 30) if len(g) >= 120 else np.nan,
            include_groups=False).dropna()
        t = raw.mean() / (raw.std(ddof=1) / np.sqrt(len(raw)))
        print(f"    {'（未控制對照）':<10}{len(raw):>6}{raw.mean()*100:>+11.4f}%{t:>+8.2f}")

    print("\n=== 3. 逐日橫斷面迴歸 oc ~ score + vol60 + beta60（Fama-MacBeth）===")
    for sc in ("v4", "zp_only"):
        coefs = []
        for t_, g in d.groupby("trade_date"):
            g = g.dropna(subset=[sc, "vol60", "beta60", "oc"])
            if len(g) < 80:
                continue
            X = np.column_stack([np.ones(len(g)), g[sc], g.vol60, g.beta60])
            try:
                coefs.append(np.linalg.lstsq(X, g.oc.to_numpy(), rcond=None)[0])
            except np.linalg.LinAlgError:
                pass
        C = np.array(coefs)
        print(f"  【{sc}】n={len(C)} 日")
        for j, nm in enumerate(["常數", "score", "vol60", "beta60"]):
            m = C[:, j].mean(); t = m / (C[:, j].std(ddof=1) / np.sqrt(len(C)))
            print(f"    {nm:<8}{m*100:>+10.5f}%   t={t:+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
