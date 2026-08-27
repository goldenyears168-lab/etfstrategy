#!/usr/bin/env python3
"""884M：更多行為子群切法 —— 找「規模穩定 + 毛邊際明顯不同於全分點」的子群。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tmp_884m_dissect import show, stats  # noqa: E402

DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"


def main() -> int:
    tid = sys.argv[1] if len(sys.argv) > 1 else "884M"
    m = pd.read_pickle(DIR / f"branch_{tid}_joined.pkl")
    assert not m.duplicated(["stock_id", "trade_date"]).any()

    print("【(d) 參與率分層（分價股 /1000 ÷ 當日張數）】")
    d = m[m.dt_vol > 0].dropna(subset=["part"]).copy()
    q = pd.qcut(d.part.rank(method="first"), 5, labels=False)
    show([stats(d[q == k], f"參與率 Q{k+1} (中位 {d[q==k].part.median()*100:.2f}%)")
          for k in range(5)])

    print("\n【(e) 價位檔數分層 —— 分散執行 = 演算法足跡】")
    show([stats(m[m.n_lvl == 1], "n_lvl = 1"),
          stats(m[m.n_lvl.between(2, 4)], "n_lvl 2~4"),
          stats(m[m.n_lvl.between(5, 9)], "n_lvl 5~9"),
          stats(m[m.n_lvl.between(10, 19)], "n_lvl 10~19"),
          stats(m[m.n_lvl >= 20], "n_lvl >=20"),
          stats(m[(m.n_lvl >= 10) & (m.rt > 0.95)], "n_lvl>=10 & rt>0.95")])

    print("\n【(f) 依「該股出現天數」分層 —— 固定宇宙 = 程式】")
    pres = m.groupby("stock_id").trade_date.nunique()
    m2 = m.assign(pres=m.stock_id.map(pres))
    show([stats(m2[m2.pres <= 20], "出現 ≤20 日的股票"),
          stats(m2[m2.pres.between(21, 100)], "出現 21~100 日"),
          stats(m2[m2.pres.between(101, 250)], "出現 101~250 日"),
          stats(m2[m2.pres > 250], "出現 >250 日（常客）"),
          stats(m2[(m2.pres > 250) & (m2.rt > 0.95)], "常客 & rt>0.95")])

    print("\n【(g) 大小型股（當日成交張數）】")
    d = m[m.dt_vol > 0].copy()
    q = pd.qcut(d.vol.rank(method="first"), 4, labels=False)
    show([stats(d[q == k], f"成交量 Q{k+1} (中位 {d[q==k].vol.median():,.0f} 張)")
          for k in range(4)])

    print("\n【(h) 規格化檢查：買量是否整數張／固定張數】")
    for lab, sub in [("全分點", m), ("rt>0.95", m[m.rt > 0.95]),
                     ("rt>0.95 & n_lvl=1", m[(m.rt > 0.95) & (m.n_lvl == 1)])]:
        b = sub.buy_vol.dropna()
        b = b[b > 0]
        if b.empty:
            continue
        vc = (b / 1000.0).round(3).value_counts(normalize=True)
        print(f"  {lab}: n={len(b):,}　整張比例 {(b % 1000 == 0).mean():.1%}　"
              f"最常見張數 {list(vc.head(5).round(3).items())}")

    print("\n【(i) 逐年／逐半年 全分點毛邊際 —— 手法有沒有漂移】")
    m3 = m[m.dt_vol > 0].copy()
    m3["h"] = m3.trade_date.str[:4] + "H" + ((m3.trade_date.str[5:7].astype(int) > 6) + 1).astype(str)
    g = m3.groupby("h").apply(
        lambda x: pd.Series({"n": len(x), "noti_yi": x.dt_noti.sum() / 1e8,
                             "gross_pct": x.dt_pnl.sum() / x.dt_noti.sum() * 100,
                             "win": (x.spread > 0).mean() * 100}), include_groups=False)
    print(g.to_string(float_format=lambda x: f"{x:,.3f}"))

    print("\n【(j) 最佳候選子群：交叉 rt>0.95 × 參與率高 × 常客】")
    cand = m2[(m2.rt > 0.95) & (m2.part > 0.005) & (m2.pres > 200)]
    show([stats(cand, "rt>0.95 & part>0.5% & 常客")])
    # 同一組條件放在 9661 上會長什麼樣（若有檔案）
    return 0


if __name__ == "__main__":
    sys.exit(main())
