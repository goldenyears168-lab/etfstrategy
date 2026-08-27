#!/usr/bin/env python3
"""9B2Y DB 層側寫：重複列檢查、日筆數/檔數、當沖度、集中度。"""
from __future__ import annotations

import pandas as pd

from stock_db import connect_ro

TID = "9B2Y"
c = connect_ro()
cols = [r[1] for r in c.execute("PRAGMA table_info(stock_broker_branch_daily)")]
print("欄位:", cols)

d = pd.read_sql_query(
    "SELECT * FROM stock_broker_branch_daily WHERE securities_trader_id=?",
    c, params=(TID,))
print(f"\n總列數 {len(d):,}")
dup = d.duplicated(["stock_id", "trade_date"], keep=False)
print(f"(stock_id,trade_date) 重複列 {dup.sum():,}  ← 坑#3")
if dup.any():
    print(d[dup].sort_values(["trade_date", "stock_id"]).head(8).to_string())

d = d.sort_values("securities_trader").drop_duplicates(["stock_id", "trade_date"])
d["y"] = d.trade_date.str[:7]
d["buy"] = pd.to_numeric(d.buy, errors="coerce")
d["sell"] = pd.to_numeric(d.sell, errors="coerce")
d["dt"] = d[["buy", "sell"]].min(axis=1)
d["mx"] = d[["buy", "sell"]].max(axis=1)
d["rt"] = d.dt / d.mx.replace(0, pd.NA)
g = d.groupby("y").agg(days=("trade_date", "nunique"), rows=("stock_id", "size"),
                       stocks=("stock_id", "nunique"), rt=("rt", "median"))
g["per_day"] = g.rows / g.days
print("\n月度:")
print(g.to_string())
