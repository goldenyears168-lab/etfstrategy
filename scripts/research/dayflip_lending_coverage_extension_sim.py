#!/usr/bin/env python3
"""Gap #1 Part 1 (follow-up to item R / item AK, 100-item creative-combo plan).

Item AK found dayflip-futures-short's securities-lending filter (item R) has
only 15.1% recent (last-10-session) coverage across the 251-stock dayflip
universe, because `src/sync_stock_chip_daily.py` scopes its
`TaiwanStockSecuritiesLending` pull to `load_etf_constituent_watchlist(conn)`
(ETF-constituent union of 0050/0056 + tracked mutual funds/benchmarks +
supplemental watchlist) -- NOT the dayflip futures universe. The 125 stocks
with zero lending history are, without exception, stocks that were never in
0050 or 0056 (0/125), i.e. this is a deterministic ingest-scope gap, not a
"no data exists" situation.

This script does NOT touch the sync script or the watchlist loader. It
independently calls FinMind's TaiwanStockSecuritiesLending endpoint directly
(via finmind_client.fetch_finmind, same helper the sync script itself uses)
for a random sample of the 125 currently-zero-coverage stocks, over the last
~30 trading days, to check whether upstream FinMind data exists for these
stocks at all. If it does, the fix is a watchlist-scope change (cheap); if it
doesn't, expanding the watchlist would not help (expensive dead end).

Read-only: does not write to data/stocks.db. Only reads
reports/research/dayflip_lending_coverage_check/per_stock_coverage.csv (base)
and calls the live FinMind API (network side-effect only, no local writes).

Output: reports/research/dayflip_lending_coverage_extension_sim/{per_stock_probe.csv,summary.json}
"""
from __future__ import annotations

import csv
import json
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from finmind_client import fetch_finmind  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
BASE_CSV = ROOT / "reports/research/dayflip_lending_coverage_check/per_stock_coverage.csv"
DEST = ROOT / "reports/research/dayflip_lending_coverage_extension_sim"
DEST.mkdir(parents=True, exist_ok=True)

RNG_SEED = 42
SAMPLE_N = 30
LOOKBACK_CALENDAR_DAYS = 45  # generous covering ~30 trading days incl weekends/holidays
REQUEST_DELAY_SEC = 0.4


def log(m: str) -> None:
    print(f"[dayflip-lending-coverage-ext-sim] {m}", flush=True)


def main() -> None:
    rows = list(csv.DictReader(open(BASE_CSV, encoding="utf-8")))
    zero_cov = [r["stock_id"] for r in rows if r["has_lending_data_ever"] == "False"]
    log(f"base file: {len(rows)} stocks total, {len(zero_cov)} with has_lending_data_ever=False")
    assert len(zero_cov) == 125, f"expected 125 zero-coverage stocks per gap brief, got {len(zero_cov)}"

    rng = random.Random(RNG_SEED)
    sample = sorted(rng.sample(zero_cov, min(SAMPLE_N, len(zero_cov))))
    log(f"sampled {len(sample)} of {len(zero_cov)} zero-coverage stocks (seed={RNG_SEED}): {sample}")

    end = date.today()
    start = end - timedelta(days=LOOKBACK_CALENDAR_DAYS)

    out_rows = []
    n_ok, n_empty, n_err = 0, 0, 0
    for i, sid in enumerate(sample):
        if i > 0:
            time.sleep(REQUEST_DELAY_SEC)
        try:
            raw = fetch_finmind("TaiwanStockSecuritiesLending", sid, start, end)
        except Exception as exc:  # noqa: BLE001
            n_err += 1
            out_rows.append(dict(stock_id=sid, status="error", n_rows=0,
                                  n_distinct_dates=0, min_date=None, max_date=None,
                                  error=str(exc)[:200]))
            log(f"  {sid}: ERROR {exc}")
            continue
        dates = sorted({str(item.get("date") or item.get("Date") or "")[:10] for item in raw if item.get("date") or item.get("Date")})
        if raw:
            n_ok += 1
            status = "has_upstream_data"
        else:
            n_empty += 1
            status = "empty"
        out_rows.append(dict(
            stock_id=sid, status=status, n_rows=len(raw),
            n_distinct_dates=len(dates),
            min_date=dates[0] if dates else None,
            max_date=dates[-1] if dates else None,
            error=None,
        ))
        log(f"  {sid}: {status} n_rows={len(raw)} n_dates={len(dates)} range=[{dates[0] if dates else None}, {dates[-1] if dates else None}]")

    fieldnames = ["stock_id", "status", "n_rows", "n_distinct_dates", "min_date", "max_date", "error"]
    with (DEST / "per_stock_probe.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out_rows)

    summary = dict(
        base_zero_coverage_universe_n=len(zero_cov),
        sample_n=len(sample),
        rng_seed=RNG_SEED,
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        n_has_upstream_data=n_ok,
        n_empty_upstream=n_empty,
        n_api_error=n_err,
        pct_has_upstream_data=round(100 * n_ok / len(sample), 1) if sample else None,
    )
    (DEST / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    log(f"summary: {summary}")


if __name__ == "__main__":
    main()
