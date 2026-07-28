#!/usr/bin/env python3
"""L1H7-DEEPDIVE-11NEG-MM · 對 11 個 BH-FDR 顯著負向分點做簡化版造市/建倉型態檢查。

對每個分點，取其交易最頻繁的 3~5 檔股票（依 stock_broker_branch_daily 出現天數排序），
彙總該分點在該股票上的總買超股數/總賣超股數/總買進金額/總賣出金額，計算：
  - buy_sell_balance_ratio = total_buy_amt / (total_buy_amt + total_sell_amt)
    接近 50% → 買賣雙邊接近相等，疑似造市/避險/自營避險型態
    接近 100% → 幾乎單向買進，疑似真正的（方向性錯誤的）選股型態
  - net_direction_days_pct = 淨買超天數 / 總交易天數（另一個單邊 vs 雙邊指標）

用法（必須在 mini 上跑，因為要查 stock_broker_branch_daily 全期間資料）：
  PYTHONPATH=src python3 scripts/research/whale_branch_l1h7_deepdive_11neg_marketmaking_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

from stock_db import DEFAULT_DB_PATH, connect

TRADER_IDS = ["980T", "8440", "1650", "1440", "8890", "9600", "1590", "7000", "8900", "9100", "980C"]
TOP_N_STOCKS = 5
OUT_CSV = ROOT / "reports/research/branch-footprint-screen/whale_branch_l1h7_deepdive_11neg_marketmaking_check.csv"


def main() -> int:
    conn = connect(DEFAULT_DB_PATH)
    placeholders = ",".join("?" * len(TRADER_IDS))

    # names
    name_rows = conn.execute(
        f"""SELECT DISTINCT securities_trader_id, securities_trader FROM stock_broker_branch_daily
            WHERE source='finmind' AND securities_trader_id IN ({placeholders})""",
        TRADER_IDS,
    ).fetchall()
    names = {r[0]: r[1] for r in name_rows}
    print("names:", names)

    all_rows = []
    placeholders_all = ",".join("?" * len(TRADER_IDS))

    # ONE combined full-table scan (avoid 11x separate scans of the 220M-row table,
    # which has no index usable for a securities_trader_id-only filter since PK leads
    # with trade_date). This pulls all rows for the 11 target traders in a single pass.
    print("scanning stock_broker_branch_daily for all 11 traders in one pass...")
    all_detail = conn.execute(
        f"""
        SELECT securities_trader_id, stock_id, trade_date, buy, sell
        FROM stock_broker_branch_daily
        WHERE source='finmind' AND securities_trader_id IN ({placeholders_all})
        """,
        TRADER_IDS,
    ).fetchall()
    full_df = pd.DataFrame(all_detail, columns=["trader_id", "stock_id", "trade_date", "buy", "sell"])
    print(f"pulled {len(full_df)} rows for {full_df['trader_id'].nunique()} traders")

    for tid in TRADER_IDS:
        tdf = full_df[full_df["trader_id"] == tid]
        if tdf.empty:
            print(f"{tid}: no data")
            continue
        day_counts = tdf.groupby("stock_id")["trade_date"].nunique().sort_values(ascending=False)
        stock_ids = day_counts.head(TOP_N_STOCKS).index.tolist()

        stock_ids_str = [str(s) for s in stock_ids]
        sph = ",".join("?" * len(stock_ids_str))
        close_rows = conn.execute(
            f"""SELECT stock_id, trade_date, close FROM stock_daily_bars
                WHERE source='finmind' AND stock_id IN ({sph})""",
            stock_ids_str,
        ).fetchall()
        close_df = pd.DataFrame(close_rows, columns=["stock_id", "trade_date", "close"])
        close_df["stock_id"] = close_df["stock_id"].astype(str)

        df = tdf[tdf["stock_id"].astype(str).isin(stock_ids_str)].copy()
        df["stock_id"] = df["stock_id"].astype(str)
        df = df.merge(close_df, on=["stock_id", "trade_date"], how="left")
        df["buy_amt"] = df["buy"] * df["close"]
        df["sell_amt"] = df["sell"] * df["close"]
        df["net_amt"] = df["buy_amt"] - df["sell_amt"]

        for sid in stock_ids_str:
            sub = df[df["stock_id"] == sid]
            total_buy = sub["buy_amt"].sum()
            total_sell = sub["sell_amt"].sum()
            n_days = len(sub)
            n_net_buy_days = int((sub["net_amt"] > 0).sum())
            n_net_sell_days = int((sub["net_amt"] < 0).sum())
            balance_ratio = total_buy / (total_buy + total_sell) if (total_buy + total_sell) > 0 else None
            all_rows.append(
                {
                    "trader_id": tid,
                    "trader_name": names.get(tid, ""),
                    "stock_id": sid,
                    "n_trading_days": n_days,
                    "total_buy_amt_ntd": round(total_buy, 0),
                    "total_sell_amt_ntd": round(total_sell, 0),
                    "buy_sell_balance_ratio_pct": round(balance_ratio * 100, 1) if balance_ratio is not None else None,
                    "net_buy_days_pct": round(n_net_buy_days / n_days * 100, 1) if n_days else None,
                    "net_sell_days_pct": round(n_net_sell_days / n_days * 100, 1) if n_days else None,
                }
            )
        print(f"{tid} ({names.get(tid)}) done, {len(stock_ids)} stocks")

    out = pd.DataFrame(all_rows)
    out.to_csv(OUT_CSV, index=False)
    print(out.to_string())
    print("saved:", OUT_CSV)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
