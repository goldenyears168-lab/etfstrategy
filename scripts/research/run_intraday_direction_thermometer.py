#!/usr/bin/env python3
"""Run intraday direction thermometer snapshot (research / observe only).

Example:
  PYTHONPATH=src .venv/bin/python scripts/research/run_intraday_direction_thermometer.py \\
    --stocks 2327,8046,3189,6451,2492
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from research.intraday_direction_thermometer import (  # noqa: E402
    Bar,
    ThermoConfig,
    build_snapshot,
    load_bench_daily,
    load_chip_daily,
    load_stock_daily,
)

TZ = timezone(timedelta(hours=8))
YAHOO_MAP = {
    "IX0001": "^TWII",
    "TAIEX": "^TWII",
}


def _yahoo_5m(stock_id: str) -> list[Bar]:
    sym = YAHOO_MAP.get(stock_id, f"{stock_id}.TW")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=5m&range=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.load(resp)
    result = payload["chart"]["result"][0]
    ts = result["timestamp"]
    q = result["indicators"]["quote"][0]
    out: list[Bar] = []
    for i, t in enumerate(ts):
        c = q["close"][i]
        if c is None:
            continue
        dt = datetime.fromtimestamp(t, TZ)
        if dt.hour < 9 or (dt.hour == 13 and dt.minute > 25):
            continue
        out.append(
            Bar(
                ts=dt,
                open=float(q["open"][i]),
                high=float(q["high"][i]),
                low=float(q["low"][i]),
                close=float(c),
                volume=float(q["volume"][i] or 0),
            )
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--stocks",
        default="2327,8046,3189,6451,2492",
        help="comma-separated stock ids",
    )
    ap.add_argument("--db", default=str(ROOT / "data" / "stocks.db"))
    ap.add_argument("--open-guard-bars", type=int, default=6)
    ap.add_argument("--swing-ready-bars", type=int, default=12)
    ap.add_argument("--swing-lookback-bars", type=int, default=8)
    ap.add_argument(
        "--out",
        default=str(
            ROOT
            / "reports"
            / "research"
            / "intraday_direction_thermometer"
            / "latest_snapshot.json"
        ),
    )
    args = ap.parse_args()
    cfg = ThermoConfig(
        open_guard_bars=args.open_guard_bars,
        swing_ready_bars=args.swing_ready_bars,
        swing_lookback_bars=args.swing_lookback_bars,
    )
    conn = sqlite3.connect(args.db)
    bench = load_bench_daily(conn)
    snapshots = []
    for sid in [s.strip() for s in args.stocks.split(",") if s.strip()]:
        bars = _yahoo_5m(sid)
        daily = load_stock_daily(conn, sid)
        chips = load_chip_daily(conn, sid)
        snap = build_snapshot(
            stock_id=sid,
            bars_5m=bars,
            stock_daily=daily,
            bench_daily=bench,
            chip_daily=chips,
            cfg=cfg,
        )
        snapshots.append(snap)
        short = snap["intraday_short"]
        swing = snap["swing_1h"]
        trend = snap["trend_3d"]
        print(f"\n=== {sid} ===")
        print(f"hint: {snap['combined_hint']}")
        print(
            f"短線: temp={short['temp']} {short['label']} | {short['action']}"
        )
        print(f"     {short['reason']}")
        print(
            f"1h:   ready={swing['ready']} temp={swing['temp']} | {swing['action']}"
        )
        print(f"三日: ready={trend['ready']} temp={trend['temp']} | {trend['action']}")
        print(f"     {trend['reason']}")
        meta = trend.get("meta") or {}
        if meta:
            print(
                f"     r1={meta.get('r1_pct')}% r3={meta.get('r3_pct')}% "
                f"rel={meta.get('rel_vs_bench_3d_pp')}pp chip={meta.get('chip')}"
            )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "config": snapshots[0]["config"] if snapshots else {},
        "snapshots": snapshots,
        "note": "Research only · observe · not Order live",
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nwrote {out_path}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
