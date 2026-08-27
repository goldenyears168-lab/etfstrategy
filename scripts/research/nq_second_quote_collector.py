#!/usr/bin/env python3
"""2026-08-11: standalone NQ live-quote collector -- companion to
tmf_second_quote_collector.py (TX side). Same purpose: accumulate real
second-level samples to eventually evaluate the "N-second acceleration"
idea against real data instead of guessing.

2026-08-12 bug fix: the original version created ONE yf.Ticker(NQ_YAHOO)
before the loop and called .fast_info on that SAME instance every poll for
the rest of the process's life. yfinance's fast_info is a lazily-fetched,
per-Ticker-instance-cached object -- confirmed live, this served the exact
same price for 18+ continuous hours (nq_2026-08-11/12.jsonl: ~57000 rows,
only 2 distinct values total, the second one frozen from early on). That
data is garbage (zero information), archived aside, not deleted --
scripts/research/tick_seconds/ARCHIVED_2026-08-12_frozen_ticker_bug/.
Fix: recreate yf.Ticker(NQ_YAHOO) fresh every poll so .fast_info is forced
to actually refetch. Confirmed empirically (8 fresh-Ticker polls, 1s apart)
this genuinely returns varying prices (29782.0 -> 29781.5 on the 8th poll)
and each fetch takes ~0.5-2s (a real network round trip, not a cache hit).

POLL_SEC kept at 1.0 per explicit user instruction (2026-08-12): each poll
is now a real HTTP request (no longer a cheap cache read, ~0.5-2s round
trip observed), so the loop below times each fetch and only sleeps the
REMAINDER of the 1s budget (never a flat sleep(1.0) on top of the fetch
itself) -- this is what makes "attempt a fetch every ~1s" actually true
instead of drifting to ~1.5-3s per cycle. Long-run rate-limiting from
Yahoo's free/unauthenticated endpoint is a real risk at this cadence over
many days; if this collector starts seeing sustained quote errors, that is
the first thing to check (raise POLL_SEC back up, or add backoff), not a
reason to silently slow it down here.

Writes to ~/goldenstocks-data/cache/tmf_channel/tick_seconds/nq_{YYYY-MM-DD}.jsonl
  {"ts": "<iso with offset>", "price": <float>, "symbol": "NQ=F"}

Run: .venv/bin/python scripts/research/nq_second_quote_collector.py
Stop: kill the process (find via `pgrep -f nq_second_quote_collector`).
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")

import yfinance as yf  # noqa: E402

from stock_db import DATA_DIR  # noqa: E402
from us_futures_overnight import NQ_YAHOO  # noqa: E402

_TZ = ZoneInfo("Asia/Taipei")
OUT_DIR = DATA_DIR.parent / "cache" / "tmf_channel" / "tick_seconds"
POLL_SEC = 1.0


def _out_path(now: datetime) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR / f"nq_{now.strftime('%Y-%m-%d')}.jsonl"


def main() -> int:
    print(f"collecting {NQ_YAHOO} every {POLL_SEC}s -> {OUT_DIR} (fresh Ticker() per poll, forces a real refetch)", flush=True)

    n_written = 0
    n_errors = 0
    last_report = time.monotonic()
    while True:
        cycle_start = time.monotonic()
        now = datetime.now(tz=_TZ)
        try:
            # Fresh instance every poll -- fast_info caches per-instance, a
            # single long-lived Ticker served the same stale snapshot for
            # 18+ hours (see module docstring, 2026-08-12 bug fix).
            ticker = yf.Ticker(NQ_YAHOO)
            fi = ticker.fast_info
            price = fi.get("lastPrice") if hasattr(fi, "get") else getattr(fi, "last_price", None)
            if price is not None:
                rec = {"ts": now.isoformat(timespec="milliseconds"), "price": float(price), "symbol": NQ_YAHOO}
                with _out_path(now).open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_written += 1
        except Exception as exc:  # noqa: BLE001 -- best-effort collector, never crash the loop
            n_errors += 1
            print(f"[{now.isoformat(timespec='seconds')}] quote error: {exc}", flush=True)
        if time.monotonic() - last_report > 300:
            print(
                f"[{now.isoformat(timespec='seconds')}] {n_written} samples written, "
                f"{n_errors} errors so far",
                flush=True,
            )
            last_report = time.monotonic()
        # Sleep only the remainder of the 1s budget -- the fetch itself is a
        # real ~0.5-2s network round trip now, a flat sleep(POLL_SEC) on top
        # of that would drift the actual cadence well past "every second".
        elapsed = time.monotonic() - cycle_start
        remaining = POLL_SEC - elapsed
        if remaining > 0:
            time.sleep(remaining)


if __name__ == "__main__":
    raise SystemExit(main())
