#!/usr/bin/env python3
"""9B2Y：更多切法 + 「日內漂移」對照檢定（散戶聚合 vs 程式的分水嶺）。

核心疑問：全分點毛邊際 +0.2313% 是真本事，還是「先買後賣 × 當日上漲」的 beta？
若把 stock-day 依 close/open 分層後，**下跌日仍為正**才是真的日內擇時。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sps

DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"
TID = "9B2Y"
m = pd.read_pickle(DIR / f"branch_{TID}_joined.pkl")
m = m[(m.dt_vol > 0)].dropna(subset=["buy_vwap", "sell_vwap", "spread"]).copy()
m["ret_oc"] = m.close / m.open - 1
m["ret_hl"] = (m.high - m.low) / m.low


def wm(d: pd.DataFrame) -> float:
    return d.dt_pnl.sum() / d.dt_noti.sum() * 100 if d.dt_noti.sum() else np.nan


print(f"樣本 {len(m):,} stock-day\n")
print("=== A. 依當日 close/open 分層（漂移對照）===")
m["q"] = pd.qcut(m.ret_oc, 5, labels=["最跌", "跌", "平", "漲", "最漲"])
t = m.groupby("q", observed=True).apply(lambda d: pd.Series({
    "n": len(d), "ret_oc%": d.ret_oc.median() * 100, "毛邊際%": wm(d),
    "勝率%": (d.spread > 0).mean() * 100, "買位": d.buy_pos.median(),
    "賣位": d.sell_pos.median(), "名目億": d.dt_noti.sum() / 1e8}), include_groups=False)
print(t.to_string())

print("\n=== A2. 純當沖子群 rt>0.95 同樣分層 ===")
s = m[m.rt > 0.95]
t2 = s.groupby("q", observed=True).apply(lambda d: pd.Series({
    "n": len(d), "ret_oc%": d.ret_oc.median() * 100, "毛邊際%": wm(d),
    "勝率%": (d.spread > 0).mean() * 100, "買位": d.buy_pos.median(),
    "賣位": d.sell_pos.median()}), include_groups=False)
print(t2.to_string())

print("\n=== A3. 回歸：spread ~ ret_oc（notional 加權）===")
o = m.dropna(subset=["ret_oc"])
b, a, r, p, se = sps.linregress(o.ret_oc * 100, o.spread)
print(f"  spread = {a:+.4f} + {b:.3f} × ret_oc   R²={r**2:.3f} p={p:.3g} n={len(o):,}")
print(f"  → 截距（漂移中性後的邊際）= {a:+.4f}% ；原始平均 spread = {o.spread.mean():+.4f}%")
resid = o.spread - (a + b * o.ret_oc * 100)
print(f"  殘差平均 {resid.mean():+.5f}%（應 ≈0）")

print("\n=== B. 依「該股在窗內出現次數」切（核心股 vs 一次性）===")
freq = m.groupby("stock_id").size()
m["nf"] = m.stock_id.map(freq)
for lo, hi, lab in [(1, 2, "只做 1 天"), (2, 5, "2-4 天"), (5, 20, "5-19 天"),
                    (20, 60, "20-59 天"), (60, 10**9, "≥60 天（核心股）")]:
    d = m[(m.nf >= lo) & (m.nf < hi)]
    if d.empty:
        continue
    print(f"  {lab:<16} n={len(d):>6,} 檔={d.stock_id.nunique():>4} "
          f"毛邊際={wm(d):+.4f}% 勝率={(d.spread>0).mean()*100:.1f}% "
          f"名目={d.dt_noti.sum()/1e8:.1f}億 買位={d.buy_pos.median():.3f} "
          f"賣位={d.sell_pos.median():.3f} 當沖度={d.rt.median():.2f}")

print("\n=== C. 依價位檔數 n_lvl 切（灑單廣度）===")
for lo, hi, lab in [(1, 2, "1 個價位"), (2, 4, "2-3"), (4, 8, "4-7"),
                    (8, 16, "8-15"), (16, 10**9, "≥16 個價位")]:
    d = m[(m.n_lvl >= lo) & (m.n_lvl < hi)]
    if d.empty:
        continue
    print(f"  {lab:<14} n={len(d):>6,} 毛邊際={wm(d):+.4f}% 勝率={(d.spread>0).mean()*100:.1f}% "
          f"名目={d.dt_noti.sum()/1e8:.1f}億 當沖度={d.rt.median():.2f} "
          f"每價位張={d.lvl_lot.median():.1f} 買位={d.buy_pos.median():.3f} "
          f"賣位={d.sell_pos.median():.3f}")

print("\n=== D. 依股價切（tick size 效應：低價股一檔 = 大 %）===")
for lo, hi in [(0, 20), (20, 50), (50, 100), (100, 500), (500, 10**9)]:
    d = m[(m.close >= lo) & (m.close < hi)]
    if d.empty:
        continue
    print(f"  {lo}~{hi} 元　n={len(d):>6,} 毛邊際={wm(d):+.4f}% 勝率={(d.spread>0).mean()*100:.1f}% "
          f"名目={d.dt_noti.sum()/1e8:.1f}億 每價位張={d.lvl_lot.median():.1f}")

print("\n=== E. 三個判準的量化 ===")
sub = m[m.rt > 0.95]
per = sub.groupby("trade_date").size()
print(f"  純當沖子群 日筆數 median={per.median():.0f} CV={per.std()/per.mean():.3f} "
      f"min={per.min()} max={per.max()}")
perall = m.groupby("trade_date").size()
print(f"  全分點     日筆數 median={perall.median():.0f} CV={perall.std()/perall.mean():.3f}")
print(f"  單筆規格化：每價位張數 mode 附近 —— "
      f"{(sub.lvl_lot.between(0.95,1.05)).mean():.1%} 落在 1.0 張")
print(f"  子群毛邊際 {wm(sub):+.4f}% vs 全分點 {wm(m):+.4f}% → "
      f"{'子群更好' if wm(sub) > wm(m) else '子群更差（與 9661 相反）'}")

print("\n=== F. 每日毛邊際的時序穩定度（程式應該窄且持續）===")
dd = m.groupby("trade_date").apply(lambda d: pd.Series({
    "gross": wm(d), "n": len(d), "noti": d.dt_noti.sum()}), include_groups=False)
print(dd.gross.describe(percentiles=[.1, .25, .5, .75, .9]).to_string())
print(f"  正日比例 {(dd.gross > 0).mean():.1%}　t(日均, H0=0) = "
      f"{sps.ttest_1samp(dd.gross.dropna(), 0).statistic:+.2f} "
      f"p={sps.ttest_1samp(dd.gross.dropna(), 0).pvalue:.3g}")
ds = m[m.rt > 0.95].groupby("trade_date").apply(lambda d: pd.Series({"gross": wm(d)}),
                                                include_groups=False)
print(f"  純當沖子群 正日比例 {(ds.gross > 0).mean():.1%}　t = "
      f"{sps.ttest_1samp(ds.gross.dropna(), 0).statistic:+.2f} "
      f"p={sps.ttest_1samp(ds.gross.dropna(), 0).pvalue:.3g}")

print("\n=== G. 市場整體同期對照（同 stock-day 母體的漂移）===")
print(f"  母體 ret_oc 中位 {m.ret_oc.median()*100:+.3f}% 平均 {m.ret_oc.mean()*100:+.3f}%")
print(f"  日均 ret_oc 為正的日子 {(m.groupby('trade_date').ret_oc.mean() > 0).mean():.1%}")
