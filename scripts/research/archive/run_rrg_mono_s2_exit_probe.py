#!/usr/bin/env python3
"""RRG mono hold7 · S2 rotation-exit mirror probe (Phase 1 leg-level).

用法：
  PYTHONPATH=src python scripts/run_rrg_mono_s2_exit_probe.py
  PYTHONPATH=src python scripts/run_rrg_mono_s2_exit_probe.py \\
    --date-start 2025-01-02 --date-end 2026-06-30 --n-slots 3
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.rrg_mono_s2_exit_probe import (  # noqa: E402
    render_rrg_mono_s2_exit_probe_md,
    run_rrg_mono_s2_exit_probe,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RRG mono hold7 · S2 exit mirror probe (Phase 1)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2025-01-02")
    parser.add_argument("--date-end", default="2026-06-30")
    parser.add_argument("--n-slots", type=int, default=3)
    parser.add_argument("--min-quad-exits", type=int, default=5)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_rrg_mono_s2_exit_probe(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            n_slots=args.n_slots,
            min_quad_exits=args.min_quad_exits,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_rrg_mono_s2_exit_probe.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_rrg_mono_s2_exit_probe_md(payload), encoding="utf-8")

    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    for vid in ("M0", "M1", "M2"):
        v = payload["variants"][vid]
        print(
            f"  {vid}: n={v.get('n_periods')} mean_excess={v.get('mean_excess_pct')}% "
            f"quad_exits={v.get('quad_exits', '—')}"
        )
    print(f"  M2 vs M0: {payload['deltas_pp'].get('M2_vs_M0')} pp")
    print(f"  recommend_phase2: {payload.get('recommend_phase2')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
