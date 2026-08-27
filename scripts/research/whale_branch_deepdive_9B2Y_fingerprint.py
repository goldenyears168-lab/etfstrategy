#!/usr/bin/env python3
"""9B2Y：規格化／持續性指紋 —— 程式 vs 散戶群的行為區辨（含同儕基準）。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"
W0, W1 = "2026-02-02", "2026-08-26"


def prep(tid: str) -> pd.DataFrame | None:
    f = DIR / f"branch_{tid}_pricelevels.pkl"
    if not f.exists():
        return None
    d = pd.read_pickle(f)
    d = d[(d.trade_date >= W0) & (d.trade_date <= W1)].drop_duplicates(
        ["stock_id", "trade_date"]).copy()
    d["bl"] = d.buy_vol / 1000.0
    d["sl"] = d.sell_vol / 1000.0
    d["mn"] = d[["buy_vol", "sell_vol"]].min(axis=1)
    d["mx"] = d[["buy_vol", "sell_vol"]].max(axis=1)
    d["rt"] = d.mn / d.mx.replace(0, np.nan)
    return d


print("=== 1. 張數規格化（程式常見固定 lot；散戶群是冪律分布）===")
print(f"{'分點':<7}{'n':>8}{'=1張%':>7}{'≤2張%':>7}{'整張%':>7}{'零股%':>7}"
      f"{'中位張':>7}{'p90張':>8}{'眾數':>7}{'眾數佔比':>9}")
for tid in ["9B2Y", "9661", "8888", "9268", "9800"]:
    d = prep(tid)
    if d is None:
        continue
    b = d.loc[d.bl > 0, "bl"]
    md = b.mode()
    print(f"{tid:<7}{len(b):>8,}{(b == 1).mean()*100:>7.1f}{(b <= 2).mean()*100:>7.1f}"
          f"{(d.buy_vol[d.buy_vol > 0] % 1000 == 0).mean()*100:>7.1f}"
          f"{(d.buy_vol[d.buy_vol > 0] % 1000 != 0).mean()*100:>7.1f}"
          f"{b.median():>7.1f}{b.quantile(.9):>8.1f}{md.iloc[0]:>7.2f}"
          f"{(b == md.iloc[0]).mean()*100:>8.1f}%")

print("\n=== 2. 標的持續性：今天做的名字明天還在嗎 ===")
for tid in ["9B2Y", "9661", "8888", "9268"]:
    d = prep(tid)
    if d is None:
        continue
    days = sorted(d.trade_date.unique())
    sets = {k: set(g.stock_id) for k, g in d.groupby("trade_date")}
    ov = [len(sets[days[i]] & sets[days[i + 1]]) / len(sets[days[i]])
          for i in range(len(days) - 1) if sets.get(days[i])]
    # 純當沖子群
    s = d[d.rt > 0.95]
    sd = sorted(s.trade_date.unique())
    ss = {k: set(g.stock_id) for k, g in s.groupby("trade_date")}
    ovs = [len(ss[sd[i]] & ss[sd[i + 1]]) / max(len(ss[sd[i]]), 1)
           for i in range(len(sd) - 1)]
    print(f"  {tid}: 全分點次日重疊 {np.mean(ov):.1%}　純當沖子群次日重疊 {np.mean(ovs):.1%}")

print("\n=== 3. 9B2Y 純當沖子群的標的集中度（程式應聚焦少數標的）===")
d = prep("9B2Y")
s = d[d.rt > 0.95]
vc = s.stock_id.value_counts()
print(f"  {len(s):,} 個 stock-day / {s.stock_id.nunique()} 檔")
print(f"  Top10 檔佔比 {vc.head(10).sum()/len(s):.1%}　Top50 {vc.head(50).sum()/len(s):.1%}")
print(f"  只出現 1 次的標的 {(vc == 1).sum()} 檔 = {(vc == 1).sum()/len(vc):.1%} 的標的")
print("  Top12:", ", ".join(f"{k}×{v}" for k, v in vc.head(12).items()))

print("\n=== 4. 9B2Y 每日『同時做多少檔』的分布 vs 泊松（多客戶=泊松；程式=固定）===")
per = d[d.rt > 0.95].groupby("trade_date").size()
print(f"  median {per.median():.0f} mean {per.mean():.1f} var {per.var():.1f} "
      f"→ var/mean = {per.var()/per.mean():.2f}（泊松=1.0；程式應 <<1）")
perall = d.groupby("trade_date").size()
print(f"  全分點 mean {perall.mean():.1f} var/mean = {perall.var()/perall.mean():.2f}")

print("\n=== 5. 買賣完全對稱（程式對沖）比例 ===")
for tid in ["9B2Y", "9661", "8888", "9268", "9800"]:
    x = prep(tid)
    if x is None:
        continue
    both = x[(x.buy_vol > 0) & (x.sell_vol > 0)]
    print(f"  {tid}: buy==sell 精確相等 {(both.buy_vol == both.sell_vol).mean():.1%} "
          f"(n={len(both):,})　rt>0.95 佔全部 {(x.rt > 0.95).mean():.1%}")
