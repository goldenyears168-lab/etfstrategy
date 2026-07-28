#!/usr/bin/env python3
"""A1 修復：回補除權息/減資官方參考價事件（TaiwanStockDividendResult +
TaiwanStockCapitalReductionReferencePrice），並算出回溯調整後收盤價
（stock_close_adjusted.adj_close_v2），取代目前因 build_stock_rows() 靜默吞掉
TaiwanStockPriceAdj 錯誤而大量 NULL 的 adj_close。

一次呼叫拿全部歷史（不像日K要逐日抓），對 FinMind 每日額度友善：
全市場約 stock 數 × 2 次呼叫（不逐年重抓）。

用法：
  PYTHONPATH=src .venv/bin/python \\
    scripts/backfill_price_adjustment_events.py [--max-stocks N] [--force]
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from finmind_client import fetch_finmind_dataset, finmind_token  # noqa: E402
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import connect  # noqa: E402
from stock_db.price_adjustment import (  # noqa: E402
    compute_adjusted_close,
    load_adjustment_events,
    upsert_stock_close_adjusted,
    upsert_stock_price_adjustment_events,
)

EARLIEST = date(2010, 1, 1)
REQUEST_DELAY_SEC = 0.35


def normalize_dividend_result(stock_id: str, rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        ex_date = str(r.get("date") or "")[:10]
        before = r.get("before_price")
        after = r.get("after_price")
        if not ex_date or before is None or after is None:
            continue
        out.append(
            {
                "stock_id": stock_id,
                "ex_date": ex_date,
                "event_type": "dividend",
                "before_price": float(before),
                "after_price": float(after),
                "detail": str(r.get("stock_and_cache_dividend") or ""),
                "source": "finmind",
            }
        )
    return out


def normalize_capital_reduction(stock_id: str, rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        ex_date = str(r.get("date") or "")[:10]
        before = r.get("ClosingPriceonTheLastTradingDay")
        after = r.get("PostReductionReferencePrice")
        if not ex_date or before is None or after is None:
            continue
        out.append(
            {
                "stock_id": stock_id,
                "ex_date": ex_date,
                "event_type": "capital_reduction",
                "before_price": float(before),
                "after_price": float(after),
                "detail": str(r.get("ReasonforCapitalReduction") or ""),
                "source": "finmind",
            }
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-stocks", type=int, default=0)
    ap.add_argument("--force", action="store_true", help="重抓即使該股已有事件紀錄")
    ap.add_argument("--delay", type=float, default=REQUEST_DELAY_SEC)
    args = ap.parse_args()

    load_project_dotenv()
    if not finmind_token():
        print("ERROR: FINMIND_TOKEN unset", file=sys.stderr)
        return 2

    conn = connect()
    stock_ids = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT stock_id FROM stock_daily_bars WHERE source='finmind' ORDER BY stock_id"
        ).fetchall()
    ]
    if args.max_stocks > 0:
        stock_ids = stock_ids[: args.max_stocks]
    print(f"universe: {len(stock_ids)} stocks", flush=True)

    if not args.force:
        done = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT stock_id FROM stock_price_adjustment_events"
            ).fetchall()
        }
        todo = [s for s in stock_ids if s not in done]
        print(f"already have events for {len(done)}, todo={len(todo)}", flush=True)
        stock_ids = todo

    n_ok = 0
    n_err = 0
    n_events_total = 0
    n_adj_rows_total = 0
    t0 = time.time()

    for i, sid in enumerate(stock_ids, 1):
        try:
            div_rows = fetch_finmind_dataset(
                "TaiwanStockDividendResult", data_id=sid, start=EARLIEST, end=date.today()
            )
            time.sleep(args.delay)
            cap_rows = fetch_finmind_dataset(
                "TaiwanStockCapitalReductionReferencePrice", data_id=sid, start=EARLIEST, end=date.today()
            )
            time.sleep(args.delay)
        except requests.HTTPError as exc:
            n_err += 1
            print(f"  WARN {sid}: {exc}", file=sys.stderr)
            continue

        events = normalize_dividend_result(sid, div_rows) + normalize_capital_reduction(sid, cap_rows)
        n_events_total += upsert_stock_price_adjustment_events(conn, events)

        raw = conn.execute(
            "SELECT trade_date, close FROM stock_daily_bars WHERE stock_id=? AND source='finmind' AND close IS NOT NULL",
            (sid,),
        ).fetchall()
        if raw:
            raw_closes = {r[0]: r[1] for r in raw}
            ev_for_calc = load_adjustment_events(conn, sid, raw_closes)
            adj = compute_adjusted_close(raw_closes, ev_for_calc)
            rows = [
                {
                    "stock_id": sid,
                    "trade_date": d,
                    "adj_close_v2": v[0],
                    "cum_factor": v[1],
                    "n_events_applied": v[2],
                    "source": "finmind",
                }
                for d, v in adj.items()
            ]
            n_adj_rows_total += upsert_stock_close_adjusted(conn, rows)

        n_ok += 1
        if i % 50 == 0 or i == len(stock_ids):
            elapsed = time.time() - t0
            rate = i / max(elapsed, 1)
            eta_min = (len(stock_ids) - i) / max(rate, 1e-9) / 60
            print(
                f"[{i}/{len(stock_ids)}] ok={n_ok} err={n_err} events={n_events_total} "
                f"adj_rows={n_adj_rows_total} elapsed={elapsed/60:.1f}m eta={eta_min:.1f}m",
                flush=True,
            )

    conn.close()
    print(f"DONE ok={n_ok} err={n_err} events_written={n_events_total} adj_rows_written={n_adj_rows_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
