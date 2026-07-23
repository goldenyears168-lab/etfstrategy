#!/usr/bin/env python3
"""Build CORE16 event JSON cache (branch-by-branch, resumable)."""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts" / "research")]
spec = importlib.util.spec_from_file_location(
    "mvp", ROOT / "scripts/research/run_abc_rolling_top10_l1h7_mvp.py"
)
mvp = importlib.util.module_from_spec(spec)
sys.modules["mvp"] = mvp
assert spec.loader is not None
spec.loader.exec_module(mvp)

CORE16 = [
    "9875", "918X", "9661", "981j", "779Z", "962A", "585b", "7008",
    "779D", "980h", "989g", "700F", "5389", "9217", "5383", "9238",
]
OUT = ROOT / "data/scratch/overnight_cache"
OUT.mkdir(parents=True, exist_ok=True)


def main() -> int:
    db = ROOT / "data/scratch/rolling_mvp_snapshot.db"
    conn = mvp.connect_readonly(db)
    cal = mvp.load_calendar(conn, "2024-07-01", "2026-07-17")
    (OUT / "calendar.json").write_text(json.dumps(cal), encoding="utf-8")
    print(f"calendar {len(cal)}", flush=True)
    events: dict = {}
    for i, bid in enumerate(CORE16):
        path = OUT / f"events_{bid}.json"
        if path.exists():
            events[bid] = json.loads(path.read_text(encoding="utf-8"))
            print(f"[{i+1}/16] {bid} cached {len(events[bid])}", flush=True)
            continue
        t0 = time.time()
        tb = mvp.load_top_buys(conn, [bid], cal[0], cal[-1])
        dated = tb.get(bid, {})
        rows = [
            {
                "trade_date": d,
                "stock_id": dated[d].stock_id,
                "buy_amt": dated[d].buy_amt,
                "buy_share": dated[d].buy_share,
            }
            for d in sorted(dated)
        ]
        path.write_text(json.dumps(rows), encoding="utf-8")
        events[bid] = rows
        print(f"[{i+1}/16] {bid} days={len(rows)} {time.time()-t0:.1f}s", flush=True)
    conn.close()
    (OUT / "events_all.json").write_text(json.dumps(events), encoding="utf-8")
    print("WROTE events_all", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
