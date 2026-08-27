#!/usr/bin/env python3
"""884M：子群是否有「持續性」—— 程式的手法會跨期複製，散戶聚合不會。

1. 參與率配對比較（消除容量約束後，884M vs 9661 vs 8888）
2. 逐股毛邊際的樣本外持續性（2025 → 2026）
3. rt>0.95 子群的月度毛邊際 t 檢定
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sps

DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"
BINS = [0, 0.0005, 0.001, 0.002, 0.004, 0.008, 0.02, 1]


def load(tid: str) -> pd.DataFrame:
    m = pd.read_pickle(DIR / f"branch_{tid}_joined.pkl")
    assert not m.duplicated(["stock_id", "trade_date"]).any()
    return m[(m.dt_vol > 0) & m.spread.notna() & m.part.notna()].copy()


def main() -> int:
    print("【1】參與率配對比較（同一參與率帶內的毛邊際%／勝率／賣位）")
    hdr = f"{'參與率帶':<16}" + "".join(f"{t:>26}" for t in ("884M", "9661", "8888"))
    print(hdr)
    tabs = {t: load(t) for t in ("884M", "9661", "8888")}
    for t, d in tabs.items():
        d["pb"] = pd.cut(d.part, BINS)
    for b in pd.cut(pd.Series([0.0]), BINS).cat.categories:
        line = f"{str(b):<16}"
        for t in ("884M", "9661", "8888"):
            d = tabs[t]
            s = d[d.pb == b]
            if len(s) < 300:
                line += f"{'—':>26}"
                continue
            gp = s.dt_pnl.sum() / s.dt_noti.sum() * 100
            sp = s.sell_pos[s.sell_pos.between(-.1, 1.1)].median()
            line += f"{gp:>+9.4f}%{(s.spread>0).mean()*100:>7.1f}%{sp:>9.3f}"
        print(line)

    print("\n【2】逐股毛邊際樣本外持續性（rt>0.95 子群，2025 → 2026）")
    for t in ("884M", "9661", "8888"):
        d = tabs[t]
        d = d[d.rt > 0.95]
        a = d[d.trade_date < "2026-01-01"]
        b = d[d.trade_date >= "2026-01-01"]
        if a.empty or b.empty:
            print(f"  {t}: 期間不足")
            continue
        ga = a.groupby("stock_id").apply(
            lambda x: pd.Series({"g": x.dt_pnl.sum()/x.dt_noti.sum()*100, "n": len(x)}),
            include_groups=False)
        gb = b.groupby("stock_id").apply(
            lambda x: pd.Series({"g": x.dt_pnl.sum()/x.dt_noti.sum()*100, "n": len(x)}),
            include_groups=False)
        j = ga.join(gb, lsuffix="_a", rsuffix="_b", how="inner")
        j = j[(j.n_a >= 10) & (j.n_b >= 10)]
        if len(j) < 20:
            print(f"  {t}: 可比股票不足 ({len(j)})")
            continue
        r, p = sps.spearmanr(j.g_a, j.g_b)
        print(f"  {t}: n={len(j)} 檔　rho(2025→2026)={r:+.3f} p={p:.3f}　"
              f"2025 中位 {j.g_a.median():+.4f}%　2026 中位 {j.g_b.median():+.4f}%")

    print("\n【3】rt>0.95 子群月度毛邊際 t 檢定（H0: 毛邊際=0）")
    for t in ("884M", "9661", "8888"):
        d = tabs[t]
        d = d[d.rt > 0.95].copy()
        d["ym"] = d.trade_date.str[:7]
        g = d.groupby("ym").apply(
            lambda x: x.dt_pnl.sum()/x.dt_noti.sum()*100, include_groups=False)
        tt = g.mean() / (g.std() / np.sqrt(len(g)))
        print(f"  {t}: 月數 {len(g)}　平均 {g.mean():+.4f}%　sd {g.std():.4f}　"
              f"t={tt:+.2f}　正月份 {(g>0).mean():.0%}")

    print("\n【4】rt>0.95 子群成本後（1.8折當沖 0.201%）的損益（億）與日均檔數穩定度")
    for t in ("884M", "9661", "8888"):
        d = tabs[t][tabs[t].rt > 0.95]
        n = d.dt_noti.sum()
        pdd = d.groupby("trade_date").size()
        print(f"  {t}: 名目 {n/1e8:,.0f} 億　毛 {d.dt_pnl.sum()/1e8:+.2f} 億　"
              f"淨 {(d.dt_pnl.sum()-n*0.00201)/1e8:+.2f} 億　"
              f"日均 {pdd.median():.0f} 檔 CV {pdd.std()/pdd.mean():.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
