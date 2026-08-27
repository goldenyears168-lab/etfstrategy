#!/usr/bin/env python3
"""9B2Y：最後一輪切法 —— 零股 vs 整張、以及「程式候選」複合條件。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sps

DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"
m = pd.read_pickle(DIR / "branch_9B2Y_joined.pkl")
m = m[m.dt_vol > 0].dropna(subset=["spread"]).copy()
m["odd_b"] = m.buy_vol % 1000 != 0
m["odd_s"] = m.sell_vol % 1000 != 0
nd = m.trade_date.nunique()


def rep(d: pd.DataFrame, lab: str) -> None:
    if len(d) < 50:
        print(f"  {lab:<26} n={len(d)} 太少")
        return
    per = d.groupby("trade_date").size()
    vc = d.stock_id.value_counts()
    g, n = d.dt_pnl.sum(), d.dt_noti.sum()
    dg = d.groupby("trade_date").apply(
        lambda x: x.dt_pnl.sum() / x.dt_noti.sum() * 100, include_groups=False).dropna()
    t = sps.ttest_1samp(dg, 0)
    print(f"  {lab:<26} n={len(d):>6,} 檔={d.stock_id.nunique():>4} "
          f"日筆={per.median():>5.0f} CV={per.std()/per.mean():.2f} "
          f"var/mean={per.var()/per.mean():>5.2f} 出席={d.trade_date.nunique()/nd:.2f} "
          f"毛={g/n*100:+.4f}% 勝={((d.spread>0).mean()*100):.1f}% "
          f"名目={n/1e8:>6.1f}億 Top10檔佔={vc.head(10).sum()/len(d):.1%} "
          f"t(日)={t.statistic:+.2f} p={t.pvalue:.3g}")


print("=== 零股 vs 整張 ===")
rep(m, "全分點")
rep(m[~m.odd_b & ~m.odd_s], "雙邊整張")
rep(m[m.odd_b | m.odd_s], "含零股")
rep(m[(~m.odd_b) & (~m.odd_s) & (m.rt > 0.95)], "雙邊整張 × 純當沖")
rep(m[(~m.odd_b) & (~m.odd_s) & (m.dt_noti > 5e6)], "雙邊整張 × 名目>500萬")

print("\n=== 程式候選複合條件（希望能篩出規格化子群）===")
rep(m[(m.n_lvl >= 8) & (m.rt > 0.9)], "廣灑價位≥8 × 純當沖")
rep(m[(m.n_lvl >= 16)], "價位≥16（掃單）")
freq = m.groupby("stock_id").size()
m["nf"] = m.stock_id.map(freq)
rep(m[(m.nf >= 100)], "近乎天天做的核心股")
rep(m[(m.nf >= 100) & (m.rt > 0.9)], "核心股 × 純當沖")
rep(m[(m.nf >= 100) & (m.rt < 0.3)], "核心股 × 純方向")
rep(m[(m.part > 0.005)], "參與率>0.5%（相對大咖）")
rep(m[(m.part < 0.0005)], "參與率<0.05%（相對小咖）")

print("\n=== 各切法的『毛邊際 vs 全分點』差距排序 ===")
base = m.dt_pnl.sum() / m.dt_noti.sum() * 100
print(f"  全分點基準 {base:+.4f}%")

print("\n=== 核心股清單（出現≥100 日）===")
core = freq[freq >= 100].sort_values(ascending=False)
print(f"  {len(core)} 檔:", ", ".join(core.head(25).index.tolist()))

print("\n=== 檢查：這 25 檔核心股是不是就是全市場最熱門當沖股（散戶特徵）===")
from stock_db import connect_ro  # noqa: E402
c = connect_ro()
top = pd.read_sql_query(
    """SELECT stock_id, AVG(volume) v FROM stock_daily_bars
        WHERE trade_date>='2026-02-02' GROUP BY stock_id ORDER BY v DESC LIMIT 300""", c)
print(f"  核心股落在全市場成交量 Top300 的比例: "
      f"{core.index.isin(top.stock_id).mean():.1%}")
allv = pd.read_sql_query(
    """SELECT stock_id, AVG(volume) v FROM stock_daily_bars
        WHERE trade_date>='2026-02-02' GROUP BY stock_id""", c).set_index("stock_id").v
pct = allv.rank(pct=True)
print(f"  核心股的全市場成交量百分位中位數: {pct.reindex(core.index).median():.3f}")
print(f"  純當沖子群所有標的的成交量百分位中位數: "
      f"{pct.reindex(m[m.rt > 0.95].stock_id.unique()).median():.3f}")
