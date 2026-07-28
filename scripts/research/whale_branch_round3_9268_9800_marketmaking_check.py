#!/usr/bin/env python3
"""WHALE-ROUND3-9268-9800-MM-CHECK · 對 9268/9800 兩個超大樣本分點做造市/綜合流量型態檢查。

比照 whale_branch_l1h7_deepdive_11neg_marketmaking_check.py 的方法論：
對每個分點最活躍的 8 檔股票，算「出現交易日數佔比」與「買賣金額平衡比例」，
外加：該分點總共觸碰過幾檔不同股票(全期間) vs 全市場交易日數，判斷是集中式選股
還是廣泛式（近乎鋪天蓋地）流量帳戶。

必須在 mini 上跑：
  ssh mac-mini-lan '... PYTHONPATH=src python3 scripts/research/whale_branch_round3_9268_9800_marketmaking_check.py'
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

from stock_db import DEFAULT_DB_PATH, connect

TRADER_IDS = ["9268", "9800"]
TOP_N_STOCKS = 8
OUT_STOCK_CSV = ROOT / "reports/research/branch-footprint-screen/whale_branch_round3_9268_9800_top_stocks.csv"
OUT_OVERVIEW_CSV = ROOT / "reports/research/branch-footprint-screen/whale_branch_round3_9268_9800_overview.csv"


def main() -> int:
    conn = connect(DEFAULT_DB_PATH)
    placeholders = ",".join("?" * len(TRADER_IDS))

    name_rows = conn.execute(
        f"""SELECT DISTINCT securities_trader_id, securities_trader FROM stock_broker_branch_daily
            WHERE source='finmind' AND securities_trader_id IN ({placeholders})""",
        TRADER_IDS,
    ).fetchall()
    names = {r[0]: r[1] for r in name_rows}
    print("names:", names)

    # total distinct trade dates in table (for denominator context)
    total_dates = conn.execute(
        "SELECT COUNT(DISTINCT trade_date) FROM stock_broker_branch_daily WHERE source='finmind'"
    ).fetchone()[0]
    print("total distinct trade_date in table:", total_dates)

    print("scanning stock_broker_branch_daily for target traders (single pass)...")
    all_detail = conn.execute(
        f"""
        SELECT securities_trader_id, stock_id, trade_date, buy, sell
        FROM stock_broker_branch_daily
        WHERE source='finmind' AND securities_trader_id IN ({placeholders})
        """,
        TRADER_IDS,
    ).fetchall()
    full_df = pd.DataFrame(all_detail, columns=["trader_id", "stock_id", "trade_date", "buy", "sell"])
    print(f"pulled {len(full_df)} rows for {full_df['trader_id'].nunique()} traders")

    overview_rows = []
    stock_rows = []

    for tid in TRADER_IDS:
        tdf = full_df[full_df["trader_id"] == tid]
        if tdf.empty:
            print(f"{tid}: no data")
            continue

        n_distinct_stocks = tdf["stock_id"].nunique()
        n_distinct_dates = tdf["trade_date"].nunique()
        # rows per date -> how many different stocks touched per active day, on average
        rows_per_date = tdf.groupby("trade_date")["stock_id"].nunique()
        overview_rows.append(
            {
                "trader_id": tid,
                "trader_name": names.get(tid, ""),
                "n_distinct_stocks_touched": n_distinct_stocks,
                "n_distinct_active_trade_dates": n_distinct_dates,
                "active_dates_pct_of_all": round(n_distinct_dates / total_dates * 100, 1) if total_dates else None,
                "avg_stocks_per_active_day": round(float(rows_per_date.mean()), 1),
                "median_stocks_per_active_day": round(float(rows_per_date.median()), 1),
                "max_stocks_per_active_day": int(rows_per_date.max()),
            }
        )

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

        # for these top stocks, how many distinct trade_dates does the *stock itself* have
        # (to compute "presence pct" = branch active days on this stock / stock's own trading days)
        for sid in stock_ids_str:
            sub = df[df["stock_id"] == sid]
            stock_own_dates = conn.execute(
                "SELECT COUNT(DISTINCT trade_date) FROM stock_daily_bars WHERE source='finmind' AND stock_id=?",
                (sid,),
            ).fetchone()[0]
            total_buy = sub["buy_amt"].sum()
            total_sell = sub["sell_amt"].sum()
            n_days = len(sub)
            n_net_buy_days = int((sub["net_amt"] > 0).sum())
            n_net_sell_days = int((sub["net_amt"] < 0).sum())
            balance_ratio = total_buy / (total_buy + total_sell) if (total_buy + total_sell) > 0 else None
            stock_rows.append(
                {
                    "trader_id": tid,
                    "trader_name": names.get(tid, ""),
                    "stock_id": sid,
                    "n_trading_days_on_stock": n_days,
                    "stock_own_trading_days": stock_own_dates,
                    "presence_pct": round(n_days / stock_own_dates * 100, 1) if stock_own_dates else None,
                    "total_buy_amt_ntd": round(total_buy, 0),
                    "total_sell_amt_ntd": round(total_sell, 0),
                    "buy_sell_balance_ratio_pct": round(balance_ratio * 100, 1) if balance_ratio is not None else None,
                    "net_buy_days_pct": round(n_net_buy_days / n_days * 100, 1) if n_days else None,
                    "net_sell_days_pct": round(n_net_sell_days / n_days * 100, 1) if n_days else None,
                }
            )
        print(f"{tid} ({names.get(tid)}) done, {len(stock_ids)} top stocks; total distinct stocks touched={n_distinct_stocks}")

    overview = pd.DataFrame(overview_rows)
    stocks = pd.DataFrame(stock_rows)
    overview.to_csv(OUT_OVERVIEW_CSV, index=False)
    stocks.to_csv(OUT_STOCK_CSV, index=False)
    print("\n=== OVERVIEW ===")
    print(overview.to_string(index=False))
    print("\n=== TOP STOCKS ===")
    print(stocks.to_string(index=False))
    print("saved:", OUT_OVERVIEW_CSV, OUT_STOCK_CSV)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
