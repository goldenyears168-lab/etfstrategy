#!/usr/bin/env python3
"""Carlo Zarattini Track 2 · 0050 5m ORB smoke test (2023 QQQ spec · TW session).

Topic: cz-intraday-tw-replication (config/research.yaml)
Phase 0: optional --backfill 0050 1m via Yahoo Chart
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from finmind_client import fetch_finmind, finmind_token  # noqa: E402
from research.backtest.archive.cz_track2_0050_orb import (  # noqa: E402
    DEFAULT_STOCK_ID,
    run_track2_backtest,
    write_track2_report,
)
from stock_db import (  # noqa: E402
    DEFAULT_DB_PATH,
    connect,
    finmind_kbar_rows_to_db,
    kbar_day_has_data,
    upsert_stock_kbar_1m,
)
from yahoo_chart_sync import fetch_tw_intraday_kbar_rows  # noqa: E402

DEFAULT_START = date(2022, 2, 8)
REPORT_PATH = ROOT / "reports/research/intraday/cz_track2_0050_orb_smoke_2026.json"


def backfill_0050_1m_yahoo(
    conn,
    *,
    start: date,
    end: date,
    delay: float = 0.4,
) -> int:
    """Yahoo 1m · 僅近 30 日有效 · 7-day chunks."""
    total = 0
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=6))
        rows, sym = fetch_tw_intraday_kbar_rows(DEFAULT_STOCK_ID, cursor, chunk_end, interval="1m")
        if rows:
            total += upsert_stock_kbar_1m(conn, rows)
            print(f"  yahoo {DEFAULT_STOCK_ID} ({sym}) {cursor}..{chunk_end}: +{len(rows)} bars")
        cursor = chunk_end + timedelta(days=1)
        time.sleep(delay)
    return total


def backfill_0050_1m_finmind(
    conn,
    *,
    start: date,
    end: date,
    delay: float = 0.35,
    force: bool = False,
) -> int:
    """FinMind TaiwanStockKBar · 單日一請求 · 歷史 1m 主路徑。"""
    if not finmind_token():
        print("  finmind skip: FINMIND_TOKEN 未設定")
        return 0
    trade_dates = [
        r[0]
        for r in conn.execute(
            """
            SELECT DISTINCT date FROM daily_bars
            WHERE code = ? AND date BETWEEN ? AND ?
            ORDER BY date
            """,
            (BENCHMARK_DAILY_CODE, start.isoformat(), end.isoformat()),
        ).fetchall()
    ]
    total = 0
    for i, trade_date in enumerate(trade_dates, start=1):
        if not force and kbar_day_has_data(conn, DEFAULT_STOCK_ID, trade_date):
            continue
        d = date.fromisoformat(trade_date)
        try:
            raw = fetch_finmind("TaiwanStockKBar", DEFAULT_STOCK_ID, d, d)
        except Exception as exc:
            if i <= 3:
                print(f"  finmind {DEFAULT_STOCK_ID} {trade_date} warn: {exc}")
            time.sleep(delay)
            continue
        rows = finmind_kbar_rows_to_db(DEFAULT_STOCK_ID, raw)
        if rows:
            total += upsert_stock_kbar_1m(conn, rows)
            if i <= 3 or i % 100 == 0 or i == len(trade_dates):
                print(f"  finmind {DEFAULT_STOCK_ID} {trade_date}: +{len(rows)} [{i}/{len(trade_dates)}]")
        time.sleep(delay)
    return total


BENCHMARK_DAILY_CODE = "IR0002"


def main() -> None:
    parser = argparse.ArgumentParser(description="Zarattini Track 2 · 0050 ORB smoke test")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--backfill", action="store_true", help="Pull 0050 1m before run")
    parser.add_argument(
        "--backfill-source",
        choices=("finmind", "yahoo", "both"),
        default="finmind",
        help="finmind=歷史主路徑 · yahoo=近30日 · both",
    )
    parser.add_argument("--start", default=DEFAULT_START.isoformat())
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--out", type=Path, default=REPORT_PATH)
    parser.add_argument("--no-short", action="store_true", help="Long-only sensitivity")
    args = parser.parse_args()

    start_d = date.fromisoformat(args.start)
    end_d = date.fromisoformat(args.end)

    conn = connect(args.db)
    try:
        if args.backfill:
            print(f"Backfill 0050 1m · {start_d} .. {end_d} · source={args.backfill_source}")
            n = 0
            if args.backfill_source in ("finmind", "both"):
                n += backfill_0050_1m_finmind(conn, start=start_d, end=end_d)
            if args.backfill_source in ("yahoo", "both"):
                recent = max(start_d, end_d - timedelta(days=29))
                n += backfill_0050_1m_yahoo(conn, start=recent, end=end_d)
            print(f"  upserted {n} bars\n")

        print("Track 2 · 0050 5m ORB · Zarattini 2023 QQQ spec")
        print(f"DB: {args.db}\n")

        result = run_track2_backtest(conn, allow_short=not args.no_short)
        write_track2_report(result, args.out)

        cov = result.get("coverage", {})
        print(f"Coverage: {cov.get('days', 0)} days · {cov.get('min_date')} — {cov.get('max_date')}")
        print(f"Split: {result.get('split_date')}\n")

        for label, stats in result.get("variants", {}).items():
            oos = stats.get("oos", {})
            print(
                f"  {label:12} n={stats.get('n', 0):4}  "
                f"OOS n={oos.get('n', 0):3} avg_net={oos.get('avg_net_pct', '—')}%  "
                f"Sharpe={oos.get('sharpe', '—')}  {stats.get('verdict', '')}"
            )

        bh = result.get("benchmark_bh_0050", {}).get("oos", {})
        print(f"\n0050 B&H OOS: total={bh.get('total_return_pct', '—')}% Sharpe={bh.get('sharpe', '—')}")

        hyp = result.get("hypotheses", {}).get("H-CZ-T2-1", {})
        print(f"\nH-CZ-T2-1 (atr5_eod): {'PASS' if hyp.get('pass') else 'FAIL'}")
        print(f"Report: {args.out}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
