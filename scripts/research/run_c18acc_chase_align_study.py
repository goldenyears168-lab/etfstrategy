#!/usr/bin/env python3
"""C18acc · chase/extension entry gate + confirm/S2 alignment study.

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/run_c18acc_chase_align_study.py
  PYTHONPATH=src .venv/bin/python scripts/research/run_c18acc_chase_align_study.py --g3-r5
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
from research.backtest.c18acc_chase_align_study import (  # noqa: E402
    DEFAULT_GATE_CACHE,
    render_chase_align_md,
    render_r5_12_g3_md,
    run_chase_align_study,
    run_r5_12_g3_study,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C18acc chase gate + confirm/S2 alignment")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-01-02")
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--is-end", default="2025-12-31")
    ap.add_argument("--gate-cache", type=Path, default=Path(DEFAULT_GATE_CACHE))
    ap.add_argument("--skip-exit", action="store_true")
    ap.add_argument("--skip-confirm", action="store_true")
    ap.add_argument(
        "--g3-r5",
        action="store_true",
        help="G_R5_12 G3 breadth stratification + R5 sensitivity deep dive",
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        if args.g3_r5:
            payload = run_r5_12_g3_study(
                conn,
                date_start=args.date_start,
                date_end=args.date_end,
                is_end=args.is_end,
                gate_cache_path=args.gate_cache,
            )
            stamp = date.today().strftime("%Y%m%d")
            out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_r5_12_g3.json"
            if args.out and str(args.out).endswith(".md"):
                out_json = Path(args.out).with_suffix(".json")
            out_md = out_json.with_suffix(".md")
            out_json.parent.mkdir(parents=True, exist_ok=True)
            out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            out_md.write_text(render_r5_12_g3_md(payload), encoding="utf-8")
            print(payload.get("decision", {}).get("summary", ""))
            print(f"Wrote {out_json}\nWrote {out_md}")
            return 0

        payload = run_chase_align_study(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            gate_cache_path=args.gate_cache,
            run_exit_variants=not args.skip_exit,
            run_confirm_variant=not args.skip_confirm,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_chase_align.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_chase_align_md(payload), encoding="utf-8")
    print(payload.get("verdict", {}).get("summary", ""))
    print(f"Wrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
