#!/usr/bin/env python3
"""981M 子群切法探索。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect_ro

D = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"
TID = "981M"
HDR = ("子群".ljust(24) + "n".rjust(8) + "檔".rjust(6) + "日均".rjust(7) + "CV".rjust(6)
       + "名目億".rjust(9) + "毛%".rjust(10) + "勝率".rjust(8) + "參與%".rjust(8)
       + "rt".rjust(6) + "買位".rjust(8) + "賣位".rjust(8) + "中位張".rjust(9))


def st(d: pd.DataFrame, lab: str) -> str:
    if len(d) < 50:
        return f"{lab:<24}{len(d):>8}  （樣本不足）"
    pdy = d.groupby("trade_date").size()
    g, n = d.dt_pnl.sum(), d.dt_noti.sum()
    v = d.dropna(subset=["buy_pos", "sell_pos"])
    v = v[v.buy_pos.between(-.1, 1.1) & v.sell_pos.between(-.1, 1.1)]
    return (f"{lab:<24}{len(d):>8}{d.stock_id.nunique():>6}{pdy.median():>7.0f}"
            f"{pdy.std()/pdy.mean():>6.2f}{n/1e8:>9.1f}{g/n*100:>+10.4f}"
            f"{(d.spread>0).mean()*100:>7.1f}%{d.part.median()*100:>8.2f}{d.rt.median():>6.2f}"
            f"{v.buy_pos.median():>8.3f}{v.sell_pos.median():>8.3f}{d.dt_lot.median():>9.2f}")


def main() -> int:
    m = pd.read_pickle(D / f"branch_{TID}_joined.pkl")
    print(HDR)
    print("-" * 130)
    print(st(m, "全分點"))
    print(st(m[m.dt_lot < 1], "零股(當沖<1張)"))
    print(st(m[m.dt_lot >= 1], "整股(當沖>=1張)"))
    for lo, hi, lab in [(0, .001, "參與<0.1%"), (.001, .005, "參與0.1~0.5%"),
                        (.005, .02, "參與0.5~2%"), (.02, 1e9, "參與>2%")]:
        print(st(m[m.part.between(lo, hi)], lab))
    for lo, hi, lab in [(1, 3, "價位檔數1-3"), (4, 10, "價位檔數4-10"),
                        (11, 30, "價位檔數11-30"), (31, 10**6, "價位檔數>30")]:
        print(st(m[m.n_lvl.between(lo, hi)], lab))
    for lo, hi, lab in [(0, 20, "股價<20"), (20, 50, "股價20-50"),
                        (50, 150, "股價50-150"), (150, 1e9, "股價>150")]:
        print(st(m[m.close.between(lo, hi)], lab))
    # 交易日流動性
    for lo, hi, lab in [(0, 1e3, "市場量<1000張"), (1e3, 1e4, "1000-1萬張"),
                        (1e4, 1e5, "1-10萬張"), (1e5, 1e12, ">10萬張")]:
        print(st(m[m.vol.between(lo, hi)], lab))

    # DB top-15 揭露子群（981M 大到擠進該股當日前十五大買/賣超）
    c = connect_ro()
    tp = pd.read_sql_query(
        "SELECT DISTINCT stock_id, trade_date FROM stock_broker_branch_daily "
        "WHERE securities_trader_id=? AND trade_date>='2025-01-01'", c, params=(TID,))
    tp["top15"] = 1
    assert not tp.duplicated(["stock_id", "trade_date"]).any()
    n0 = len(m)
    mm = m.merge(tp, on=["stock_id", "trade_date"], how="left", validate="one_to_one")
    assert len(mm) == n0, "top15 merge 產生重複"
    mm["top15"] = mm.top15.fillna(0)
    print()
    print(st(mm[mm.top15 == 1], "DB前15大揭露日"))
    print(st(mm[mm.top15 == 0], "未進前15大"))

    # 核心宇宙：出席天數最多的股票
    att = m.groupby("stock_id").trade_date.nunique()
    for lo, hi, lab in [(300, 10**6, "出席>=300日核心"), (150, 300, "出席150-300日"),
                        (30, 150, "出席30-150日"), (1, 30, "出席<30日")]:
        sids = att[att.between(lo, hi if hi < 10**6 else 10**9)].index
        print(st(m[m.stock_id.isin(sids)], lab))
    print(f"\n出席天數分布：{att.describe(percentiles=[.5,.75,.9,.95,.99]).round(1).to_dict()}")
    print(f"出席>=300 日的股票數：{(att>=300).sum()}　>=380：{(att>=380).sum()}")

    # 純當沖 × 核心宇宙
    core = att[att >= 300].index
    p = m[m.rt > 0.95]
    print()
    print(st(p[p.stock_id.isin(core)], "rt>.95 & 核心宇宙"))
    print(st(p[~p.stock_id.isin(core)], "rt>.95 & 非核心"))
    print(st(p[p.dt_lot >= 1], "rt>.95 & 整股"))
    print(st(p[p.dt_lot < 1], "rt>.95 & 零股"))
    print(st(p[(p.dt_lot >= 1) & (p.part > 0.002)], "rt>.95 & 整股 & 參與>0.2%"))
    print(st(m[(m.dt_lot >= 1) & (m.part > 0.005)], "整股 & 參與>0.5%"))

    # 單筆規格化：檢查當沖張數的眾數集中度
    print("\n[當沖張數眾數（整股）]")
    lots = m[m.dt_lot >= 1].dt_lot.round(0)
    print(lots.value_counts(normalize=True).head(12).round(4).to_string())
    print("\n[買進股數是否為 1000 倍數]", (m.buy_vol % 1000 == 0).mean().round(4))
    return 0


if __name__ == "__main__":
    sys.exit(main())
