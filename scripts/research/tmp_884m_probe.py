#!/usr/bin/env python3
"""884M 玉山數位：分價揭露可用性單日探測（坑 #2）。"""
from __future__ import annotations

import sys

import pandas as pd

from finmind_client import fetch_taiwan_stock_trading_daily_report


def main() -> int:
    days = sys.argv[1:] or ["2026-08-11", "2026-06-10", "2025-03-12"]
    for day in days:
        r = fetch_taiwan_stock_trading_daily_report(
            trade_date=day, securities_trader_id="884M")
        if not r:
            print(f"{day}: 無資料")
            continue
        d = pd.DataFrame(r)
        p = pd.to_numeric(d.price, errors="coerce")
        b = pd.to_numeric(d.buy, errors="coerce")
        s = pd.to_numeric(d.sell, errors="coerce")
        print(f"{day}: 列數 {len(d):,} · 標的 {d.stock_id.nunique()} · "
              f"price>0 比例 {(p > 0).mean():.3f} · "
              f"買量合計 {b.sum():,.0f} 股 · 賣量合計 {s.sum():,.0f} 股")
        print(d.head(5).to_string())
        print("欄位:", d.columns.tolist())
    return 0


if __name__ == "__main__":
    sys.exit(main())
