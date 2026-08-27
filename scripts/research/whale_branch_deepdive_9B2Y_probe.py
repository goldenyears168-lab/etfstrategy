#!/usr/bin/env python3
"""9B2Y：找 API 覆蓋邊界 + 分點名稱隨時間變化。"""
from __future__ import annotations

import time

import pandas as pd

from finmind_client import fetch_taiwan_stock_trading_daily_report
from stock_db import connect_ro

TID = "9B2Y"
c = connect_ro()

print("=== DB 名稱隨時間 ===")
nm = pd.DataFrame([tuple(r) for r in c.execute(
    "SELECT trade_date, securities_trader, COUNT(*) FROM stock_broker_branch_daily "
    "WHERE securities_trader_id=? GROUP BY trade_date, securities_trader", (TID,))],
    columns=["d", "name", "n"])
g = nm.groupby("name").agg(first=("d", "min"), last=("d", "max"),
                           days=("d", "nunique"), rows=("n", "sum"))
print(g.to_string())

days = [r[0] for r in c.execute(
    "SELECT DISTINCT trade_date FROM stock_daily_bars "
    "WHERE trade_date BETWEEN '2025-11-01' AND '2026-05-01' ORDER BY trade_date")]

lo, hi = 0, len(days) - 1
print(f"\n=== 二分搜尋 API 覆蓋起點（{days[0]} ~ {days[-1]}, {len(days)} 日）===")


def has(day: str) -> bool:
    for _ in range(3):
        try:
            r = fetch_taiwan_stock_trading_daily_report(
                trade_date=day, securities_trader_id=TID)
            return bool(r)
        except Exception as exc:  # noqa: BLE001
            print(f"   {day} ERR {str(exc)[:70]}")
            time.sleep(3)
    return False


while lo < hi:
    mid = (lo + hi) // 2
    ok = has(days[mid])
    print(f"  {days[mid]}: {'有' if ok else '空'}")
    if ok:
        hi = mid
    else:
        lo = mid + 1
    time.sleep(0.5)
print(f"→ API 最早有資料日 ≈ {days[lo]}")
