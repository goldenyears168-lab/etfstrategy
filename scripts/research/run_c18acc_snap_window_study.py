#!/usr/bin/env python3
"""C18acc · SNAP provisional RRG open-window (12:00 vs 13:20).

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/run_c18acc_snap_window_study.py
  PYTHONPATH=src .venv/bin/python scripts/research/run_c18acc_snap_window_study.py --minutes 13:20,12:00
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
from research.backtest.c18acc_open_timing_study import DEFAULT_GATE_CACHE  # noqa: E402
from research.backtest.c18acc_snap_window_study import (  # noqa: E402
    render_snap_window_md,
    run_snap_window_study,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C18acc SNAP window study")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-01-02")
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--is-end", default="2025-06-30")
    ap.add_argument("--confirm-bars", type=int, default=1)
    ap.add_argument("--n-slots", type=int, default=3)
    ap.add_argument("--gate-cache", type=Path, default=Path(DEFAULT_GATE_CACHE))
    ap.add_argument(
        "--minutes",
        default="13:20,13:00,12:40,12:00",
        help="comma-separated snapshot/fill minutes (always includes 13:20 baseline)",
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    minutes = tuple(m.strip() for m in str(args.minutes).split(",") if m.strip())
    conn = connect(args.db)
    try:
        payload = run_snap_window_study(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            confirm_bars=args.confirm_bars,
            n_slots=args.n_slots,
            gate_cache_path=args.gate_cache if args.gate_cache.is_file() else None,
            minutes=minutes,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_snap_window.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_snap_window_md(payload), encoding="utf-8")
    print((payload.get("verdict") or {}).get("summary", ""))
    print((payload.get("verdict") or {}).get("recommendation", ""))
    print(f"Wrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
