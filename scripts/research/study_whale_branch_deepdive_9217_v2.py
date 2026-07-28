#!/usr/bin/env python3
"""分點交易習慣全面分析 + 目前持倉估算（通用版，指定 --trader-id）.

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_branch_deepdive_9217_v2.py --trader-id 9217
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_branch_deepdive_9217_v2.py --trader-id 9661
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd

from stock_db import DEFAULT_DB_PATH, connect

OUT_DIR = ROOT / "reports/research/branch-footprint-screen"


def load_all(conn, trader_id: str) -> pd.DataFrame:
    q = """
        SELECT b.trade_date, b.stock_id, b.buy, b.sell, b.net, p.close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id AND p.trade_date = b.trade_date AND p.source = 'finmind'
        WHERE b.securities_trader_id = ? AND b.source = 'finmind'
        ORDER BY b.trade_date
    """
    df = pd.read_sql_query(q, conn, params=(trader_id,))
    df["buy_amt"] = df["buy"] * df["close"]
    df["sell_amt"] = df["sell"] * df["close"]
    df["net_amt"] = df["net"] * df["close"]
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def habit_dow_month(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = df.copy()
    d["dow"] = d["trade_date"].dt.day_name()
    d["gross_amt"] = d["buy_amt"] + d["sell_amt"]
    dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    by_dow = (
        d.groupby("dow")
        .agg(n_trades=("stock_id", "size"), gross_amt=("gross_amt", "sum"), net_amt=("net_amt", "sum"))
        .reindex(dow_order)
    )
    d["ym"] = d["trade_date"].dt.strftime("%Y-%m")
    by_month = d.groupby("ym").agg(
        n_stocks=("stock_id", "nunique"),
        n_trades=("stock_id", "size"),
        gross_amt=("gross_amt", "sum"),
        net_amt=("net_amt", "sum"),
    )
    return by_dow, by_month


def trade_size_distribution(df: pd.DataFrame) -> pd.DataFrame:
    gross = df["buy_amt"] + df["sell_amt"]
    return gross.describe(percentiles=[0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99]).to_frame("gross_amt_per_row")


def price_range_preference(df: pd.DataFrame) -> pd.DataFrame:
    buys = df[df["buy"] > 0].copy()
    bins = [0, 20, 50, 100, 200, 500, 1000, np.inf]
    labels = ["<20", "20-50", "50-100", "100-200", "200-500", "500-1000", ">1000"]
    buys["price_band"] = pd.cut(buys["close"], bins=bins, labels=labels)
    out = buys.groupby("price_band", observed=True).agg(
        n_trades=("stock_id", "size"), buy_amt=("buy_amt", "sum")
    )
    out["pct_of_trades"] = (out["n_trades"] / out["n_trades"].sum() * 100).round(1)
    out["pct_of_amt"] = (out["buy_amt"] / out["buy_amt"].sum() * 100).round(1)
    return out


def position_building(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for stock_id, g in df.groupby("stock_id"):
        g = g.sort_values("trade_date")
        cum_net = g["net"].cumsum()
        gross_shares = (g["buy"] + g["sell"]).sum()
        if gross_shares <= 0:
            continue
        final_net = cum_net.iloc[-1]
        max_abs = cum_net.abs().max()
        n_days = len(g)
        first_date = g["trade_date"].min()
        last_date = g["trade_date"].max()
        span_days = (last_date - first_date).days
        rows.append(
            {
                "stock_id": stock_id,
                "n_trade_days": n_days,
                "first_date": first_date.date(),
                "last_date": last_date.date(),
                "span_calendar_days": span_days,
                "gross_shares": gross_shares,
                "final_net_shares": final_net,
                "max_abs_net_shares": max_abs,
                "retention_ratio": (final_net / max_abs) if max_abs > 0 else np.nan,
                "final_close": g["close"].iloc[-1],
                "est_current_position_value_ntd": final_net * g["close"].iloc[-1],
            }
        )
    return pd.DataFrame(rows)


def current_holdings(pos: pd.DataFrame, min_shares: int = 1000) -> pd.DataFrame:
    holdings = pos[pos["final_net_shares"] >= min_shares].copy()
    holdings = holdings.sort_values("est_current_position_value_ntd", ascending=False)
    return holdings[
        [
            "stock_id",
            "final_net_shares",
            "final_close",
            "est_current_position_value_ntd",
            "n_trade_days",
            "first_date",
            "last_date",
            "retention_ratio",
        ]
    ]


def buy_sell_duality(df: pd.DataFrame) -> dict:
    by_stock_date_buy = (df["buy"] > 0).sum()
    by_stock_date_sell = (df["sell"] > 0).sum()
    both = ((df["buy"] > 0) & (df["sell"] > 0)).sum()
    return {
        "rows_with_buy": int(by_stock_date_buy),
        "rows_with_sell": int(by_stock_date_sell),
        "rows_with_both_same_day_same_stock": int(both),
        "pct_rows_both": round(both / len(df) * 100, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trader-id", default="9217")
    args = ap.parse_args()
    tid = args.trader_id

    conn = connect(DEFAULT_DB_PATH)
    df = load_all(conn, tid)
    conn.close()
    print(f"[INFO] 分點：{tid}")
    print(f"[INFO] 總列數：{len(df):,}，日期範圍 {df['trade_date'].min().date()} ~ {df['trade_date'].max().date()}")
    print(f"[INFO] 涉及股票數：{df['stock_id'].nunique()}")

    by_dow, by_month = habit_dow_month(df)
    print("\n=== 星期分布 ===")
    print(by_dow)
    by_dow.to_csv(OUT_DIR / f"whale_{tid}_habit_by_dow.csv")
    by_month.to_csv(OUT_DIR / f"whale_{tid}_habit_by_month.csv")
    print(f"\n=== 月度趨勢（近12個月） ===")
    print(by_month.tail(12))

    size_dist = trade_size_distribution(df)
    print("\n=== 單筆(單股單日)交易金額分布 ===")
    print(size_dist)
    size_dist.to_csv(OUT_DIR / f"whale_{tid}_habit_trade_size.csv")

    price_pref = price_range_preference(df)
    print("\n=== 買進價格區間偏好 ===")
    print(price_pref)
    price_pref.to_csv(OUT_DIR / f"whale_{tid}_habit_price_range.csv")

    duality = buy_sell_duality(df)
    print("\n=== 買賣雙邊活躍度 ===")
    print(duality)

    pos = position_building(df)
    pos.to_csv(OUT_DIR / f"whale_{tid}_all_positions.csv", index=False)
    print(f"\n[INFO] 全部持有過部位（含已出清）：{len(pos)} 檔股票")

    holdings = current_holdings(pos)
    holdings.to_csv(OUT_DIR / f"whale_{tid}_estimated_current_holdings.csv", index=False)
    print(f"\n=== 估計目前持倉（net_shares >= 1000股，依估計市值排序，前30） ===")
    print(holdings.head(30).to_string(index=False))

    total_est_value = holdings["est_current_position_value_ntd"].sum()
    print(f"\n[INFO] 估計目前總持倉市值（僅本資料庫涵蓋範圍內的股票）：{total_est_value / 1e8:.2f} 億元")
    print(f"[INFO] 估計持有檔數：{len(holdings)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
