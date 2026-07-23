#!/usr/bin/env python3
"""Buy-radar C0 fresh/tier2 event study (Part B).

用法：
  PYTHONPATH=src python3 scripts/research/run_buy_radar_c0_fresh_tier2_events.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.buy_radar_c0_fresh_tier2_events import (  # noqa: E402
    render_buy_radar_c0_fresh_tier2_events_md,
    run_buy_radar_c0_fresh_tier2_events,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Buy-radar C0 fresh/tier2 events")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-01-02")
    ap.add_argument("--date-end", default="2026-07-16")
    ap.add_argument("--is-end", default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_buy_radar_c0_fresh_tier2_events(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
        )
    finally:
        conn.close()

    # keep sample only in artifact
    payload = {k: v for k, v in payload.items() if k != "events"}

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_buy_radar_c0_fresh_tier2_events.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_buy_radar_c0_fresh_tier2_events_md(payload), encoding="utf-8")
    print((payload.get("verdict") or {}).get("summary", ""))
    print(f"Wrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
