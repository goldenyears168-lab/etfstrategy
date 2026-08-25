#!/usr/bin/env python3
"""抓「與借券體系正交」的候選因子 —— 探索用快取，先不進 DB schema。

現有 v4 的五個分項全部來自**同一個資訊源**（借券／融券複合體）＋分點家數，
彼此高度相關。要真正加分，新因子必須來自**不同的資料產生機制**。

本檔抓兩個：
  1. 集保股權分散表（FinMind TaiwanStockHoldingSharesPer）· **週頻**
     大戶／散戶持股比例。來源是集保結算所過戶紀錄，與借券完全無關。
     週頻＝天然低換手，正好對症 2026-08-25 發現的 77% 換手問題。
  2. 月營收（FinMind TaiwanStockMonthRevenue）· **月頻**
     基本面動能。與籌碼結構正交，且 PIT 乾淨（每月 10 日前公布上月）。

⚠️ 探索階段刻意存 pickle 不進 DB：還不知道有沒有用，不該先污染 schema。
"""
from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

from finmind_client import fetch_finmind
from stock_db import connect_ro

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "reports" / "research" / "chip-signal-daily-horizon"
START, END = date(2024, 6, 1), date(2026, 8, 25)


def universe() -> list[str]:
    """與每日名單同口徑：有借券資料、成交 ≥500 張、股價 ≥10、非 ETF。"""
    c = connect_ro()
    q = """SELECT DISTINCT s.stock_id FROM stock_short_interest_daily s
             JOIN stock_daily_bars p ON p.stock_id=s.stock_id AND p.trade_date=s.trade_date
            WHERE s.trade_date >= '2026-06-01' AND p.close >= 10
              AND p.volume/1000.0 >= 500"""
    return sorted({r[0] for r in c.execute(q) if not r[0].startswith("00")})


def pull(dataset: str, sids: list[str], label: str) -> pd.DataFrame:
    rows, bad = [], 0
    for i, sid in enumerate(sids):
        try:
            r = fetch_finmind(dataset, sid, START, END)
            rows.extend(r)
        except Exception:  # noqa: BLE001
            bad += 1
            time.sleep(2.0)                      # 多半是額度，退避後續抓
        if i % 50 == 0:
            print(f"  {label} {i}/{len(sids)}（失敗 {bad}）", flush=True)
    print(f"  {label} 完成：{len(rows):,} 列，失敗 {bad} 檔")
    return pd.DataFrame(rows)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    sids = universe()
    print(f"宇宙 {len(sids)} 檔　期間 {START}~{END}\n")
    for ds, name in (("TaiwanStockHoldingSharesPer", "dispersion"),
                     ("TaiwanStockMonthRevenue", "revenue")):
        p = OUT / f"factor_{name}.pkl"
        if p.exists():
            print(f"  {name} 已有快取，略過")
            continue
        pull(ds, sids, name).to_pickle(p)
        print(f"  → {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
