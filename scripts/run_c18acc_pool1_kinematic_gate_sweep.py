#!/usr/bin/env python3
"""C18acc · POOL1 kinematic gate sweep · phase 1 hard gates · phase 2 soft variants."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.c18acc_pool1_kinematic_gate_sweep import (  # noqa: E402
    render_pool1_kinematic_gate_md,
    run_pool1_kinematic_gate_sweep,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2024-01-01")
    ap.add_argument("--date-end", default="2026-06-30")
    ap.add_argument("--is-end", default="2025-12-31")
    ap.add_argument("--oos-start", default="2026-01-01")
    ap.add_argument("--phase2", action="store_true", help="Run phase 2 soft-rank / streak / tiebreak sweep")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    phase = 2 if args.phase2 else 1
    conn = connect(args.db)
    try:
        payload = run_pool1_kinematic_gate_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            oos_start=args.oos_start,
            phase=phase,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    suffix = "_v2" if phase == 2 else ""
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_pool1_kinematic_gate{suffix}.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_pool1_kinematic_gate_md(payload), encoding="utf-8")

    v = payload.get("verdict", {})
    print(v.get("summary", ""))
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
