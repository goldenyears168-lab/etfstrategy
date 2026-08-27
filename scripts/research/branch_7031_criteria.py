#!/usr/bin/env python3
"""7031 致和台北 —— 四項判準逐項檢定（2026-08-27 改版判準）。

判準（至少過三項才算程式）：
  1 單筆規格化：整張率（越低越像程式）／檔內張數 CV
  2 子群毛邊際 > 全分點（9661 指紋 = 2.07×）
  3 子群層級過離散度 var/mean << 1
  4 beta／日內漂移控制（用 0050 同期報酬控制；5110 死在這條）

⚠️ 禁止依「當日價差」分層 —— 那是循環論證（判準 4 舊版已刪）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sps

from stock_db import connect_ro

DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"
TID = sys.argv[1] if len(sys.argv) > 1 else "7031"


def wm(d: pd.DataFrame) -> float:
    return d.dt_pnl.sum() / d.dt_noti.sum() * 100 if d.dt_noti.sum() else np.nan


def overdisp(d: pd.DataFrame) -> tuple[float, float]:
    """回傳 (var/mean, 尺度不變的隱含 CV_lambda)。

    overdisp = 1 + mean * CV_lambda^2 → 原始 var/mean 隨日均檔數機械上升，
    小分點天生看起來「像程式」。cvl 才是可跨規模比較的量。
    """
    k = d.groupby("trade_date").size()
    if len(k) < 10 or k.mean() <= 0:
        return np.nan, np.nan
    od = k.var() / k.mean()
    return od, float(np.sqrt(max(od - 1, 0) / k.mean()))


def main() -> int:
    m = pd.read_pickle(DIR / f"branch_{TID}_joined.pkl")
    m = m[m.dt_vol > 0].dropna(subset=["buy_vwap", "sell_vwap", "spread"]).copy()
    assert not m.duplicated(["stock_id", "trade_date"]).any()
    m["lots_b"] = m.buy_vol / 1000.0
    m["lots_s"] = m.sell_vol / 1000.0
    m["whole"] = (np.isclose(m.lots_b % 1, 0, atol=1e-6)
                  & np.isclose(m.lots_s % 1, 0, atol=1e-6))
    m["ret_oc"] = m.close / m.open - 1
    pure = m[m.rt > 0.95]

    print(f"=== {TID}　母體 {len(m):,} stock-day · {m.trade_date.nunique()} 日 · "
          f"{m.stock_id.nunique()} 檔　純當沖(rt>.95) {len(pure):,} ===\n")

    print("【判準 1】單筆規格化（整張率越低越像程式；9661=45.5%、9225=71.2%、5110=97.1%）")
    for lab, d in (("全分點", m), ("純當沖 rt>.95", pure)):
        lv = ((d.buy_vol + d.sell_vol) / 1000.0 / d.n_lvl.replace(0, np.nan)).dropna()
        print(f"  {lab:<14} 整張率 {d.whole.mean()*100:>5.1f}%　"
              f"檔內張數 中位 {lv.median():.2f} CV {lv.std()/lv.mean():.2f}　"
              f"買量≤2張 {(d.lots_b <= 2).mean()*100:.1f}%　"
              f"眾數張 {d.lots_b.round().mode().iloc[0]:.0f}")

    print("\n【判準 2】子群毛邊際 vs 全分點（9661 = 2.07×）")
    base = wm(m)
    for lab, d in (("全分點", m), ("rt>0.90", m[m.rt > 0.90]), ("rt>0.95", pure),
                   ("rt>0.99", m[m.rt > 0.99]), ("rt<0.30", m[m.rt < 0.30]),
                   ("rt<0.10", m[m.rt < 0.10]),
                   ("名目>500萬", m[m.dt_noti > 5e6]),
                   ("rt>.95 & 名目>500萬", pure[pure.dt_noti > 5e6]),
                   ("整張", m[m.whole]), ("含零股", m[~m.whole]),
                   ("參與率>1%", m[m.part > 0.01])):
        if len(d) < 30:
            print(f"  {lab:<20} n={len(d):>6}  樣本不足")
            continue
        g = wm(d)
        sp = d.spread.dropna()
        t = sp.mean() / (sp.std(ddof=1) / np.sqrt(len(sp)))
        print(f"  {lab:<20} n={len(d):>6,} 毛邊際 {g:>+8.4f}%　×全分點 {g/base:>+6.2f}"
              f"　t={t:>+5.1f} 勝率 {(d.spread>0).mean()*100:>4.1f}% "
              f"名目 {d.dt_noti.sum()/1e8:>6.1f}億")

    print("\n【判準 3】過離散度（原始 var/mean 與尺度不變的隱含 CV_λ）")
    for lab, d in (("全分點", m), ("rt>0.90", m[m.rt > 0.90]), ("rt>0.95", pure),
                   ("rt>0.99", m[m.rt > 0.99]), ("整張", m[m.whole])):
        od, cvl = overdisp(d)
        k = d.groupby("trade_date").size()
        print(f"  {lab:<12} 日均 {k.mean():>6.1f} var/mean {od:>6.2f}　CV_λ {cvl:>6.3f}"
              f"　出席日 {len(k)}/{m.trade_date.nunique()}")

    print("\n【判準 4】beta／日內漂移控制")
    c = connect_ro()
    mkt = pd.read_sql_query(
        "SELECT trade_date, open, close FROM stock_daily_bars "
        "WHERE stock_id='0050' AND trade_date>='2025-01-01' AND close>0", c)
    mkt = mkt.drop_duplicates("trade_date")
    mkt["mkt_oc"] = mkt.close / mkt.open - 1
    m2 = m.merge(mkt[["trade_date", "mkt_oc"]], on="trade_date", how="inner")
    assert not m2.duplicated(["stock_id", "trade_date"]).any()
    print(f"  併 0050 後 {len(m2):,} stock-day（{m2.trade_date.nunique()} 日）")
    m2["mq"] = pd.qcut(m2.mkt_oc, 5, labels=["最跌", "跌", "平", "漲", "最漲"])
    print(f"  {'0050 五分位':<10}{'n':>7}{'0050 oc%':>10}{'毛邊際%':>10}{'勝率%':>8}"
          f"{'買位':>7}{'賣位':>7}")
    for q, d in m2.groupby("mq", observed=True):
        v = d[d.buy_pos.between(-.1, 1.1) & d.sell_pos.between(-.1, 1.1)]
        print(f"  {str(q):<12}{len(d):>7,}{d.mkt_oc.median()*100:>+9.3f}{wm(d):>+10.4f}"
              f"{(d.spread>0).mean()*100:>8.1f}{v.buy_pos.median():>7.3f}"
              f"{v.sell_pos.median():>7.3f}")
    o = m2.dropna(subset=["mkt_oc", "spread"])
    b, a, r, p, se = sps.linregress(o.mkt_oc * 100, o.spread)
    print(f"  回歸 spread ~ 0050_oc：截距 {a:+.4f}%（±{sps.linregress(o.mkt_oc*100,o.spread).stderr*0+0:.0f}）"
          f" 斜率 {b:+.3f} R²={r**2:.4f} p={p:.3g}")
    o2 = m2.dropna(subset=["ret_oc", "spread"])
    b2, a2, r2, p2, _ = sps.linregress(o2.ret_oc * 100, o2.spread)
    print(f"  回歸 spread ~ 個股 oc  ：截距 {a2:+.4f}% 斜率 {b2:+.3f} "
          f"R²={r2**2:.4f} p={p2:.3g}")
    print("  ↑ 截距 = 漂移中性後的邊際；若原始毛邊際幾乎全被 oc 解釋 → 沒有真本事")
    # 純當沖子群同樣做
    p95 = m2[m2.rt > 0.95].dropna(subset=["mkt_oc", "spread"])
    if len(p95) > 50:
        b3, a3, r3, p3, _ = sps.linregress(p95.mkt_oc * 100, p95.spread)
        print(f"  純當沖子群 spread ~ 0050_oc：截距 {a3:+.4f}% 斜率 {b3:+.3f} p={p3:.3g}"
              f"　（原始毛邊際 {wm(p95):+.4f}%）")

    print("\n【穩健性】兩個獨立時間半段")
    dates = np.sort(m.trade_date.unique())
    mid = dates[len(dates) // 2]
    for lab, d in (("全分點", m), ("rt>0.95", pure)):
        h1, h2 = d[d.trade_date < mid], d[d.trade_date >= mid]
        print(f"  {lab:<10} 前半 {wm(h1):+.4f}% (n={len(h1):,})　"
              f"後半 {wm(h2):+.4f}% (n={len(h2):,})")

    print("\n【成本】當沖 1.8 折合計 0.201%／6 折 0.321%")
    for lab, d in (("全分點", m), ("rt>0.95", pure)):
        g, n = d.dt_pnl.sum(), d.dt_noti.sum()
        print(f"  {lab:<10} 名目 {n/1e8:.1f}億　毛 {g/1e4:+,.0f} 萬　"
              f"淨@1.8折 {(g-n*0.00201)/1e4:+,.0f} 萬　淨@6折 {(g-n*0.00321)/1e4:+,.0f} 萬")
    return 0


if __name__ == "__main__":
    sys.exit(main())
