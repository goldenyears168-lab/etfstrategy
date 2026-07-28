#!/usr/bin/env python3
"""Direction A（sell-side whale concentration · calibration scan）.

跟 scan_whale_buy_concentration.py 邏輯完全對稱，只是把 buy 換成 sell：
對每個 (trade_date, stock_id)，用 stock_broker_branch_daily 算當日所有出手
分點的賣超金額（sell_shares * close），排序後看 Top1／Top1+2 分點占比。

假說：單一（或兩位）分點主導賣超、其他大戶未同步賣出（Top3 占比低、出手
分點數不少）時，疑似該分點出貨、對手盤分散由散戶承接，事件後 forward
excess return 顯著為負。

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/scan_whale_sell_concentration.py \
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
            b.sell,
            p.close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id
         AND p.trade_date = b.trade_date
         AND p.source = 'finmind'
        WHERE b.source = 'finmind'
          AND b.sell > 0
          AND b.trade_date >= ?
          AND b.trade_date <= ?
    """
    df = pd.read_sql_query(query, conn, params=(date_start, date_end))
    conn.close()
    return df


def compute_daily_concentration(df: pd.DataFrame, mega: set[str]) -> pd.DataFrame:
    df = df[~df["stock_id"].isin(mega)]
    # ETF（含 ETN）代碼以 "00" 開頭；實物贖回／建倉常態性由單一參與券商
    # 大量申報，會被誤判成「單一大戶主導賣超」，排除之。
    df = df[~df["stock_id"].str.startswith("00")].copy()
    df["sell_amt"] = df["sell"] * df["close"]

    group_cols = ["trade_date", "stock_id"]
    df["rank"] = df.groupby(group_cols)["sell_amt"].rank(method="first", ascending=False)
    df["total_sell_amt"] = df.groupby(group_cols)["sell_amt"].transform("sum")
    df["n_branches"] = df.groupby(group_cols)["sell_amt"].transform("size")

    top = df[df["rank"] <= 3].copy()
    top_wide = top.pivot_table(
        index=group_cols,
        columns="rank",
        values=["sell_amt", "securities_trader_id"],
        aggfunc="first",
    )
    top_wide.columns = [f"{a}_{int(b)}" for a, b in top_wide.columns]
    top_wide = top_wide.reset_index()

    meta = df[group_cols + ["total_sell_amt", "n_branches"]].drop_duplicates(group_cols)
    out = meta.merge(top_wide, on=group_cols, how="left")

    for col in ("sell_amt_1", "sell_amt_2", "sell_amt_3"):
        if col not in out.columns:
            out[col] = 0.0
        out[col] = out[col].fillna(0.0)

    out["top1_trader_id"] = out.get("securities_trader_id_1")
    out["top1_amt"] = out["sell_amt_1"]
    out["top12_amt"] = out["sell_amt_1"] + out["sell_amt_2"]
    out["top1_share"] = out["top1_amt"] / out["total_sell_amt"]
    out["top12_share"] = out["top12_amt"] / out["total_sell_amt"]
    out["top3_share"] = out["sell_amt_3"] / out["total_sell_amt"]

    cols = [
        "trade_date",
        "stock_id",
        "total_sell_amt",
        "n_branches",
        "top1_trader_id",
        "top1_amt",
        "top1_share",
        "top12_amt",
        "top12_share",
        "top3_share",
    ]
    return out[cols]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--date-start",
        default="2024-07-01",
        help="stock_broker_branch_daily 在此之前僅追蹤極少數分點，集中度計算無意義",
    )
    ap.add_argument("--date-end", default="2026-07-22")
    ap.add_argument("--amt-floor", type=float, default=1e8)
    ap.add_argument("--share-floor", type=float, default=0.80)
    ap.add_argument("--top3-ceiling", type=float, default=0.10)
    ap.add_argument("--min-branches", type=int, default=15)
    ap.add_argument(
        "--include-mega", action="store_true", help="不排除 mega 黑名單（權值股）"
    )
    ap.add_argument(
        "--out-tag",
        default="",
        help="輸出檔名加註記（例如 nomega_nodispersion），避免覆蓋其他 variant 的結果",
    )
    args = ap.parse_args()

    mega = set() if args.include_mega else load_mega_blacklist()
    print(f"[INFO] mega blacklist: {len(mega)} symbols" + (" (停用)" if args.include_mega else ""))
    print(f"[INFO] loading {args.date_start} .. {args.date_end} ...")
    raw = load_frame(args.date_start, args.date_end)
    print(f"[INFO] raw rows: {len(raw):,}")

    daily = compute_daily_concentration(raw, mega)
    print(f"[INFO] (date,stock) groups with sell activity: {len(daily):,}")

    print("\n=== Top1_share / Top12_share 分布（全部 (date,stock) 樣本） ===")
    print(daily[["top1_share", "top12_share", "n_branches", "total_sell_amt"]].describe())

    events = daily[
        (daily["top12_amt"] >= args.amt_floor)
        & (daily["top12_share"] >= args.share_floor)
    ].copy()
    print(f"\n[純門檻：金額≥{args.amt_floor/1e8:.1f}億 且 top12_share≥{args.share_floor:.0%}] events = {len(events):,}")

    events_strict = events[
        (events["top3_share"] < args.top3_ceiling)
        & (events["n_branches"] >= args.min_branches)
    ].copy()
    print(
        f"[加碼：top3_share<{args.top3_ceiling:.0%} 且 n_branches≥{args.min_branches}] "
        f"events = {len(events_strict):,}"
    )

    out_dir = ROOT / "reports/research/branch-footprint-screen"
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = "whale_sell_concentration_calibration_events"
    if args.out_tag:
        fname += f"_{args.out_tag}"
    out_path = out_dir / f"{fname}.csv"
    events_strict.sort_values(["trade_date", "top12_share"], ascending=[True, False]).to_csv(
        out_path, index=False
    )
    print(f"\n[OK] 事件清單寫入 {out_path}")

    print("\n=== 事件按年分布 ===")
    events_strict["year"] = events_strict["trade_date"].str[:4]
    print(events_strict.groupby("year").size())

    print("\n=== 事件按股票分布（前 15 檔最常出現） ===")
    print(events_strict.groupby("stock_id").size().sort_values(ascending=False).head(15))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
