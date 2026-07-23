#!/usr/bin/env python3
"""C18acc · core + satellite small book · Phase 0/1 study.

用法：
  PYTHONPATH=src python scripts/research/run_c18acc_satellite_slot_study.py
  PYTHONPATH=src python scripts/research/run_c18acc_satellite_slot_study.py \\
    --date-end 2026-07-09 \\
    --gate-cache reports/research/rrg/20260711_c18acc_avoid_mixed_gate_live_poll5m_2025-01-02_2026-07-09.json
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
from research.backtest.c18acc_satellite_slot_study import (  # noqa: E402
    render_c18acc_satellite_slot_md,
    run_c18acc_satellite_slot_study,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

DEFAULT_GATE = (
    RESEARCH_RRG
    / "20260711_c18acc_avoid_mixed_gate_live_poll5m_2025-01-02_2026-07-09.json"
)
DEFAULT_DATE_END = "2026-07-09"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="C18acc core + satellite slot Phase 0/1 study"
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-01-02")
    ap.add_argument("--date-end", default=DEFAULT_DATE_END)
    ap.add_argument("--is-end", default="2025-12-31")
    ap.add_argument("--oos-start", default="2026-01-02")
    ap.add_argument("--gate-cache", type=Path, default=DEFAULT_GATE)
    ap.add_argument("--nav-book", type=float, default=100_000.0)
    ap.add_argument("--skip-phase1", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_c18acc_satellite_slot_study(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            oos_start=args.oos_start,
            gate_cache_path=args.gate_cache if args.gate_cache.is_file() else None,
            nav_book_twd=float(args.nav_book),
            skip_phase1=bool(args.skip_phase1),
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_satellite_slot.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    out_md.write_text(render_c18acc_satellite_slot_md(payload), encoding="utf-8")
    print((payload.get("verdict") or {}).get("summary", ""))
    print(f"Wrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
