#!/usr/bin/env python3
"""C18acc · avoid spread_mixed · slot re-sim OOS holdout.

用法：
  PYTHONPATH=src python3 scripts/research/archive/run_c18acc_avoid_mixed_slot_resim.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.c18acc_avoid_mixed_slot_resim import (  # noqa: E402
    render_c18acc_avoid_mixed_slot_resim_md,
    run_c18acc_avoid_mixed_slot_resim,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc avoid spread_mixed slot re-sim")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2025-01-02")
    parser.add_argument("--date-end", default=None)
    parser.add_argument("--n-slots", type=int, default=3)
    parser.add_argument("--is-end", default="2025-12-31")
    parser.add_argument("--oos-start", default="2026-01-01")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--gate-cache", type=Path, default=None, help="Load/save spread gate cache JSON")
    parser.add_argument("--total-capital", type=float, default=100_000.0)
    args = parser.parse_args(argv)

    gate_cache = args.gate_cache
    if gate_cache is None:
        gate_cache = RESEARCH_RRG / "20260711_c18acc_avoid_mixed_gate_cache.json"

    conn = connect(args.db)
    try:
        payload = run_c18acc_avoid_mixed_slot_resim(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            n_slots=args.n_slots,
            is_end=args.is_end,
            oos_start=args.oos_start,
            total_capital=args.total_capital,
            gate_cache_path=str(gate_cache),
            save_gate_cache_path=str(gate_cache),
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_avoid_mixed_slot_resim_nav.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_avoid_mixed_slot_resim_md(payload), encoding="utf-8")

    v = payload["verdict"]
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print(f"  {v.get('leg_summary')}")
    print(f"  {v.get('nav_summary')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
