#!/usr/bin/env python3
"""方向 B（對倒/換手偵測）· buy-branch vs sell-branch crossing scan.

對每個 (trade_date, stock_id)，用 stock_broker_branch_daily 分別算出當日所有
出手分點的「買超金額」（buy*close）與「賣超金額」（sell*close），找出：
  - Top1 買方分點金額 ≥ 1億 且占當日總買方金額 ≥50%
  - Top1 賣方分點金額 ≥ 1億 且占當日總賣方金額 ≥50%
  - Top1 買方分點 ≠ Top1 賣方分點
  - 雙方金額量級相近：min(買,賣)/max(買,賣) ≥ 0.5

這比「買方集中、其他人分散」更可能是真實的大宗持股轉手（一個大戶賣給另一個
大戶），訊號意義跟「大戶對散戶」不一樣，值得分開驗證。

注意：這裡用的是「買超金額」「賣超金額」（=買進股數*close / 賣出股數*close），
不是淨額；一個分點若同時買又賣，buy 和 sell 是分開統計的兩個數字（FinMind
分點進出表本身就是分開的買量/賣量欄位，不是 net）。

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/scan_whale_crossing.py \
    --date-start 2024-07-01 --date-end 2026-07-22
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

from stock_db import DEFAULT_DB_PATH, connect

MEGA_BLACKLIST_PATH = (
    ROOT / "reports/research/branch-footprint-screen/ab58_xMega_copytrade/mega_blacklist_v1.json"
)
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"


def load_mega_blacklist() -> set[str]:
    data = json.loads(MEGA_BLACKLIST_PATH.read_text(encoding="utf-8"))
    return set(data["symbols"])


def load_frame(date_start: str, date_end: str) -> pd.DataFrame:
    conn = connect(DEFAULT_DB_PATH)
    query = """
        SELECT
            b.trade_date,
            b.stock_id,
            b.securities_trader_id,
            b.buy,
            b.sell,
            p.close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id
         AND p.trade_date = b.trade_date
         AND p.source = 'finmind'
        WHERE b.source = 'finmind'
          AND (b.buy > 0 OR b.sell > 0)
          AND b.trade_date >= ?
          AND b.trade_date <= ?
    """
    df = pd.read_sql_query(query, conn, params=(date_start, date_end))
    conn.close()
    return df


def _top1(df: pd.DataFrame, amt_col: str, group_cols: list[str]) -> pd.DataFrame:
    """回傳每個 (trade_date, stock_id) 的 top1 分點（依 amt_col 排序）+ 該分點金額
    + 該日該方向總金額 + 出手分點數。"""
    sub = df[df[amt_col] > 0].copy()
    sub["rank"] = sub.groupby(group_cols)[amt_col].rank(method="first", ascending=False)
    sub["total_amt"] = sub.groupby(group_cols)[amt_col].transform("sum")
    sub["n_branches"] = sub.groupby(group_cols)[amt_col].transform("size")
    top1 = sub[sub["rank"] == 1][group_cols + ["securities_trader_id", amt_col, "total_amt", "n_branches"]]
    return top1.rename(
        columns={
            "securities_trader_id": "trader_id",
            amt_col: "top1_amt",
        }
    )


def compute_daily_crossing(df: pd.DataFrame, mega: set[str]) -> pd.DataFrame:
    df = df[~df["stock_id"].isin(mega)]
    # ETF（含 ETN）代碼以 "00" 開頭，實物申購/建倉常態性由單一參與券商大量申報，
    # 排除避免假訊號（沿用既有慣例）。
    df = df[~df["stock_id"].str.startswith("00")].copy()
    df["buy_amt"] = df["buy"] * df["close"]
    df["sell_amt"] = df["sell"] * df["close"]

    group_cols = ["trade_date", "stock_id"]
    buy_top1 = _top1(df, "buy_amt", group_cols)
    sell_top1 = _top1(df, "sell_amt", group_cols)

    merged = buy_top1.merge(
        sell_top1,
        on=group_cols,
        how="inner",
        suffixes=("_buy", "_sell"),
    )
    merged = merged.rename(
        columns={
            "trader_id_buy": "buy_trader_id",
            "trader_id_sell": "sell_trader_id",
            "top1_amt_buy": "buy_top1_amt",
            "top1_amt_sell": "sell_top1_amt",
            "total_amt_buy": "total_buy_amt",
            "total_amt_sell": "total_sell_amt",
            "n_branches_buy": "n_buy_branches",
            "n_branches_sell": "n_sell_branches",
        }
    )
    merged["buy_share"] = merged["buy_top1_amt"] / merged["total_buy_amt"]
    merged["sell_share"] = merged["sell_top1_amt"] / merged["total_sell_amt"]
    merged["cross_ratio"] = merged[["buy_top1_amt", "sell_top1_amt"]].min(axis=1) / merged[
        ["buy_top1_amt", "sell_top1_amt"]
    ].max(axis=1)
    return merged


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--date-start",
        default="2024-07-01",
        help="stock_broker_branch_daily 在此之前僅追蹤極少數分點，集中度計算無意義",
    )
    ap.add_argument("--date-end", default="2026-07-22")
    ap.add_argument("--amt-floor", type=float, default=1e8, help="買方/賣方 top1 金額門檻")
    ap.add_argument("--share-floor", type=float, default=0.50, help="買方/賣方 top1 占比門檻")
    ap.add_argument("--cross-ratio-floor", type=float, default=0.50, help="min(買,賣)/max(買,賣) 門檻")
    ap.add_argument(
        "--include-mega", action="store_true", help="不排除 mega 黑名單（權值股）"
    )
    ap.add_argument(
        "--out-tag",
        default="",
        help="輸出檔名加註記，避免覆蓋其他 variant 的結果",
    )
    args = ap.parse_args()

    mega = set() if args.include_mega else load_mega_blacklist()
    print(f"[INFO] mega blacklist: {len(mega)} symbols" + (" (停用)" if args.include_mega else ""))
    print(f"[INFO] loading {args.date_start} .. {args.date_end} ...")
    raw = load_frame(args.date_start, args.date_end)
    print(f"[INFO] raw rows: {len(raw):,}")

    daily = compute_daily_crossing(raw, mega)
    print(f"[INFO] (date,stock) groups with both buy & sell top1: {len(daily):,}")

    events = daily[
        (daily["buy_top1_amt"] >= args.amt_floor)
        & (daily["buy_share"] >= args.share_floor)
        & (daily["sell_top1_amt"] >= args.amt_floor)
        & (daily["sell_share"] >= args.share_floor)
        & (daily["buy_trader_id"] != daily["sell_trader_id"])
        & (daily["cross_ratio"] >= args.cross_ratio_floor)
    ].copy()
    print(
        f"\n[門檻：買/賣 top1 金額≥{args.amt_floor/1e8:.1f}億 且占比≥{args.share_floor:.0%}、"
        f"買賣分點不同、cross_ratio≥{args.cross_ratio_floor:.0%}] events = {len(events):,}"
    )

    cols = [
        "trade_date",
        "stock_id",
        "buy_trader_id",
        "buy_top1_amt",
        "buy_share",
        "n_buy_branches",
        "total_buy_amt",
        "sell_trader_id",
        "sell_top1_amt",
        "sell_share",
        "n_sell_branches",
        "total_sell_amt",
        "cross_ratio",
    ]
    events = events[cols].sort_values(["trade_date", "cross_ratio"], ascending=[True, False])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fname = "whale_crossing_events"
    if args.out_tag:
        fname += f"_{args.out_tag}"
    out_path = OUT_DIR / f"{fname}.csv"
    events.to_csv(out_path, index=False)
    print(f"\n[OK] 事件清單寫入 {out_path}")

    if events.empty:
        print("[INFO] 無事件，結束。")
        return 0

    print("\n=== 事件按年分布 ===")
    events["year"] = events["trade_date"].str[:4]
    print(events.groupby("year").size())
    events.drop(columns=["year"], inplace=True)

    print("\n=== 事件按股票分布（前 15 檔最常出現） ===")
    print(events.groupby("stock_id").size().sort_values(ascending=False).head(15))

    print("\n=== 賣方分點出現次數（前 15，觀察是否有重複出現的分點——可能是常態性轉倉/信託帳戶） ===")
    print(events.groupby("sell_trader_id").size().sort_values(ascending=False).head(15))

    print("\n=== 買方分點出現次數（前 15） ===")
    print(events.groupby("buy_trader_id").size().sort_values(ascending=False).head(15))

    print("\n=== 買賣分點組合出現次數（前 15，觀察是否有固定的 A→B pair 反覆出現） ===")
    print(
        events.groupby(["buy_trader_id", "sell_trader_id"])
        .size()
        .sort_values(ascending=False)
        .head(15)
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
