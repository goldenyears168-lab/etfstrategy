#!/usr/bin/env python3
"""2026-08-11: Yahoo only serves 1m intraday bars for a trailing ~8 days, so
the "台美偏離差" (TW-US MA-deviation spread) research at real 1-minute
granularity is permanently capped at a few days of history unless something
accumulates it daily. This fetches the current trailing 8-day 1m window for
NQ=F and ES=F and merges it into a persistent local archive (parquet,
keyed by UTC timestamp, deduped) -- run this daily (before the trailing
window rolls a day off) and the archive grows without bound instead of
staying capped at 8 days.

Safe to re-run same-day (dedup on index). Research-only, no order-layer
touch, no live capital involved.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, "src")

import pandas as pd  # noqa: E402

from us_futures_overnight import ES_YAHOO, NQ_YAHOO, fetch_yahoo_intraday_closes  # noqa: E402

ARCHIVE_DIR = Path("/Users/jackm4/goldenstocks-data/cache/tmf_channel/nq_es_1m_archive")
SYMBOLS = {"NQ": NQ_YAHOO, "ES": ES_YAHOO}


def archive_path(code: str) -> Path:
    return ARCHIVE_DIR / f"{code}_1m.parquet"


def fetch_recent(yahoo_symbol: str) -> pd.Series:
    end = date.today()
    start = end - timedelta(days=6)  # yfinance pads end by +2d internally; 6+2=8 stays inside the 1m cap
    return fetch_yahoo_intraday_closes(yahoo_symbol, start, end, interval="1m")


def merge_and_save(code: str, fresh: pd.Series) -> int:
    path = archive_path(code)
    if fresh.empty:
        return 0
    fresh_df = fresh.rename("close").to_frame()
    if path.exists():
        existing = pd.read_parquet(path)
        combined = pd.concat([existing, fresh_df])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    else:
        combined = fresh_df.sort_index()
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(path)
    return len(combined)


def main() -> None:
    for code, yahoo_symbol in SYMBOLS.items():
        fresh = fetch_recent(yahoo_symbol)
        total = merge_and_save(code, fresh)
        span = ""
        path = archive_path(code)
        if path.exists() and total:
            df = pd.read_parquet(path)
            span = f" span={df.index.min().date()}..{df.index.max().date()}"
        print(f"{code}: fetched {len(fresh)} fresh bars, archive now {total} rows{span}")


if __name__ == "__main__":
    main()
