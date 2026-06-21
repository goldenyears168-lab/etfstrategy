#!/usr/bin/env python3
"""Sync benchmark ETF constituents (0050) into benchmark_constituents tables."""

from __future__ import annotations

import argparse
import re
import sys
import warnings
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests
import urllib3

from project_config import BENCHMARK_ETF_WATCHLIST_CODES, parse_etf_codes
from stock_db import DEFAULT_DB_PATH, connect
from stock_db.benchmark import upsert_benchmark_constituents, upsert_benchmark_constituents_meta
from sync_stock_beta import fetch_stock_universe

YUANTA_RATIO_URL = "https://www.yuantaetfs.com/product/detail/{benchmark_code}/ratio"
YUANTA_ROW_PATTERN = re.compile(r'"(\d{4})","([^"]+)","([A-Z][^"]*)"')
TW_STOCK_ID = re.compile(r"^\d{4}$")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
}


@dataclass(frozen=True)
class BenchmarkSnapshot:
    benchmark_code: str
    snapshot_date: str
    holdings: list[dict]


def _cjk_name(name: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in name)


def fetch_yuanta_benchmark_snapshot(
    benchmark_code: str,
    *,
    listed_ids: set[str] | None = None,
    session: requests.Session | None = None,
    min_holdings: int = 40,
) -> BenchmarkSnapshot:
    code = benchmark_code.upper()
    if listed_ids is None:
        listed_ids = {row.stock_id for row in fetch_stock_universe(include_emerging=False)}

    sess = session or requests.Session()
    url = YUANTA_RATIO_URL.format(benchmark_code=code)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
        response = sess.get(url, headers=HEADERS, timeout=30, verify=False)
    response.raise_for_status()
    if not response.text.strip():
        raise RuntimeError(f"Yuanta returned empty body for {code}")

    holdings: list[dict] = []
    seen: set[str] = set()
    for stock_id, stock_name, _english in YUANTA_ROW_PATTERN.findall(response.text):
        if not TW_STOCK_ID.match(stock_id):
            continue
        if stock_id.startswith("00"):
            continue
        if stock_id not in listed_ids or not _cjk_name(stock_name):
            continue
        if stock_id in seen:
            continue
        seen.add(stock_id)
        holdings.append(
            {
                "stock_id": stock_id,
                "stock_name": stock_name.strip(),
                "weight_pct": None,
            }
        )

    if len(holdings) < min_holdings:
        raise RuntimeError(
            f"Yuanta {code} parsed only {len(holdings)} listed stocks "
            f"(need >= {min_holdings})"
        )

    holdings.sort(key=lambda row: row["stock_id"])
    return BenchmarkSnapshot(
        benchmark_code=code,
        snapshot_date=date.today().isoformat(),
        holdings=holdings,
    )


def sync_benchmark_constituents(
    db_path: Path,
    benchmark_codes: tuple[str, ...],
    *,
    quiet: bool = False,
) -> dict[str, int]:
    conn = connect(db_path)
    stats = {"benchmarks": 0, "holdings": 0}
    try:
        listed_ids = {row.stock_id for row in fetch_stock_universe(include_emerging=False)}
        session = requests.Session()
        for benchmark_code in benchmark_codes:
            snapshot = fetch_yuanta_benchmark_snapshot(
                benchmark_code,
                listed_ids=listed_ids,
                session=session,
            )
            upsert_benchmark_constituents_meta(
                conn,
                {
                    "benchmark_code": snapshot.benchmark_code,
                    "snapshot_date": snapshot.snapshot_date,
                    "holding_count": len(snapshot.holdings),
                    "source": "yuanta_html",
                },
            )
            rows = [
                {
                    "benchmark_code": snapshot.benchmark_code,
                    "snapshot_date": snapshot.snapshot_date,
                    "stock_id": item["stock_id"],
                    "stock_name": item["stock_name"],
                    "weight_pct": item["weight_pct"],
                    "source": "yuanta_html",
                }
                for item in snapshot.holdings
            ]
            n = upsert_benchmark_constituents(conn, rows)
            stats["benchmarks"] += 1
            stats["holdings"] += n
            if not quiet:
                print(
                    f"{snapshot.benchmark_code}: {n} stocks @ {snapshot.snapshot_date} "
                    f"(source=yuanta_html)"
                )
    finally:
        conn.close()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync benchmark ETF constituents (0050)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--benchmark-codes",
        default=",".join(BENCHMARK_ETF_WATCHLIST_CODES),
        help="Comma-separated benchmark ETF codes (default: 0050)",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    codes = parse_etf_codes(args.benchmark_codes, default=BENCHMARK_ETF_WATCHLIST_CODES)
    if not codes:
        print("No benchmark codes configured", file=sys.stderr)
        return 2

    try:
        stats = sync_benchmark_constituents(args.db, codes, quiet=args.quiet)
    except Exception as exc:
        print(f"benchmark sync failed: {exc}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(
            f"Done: {stats['benchmarks']} benchmark(s), "
            f"{stats['holdings']} constituent row(s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
