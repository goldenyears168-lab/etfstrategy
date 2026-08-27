#!/usr/bin/env python3
"""2026-08-11: genuinely real-time NQ collector via Databento (CME Globex
direct feed), replacing the Yahoo-based nq_second_quote_collector.py for
this purpose -- confirmed empirically that Yahoo's NQ=F quote runs ~10min
behind wall-clock (regularMarketTime vs now), unusable for any second-level
analysis. Databento's Live client streams individual trade prints with
real exchange timestamps (nanosecond ts_event), not a polled snapshot.

Requires DATABENTO_API_KEY in .env (see .env.example) -- sign up at
https://databento.com, no CME data license needed for retail/research use
at Databento's published rates (dataset=GLBX.MDP3 covers CME Globex incl.
NQ). Never paste the key into chat/commit it -- .env only, gitignored.

symbols="NQ.c.0" (stype_in="continuous") tracks the front-month NQ
contract automatically, matching how nq_signal.py tracks front-month via
NQ=F on the Yahoo side.

Writes one JSON line per trade print to
~/goldenstocks-data/cache/tmf_channel/tick_seconds/nq_databento_{YYYY-MM-DD}.jsonl
  {"ts": "<iso with offset, real exchange ts_event>", "price": <float>, "size": <int>, "symbol": "NQZ6"}

Run: PYTHONPATH=src .venv/bin/python scripts/research/nq_databento_live_collector.py
Stop: kill the process (find via `pgrep -f nq_databento_live_collector`).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")

import databento as db  # noqa: E402

from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import DATA_DIR  # noqa: E402

_TZ = ZoneInfo("Asia/Taipei")
OUT_DIR = DATA_DIR.parent / "cache" / "tmf_channel" / "tick_seconds"
DATASET = "GLBX.MDP3"
SYMBOLS = ["NQ.c.0"]


def _out_path(now_tw: datetime) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR / f"nq_databento_{now_tw.strftime('%Y-%m-%d')}.jsonl"


def main() -> int:
    load_project_dotenv()
    api_key = os.environ.get("DATABENTO_API_KEY", "").strip()
    if not api_key:
        print("DATABENTO_API_KEY not set in .env -- see .env.example. Sign up at https://databento.com first.")
        return 1

    client = db.Live(key=api_key)
    client.subscribe(
        dataset=DATASET,
        schema="trades",
        stype_in="continuous",
        symbols=SYMBOLS,
    )
    print(f"connected -- streaming {SYMBOLS} trades from {DATASET}", flush=True)

    n_written = 0
    for record in client:
        try:
            price = getattr(record, "price", None)
            ts_event_ns = getattr(record, "ts_event", None)
            size = getattr(record, "size", None)
            if price is None or ts_event_ns is None:
                continue
            # DBN fixed-point price: integer scaled by 1e-9
            price_f = float(price) / 1e9
            ts_utc = datetime.fromtimestamp(ts_event_ns / 1e9, tz=timezone.utc)
            ts_tw = ts_utc.astimezone(_TZ)
            rec = {
                "ts": ts_tw.isoformat(timespec="milliseconds"),
                "price": price_f,
                "size": int(size) if size is not None else None,
                "symbol": str(getattr(record, "symbol", SYMBOLS[0])),
            }
            with _out_path(ts_tw).open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_written += 1
            if n_written % 500 == 0:
                print(f"{n_written} trades written so far (latest: {rec['ts']} @ {price_f})", flush=True)
        except Exception as exc:  # noqa: BLE001 -- best-effort collector, never crash the loop
            print(f"record error: {exc}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
