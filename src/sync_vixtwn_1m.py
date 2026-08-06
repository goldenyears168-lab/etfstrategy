"""Sync VIXTWN 1-minute OHLC from FinMind TaiwanOptionVix (15s ticks).

Source
------
FinMind ``TaiwanOptionVix``: official TAIFEX VIXTWN prints every 15s during
**day session only** (09:00–13:45). No night session. Coverage from ~2026-03.

This is **not** recomputed from TXO chain (daily settlement only). We aggregate
the official 15s series → 1m bars (open/high/low/close + n_ticks).

Writes
------
1. SQLite ``market_vix_1m`` (symbol='VIXTWN', source='finmind')
2. Optional JSON cache for channel_lab research

Usage
-----
    PYTHONPATH=src .venv/bin/python src/sync_vixtwn_1m.py
    PYTHONPATH=src .venv/bin/python src/sync_vixtwn_1m.py --start 2026-06-26 --end 2026-07-31
    PYTHONPATH=src .venv/bin/python src/sync_vixtwn_1m.py --dry-run --json-out reports/research/channel_lab/vixtwn_1m_cache.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

from finmind_client import fetch_finmind_json
from stock_db import connect
from stock_db.util import utc_now_iso

_CREATE = """
CREATE TABLE IF NOT EXISTS market_vix_1m (
  symbol TEXT NOT NULL,
  ts TEXT NOT NULL,
  open REAL NOT NULL,
  high REAL NOT NULL,
  low REAL NOT NULL,
  close REAL NOT NULL,
  n_ticks INTEGER NOT NULL,
  source TEXT NOT NULL DEFAULT 'finmind',
  synced_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (symbol, ts, source)
);
CREATE INDEX IF NOT EXISTS idx_market_vix_1m_date
  ON market_vix_1m (symbol, substr(ts, 1, 10), source);
"""

_UPSERT = """
INSERT INTO market_vix_1m (symbol, ts, open, high, low, close, n_ticks, source, synced_at)
VALUES (:symbol, :ts, :open, :high, :low, :close, :n_ticks, :source, :synced_at)
ON CONFLICT(symbol, ts, source) DO UPDATE SET
  open=excluded.open, high=excluded.high, low=excluded.low,
  close=excluded.close, n_ticks=excluded.n_ticks, synced_at=excluded.synced_at
"""


def _minute_floor(t: str) -> str:
    """'09:00:45' → '09:00' ; also accept '09:00'."""
    s = str(t).strip()
    if len(s) >= 5:
        return s[:5]
    return s


def ticks_to_1m(rows: list[dict]) -> list[dict]:
    """Aggregate {date,time,vix} ticks → 1m OHLC bars with ts='YYYY-MM-DD HH:MM'."""
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in rows:
        d = r.get("date")
        tm = r.get("time")
        v = r.get("vix")
        if not d or not tm or v is None:
            continue
        try:
            vx = float(v)
        except (TypeError, ValueError):
            continue
        if not (1.0 < vx < 200.0):
            continue
        buckets[(str(d)[:10], _minute_floor(tm))].append(vx)

    out: list[dict] = []
    for (d, hm), xs in sorted(buckets.items()):
        if not xs:
            continue
        out.append(
            {
                "symbol": "VIXTWN",
                "ts": f"{d} {hm}",
                "open": round(xs[0], 4),
                "high": round(max(xs), 4),
                "low": round(min(xs), 4),
                "close": round(xs[-1], 4),
                "n_ticks": len(xs),
                "source": "finmind",
            }
        )
    return out


def fetch_range(start: str, end: str, *, chunk_days: int = 7, sleep_s: float = 0.35) -> list[dict]:
    """Fetch TaiwanOptionVix in week chunks; return raw ticks."""
    d0 = dt.date.fromisoformat(start)
    d1 = dt.date.fromisoformat(end)
    cur = d0
    all_ticks: list[dict] = []
    while cur <= d1:
        e = min(cur + dt.timedelta(days=chunk_days - 1), d1)
        js = fetch_finmind_json(
            {
                "dataset": "TaiwanOptionVix",
                "start_date": cur.isoformat(),
                "end_date": e.isoformat(),
            }
        )
        data = js.get("data") or []
        all_ticks.extend(data)
        print(f"  fetch {cur}→{e}: ticks={len(data)}", flush=True)
        cur = e + dt.timedelta(days=1)
        if cur <= d1:
            time.sleep(sleep_s)
    return all_ticks


def default_start() -> str:
    return "2026-03-02"


def default_end() -> str:
    return dt.date.today().isoformat()


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync VIXTWN 1m bars from TaiwanOptionVix")
    ap.add_argument("--start", default=default_start())
    ap.add_argument("--end", default=default_end())
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--json-out",
        default="",
        help="Also write day→bars JSON cache (research)",
    )
    A = ap.parse_args()

    print(f"VIXTWN 1m sync {A.start}→{A.end} (day session 09:00–13:45 only)", flush=True)
    ticks = fetch_range(A.start, A.end)
    bars = ticks_to_1m(ticks)
    print(f"aggregated bars={len(bars)} from ticks={len(ticks)}", flush=True)

    by_day: dict[str, list[dict]] = defaultdict(list)
    for b in bars:
        by_day[b["ts"][:10]].append(
            {
                "ts": b["ts"],
                "o": b["open"],
                "h": b["high"],
                "l": b["low"],
                "c": b["close"],
                "n": b["n_ticks"],
            }
        )
    print(f"days={len(by_day)} bars/day≈{len(bars) / max(1, len(by_day)):.0f}", flush=True)

    # Sanity vs daily finmind close (last 1m close ≈ daily close)
    if not A.dry_run or True:
        try:
            con = sqlite3.connect(
                f"file:{Path.home() / 'goldenstocks-data/data/stocks.db'}?mode=ro",
                uri=True,
            )
            daily = {
                d: float(c)
                for d, c in con.execute(
                    "SELECT date, close FROM market_vix_daily "
                    "WHERE symbol='VIXTWN' AND source='finmind'"
                )
            }
            con.close()
            diffs = []
            for d, xs in by_day.items():
                if d not in daily or not xs:
                    continue
                diffs.append(abs(xs[-1]["c"] - daily[d]))
            if diffs:
                print(
                    f"vs daily finmind close: n={len(diffs)} "
                    f"mae={sum(diffs)/len(diffs):.3f} max={max(diffs):.3f}",
                    flush=True,
                )
        except Exception as e:
            print(f"(skip daily check: {e})", flush=True)

    synced = utc_now_iso()
    if not A.dry_run:
        conn = connect()
        conn.executescript(_CREATE)
        payload = [{**b, "synced_at": synced} for b in bars]
        conn.executemany(_UPSERT, payload)
        conn.commit()
        print(f"wrote market_vix_1m rows={len(payload)}", flush=True)
        conn.close()
    else:
        print("dry-run: skip DB write", flush=True)

    if A.json_out:
        out = Path(A.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "title": "VIXTWN 1m OHLC",
            "source": "FinMind TaiwanOptionVix → 1m aggregate",
            "session": "day 09:00–13:45 (no night)",
            "start": A.start,
            "end": A.end,
            "n_days": len(by_day),
            "n_bars": len(bars),
            "synced_at": synced,
            "days": dict(sorted(by_day.items())),
        }
        out.write_text(json.dumps(payload, ensure_ascii=False))
        print(f"wrote JSON cache {out} ({out.stat().st_size // 1024} KB)", flush=True)

    print("DONE", flush=True)


if __name__ == "__main__":
    main()
