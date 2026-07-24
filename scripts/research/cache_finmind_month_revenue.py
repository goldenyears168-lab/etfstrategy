#!/usr/bin/env python3
"""Cache FinMind TaiwanStockMonthRevenue for liquid TW stocks (PIT via create_time).

Out: reports/research/chip-overlays/cache/month_revenue_liquid.parquet
     (+ .csv mirror for easy inspect)

Book-only research · uses FINMIND_TOKEN · does not print secrets.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from finmind_client import fetch_finmind, finmind_token  # noqa: E402

OUT = ROOT / "reports" / "research" / "chip-overlays" / "cache"
DB = ROOT / "data" / "stocks.db"
START, END = date(2023, 1, 1), date(2026, 7, 20)
DELAY = 0.35


def liquid_ids(min_adv: float = 1e7) -> list[str]:
    conn = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True)
    ids = [
        r[0]
        for r in conn.execute(
            """
            SELECT stock_id FROM (
              SELECT stock_id,
                     AVG(CASE WHEN amount>0 THEN amount ELSE close*volume END) adv
              FROM stock_daily_bars
              WHERE source='finmind' AND trade_date>='2024-07-01' AND trade_date<='2026-07-20'
                AND length(stock_id)=4 AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
              GROUP BY stock_id HAVING COUNT(*)>=180 AND adv>=?
            )
            """,
            (min_adv,),
        ).fetchall()
    ]
    conn.close()
    return sorted(ids)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0=all liquid")
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "month_revenue_liquid.parquet"
    assert finmind_token(), "FINMIND_TOKEN missing"

    done: set[str] = set()
    frames: list[pd.DataFrame] = []
    if args.resume and path.exists():
        old = pd.read_parquet(path)
        frames.append(old)
        done = set(old["stock_id"].astype(str).unique())
        print(f"resume: have {len(done)} stocks")

    ids = liquid_ids()
    if args.limit:
        ids = ids[: args.limit]
    todo = [s for s in ids if s not in done]
    print(f"fetch {len(todo)} / {len(ids)} stocks")

    rows_all: list[dict] = []
    for i, sid in enumerate(todo, 1):
        try:
            raw = fetch_finmind(
                "TaiwanStockMonthRevenue", sid, START, END, timeout=120
            )
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {sid}: {type(exc).__name__}: {exc}")
            time.sleep(DELAY)
            continue
        for item in raw:
            rows_all.append(
                {
                    "stock_id": sid,
                    "date": str(item.get("date") or "")[:10],
                    "revenue": item.get("revenue"),
                    "revenue_month": item.get("revenue_month"),
                    "revenue_year": item.get("revenue_year"),
                    "create_time": str(item.get("create_time") or "")[:10],
                }
            )
        if i % 25 == 0:
            print(f"  …{i}/{len(todo)}")
            # checkpoint
            if rows_all:
                part = pd.DataFrame(rows_all)
                frames.append(part)
                rows_all = []
                pd.concat(frames, ignore_index=True).drop_duplicates(
                    subset=["stock_id", "revenue_year", "revenue_month"], keep="last"
                ).to_parquet(path, index=False)
        time.sleep(DELAY)

    if rows_all:
        frames.append(pd.DataFrame(rows_all))
    if not frames:
        print("no data")
        return
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(
        subset=["stock_id", "revenue_year", "revenue_month"], keep="last"
    )
    df.to_parquet(path, index=False)
    df.to_csv(OUT / "month_revenue_liquid.csv", index=False)
    print(
        f"wrote {path} rows={len(df)} stocks={df.stock_id.nunique()} "
        f"months={df.revenue_year.min()}-{df.revenue_month.min()}.."
        f"{df.revenue_year.max()}-{df.revenue_month.max()}"
    )


if __name__ == "__main__":
    main()
