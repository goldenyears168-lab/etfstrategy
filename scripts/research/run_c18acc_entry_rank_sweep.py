#!/usr/bin/env python3
"""C18acc · entry rank diagnostic + Phase 1 slot re-sim · G3 adoption.

用法：
  PYTHONPATH=src python3 scripts/research/run_c18acc_entry_rank_sweep.py --variants R0,R2,R4
  PYTHONPATH=src python3 scripts/research/run_c18acc_entry_rank_sweep.py --g3-adoption
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
from research.backtest.c18acc_entry_rank_study import (  # noqa: E402
    PHASE1_VARIANTS,
    render_entry_rank_study_md,
    render_r2_adoption_draft_md,
    run_entry_rank_g3_adoption,
    run_entry_rank_sweep,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

DEFAULT_GATE = (
    RESEARCH_RRG
    / "20260711_c18acc_avoid_mixed_gate_live_poll5m_2025-01-02_2026-07-09.json"
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C18acc entry rank Phase 0–1 / G3 adoption")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2024-01-02")
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--is-end", default="2025-12-31")
    ap.add_argument("--confirm-bars", type=int, default=2)
    ap.add_argument("--n-slots", type=int, default=3)
    ap.add_argument("--gate-cache", type=Path, default=DEFAULT_GATE)
    ap.add_argument(
        "--variants",
        default="R0,R2,R3,R4",
        help="Comma-separated variant ids (default: R0,R2,R3,R4)",
    )
    ap.add_argument(
        "--g3-adoption",
        action="store_true",
        help="Run R0 vs R2 G3 breadth + adoption draft only",
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        if args.g3_adoption:
            start = args.date_start if args.date_start != "2024-01-02" else "2025-01-02"
            payload = run_entry_rank_g3_adoption(
                conn,
                date_start=start,
                date_end=args.date_end,
                is_end=args.is_end,
                confirm_bars=args.confirm_bars,
                n_slots=args.n_slots,
                gate_cache_path=args.gate_cache if args.gate_cache.is_file() else None,
            )
            stamp = date.today().strftime("%Y%m%d")
            out_json = (
                args.out or RESEARCH_RRG / f"{stamp}_c18acc_entry_rank_r2_adoption.json"
            )
            out_md = out_json.with_suffix(".md")
            out_json.parent.mkdir(parents=True, exist_ok=True)
            slim = {
                **payload,
                "g3_full": {
                    k: v
                    for k, v in (payload.get("g3_full") or {}).items()
                    if k not in ("r0", "r2")
                },
                "g3_oos": {
                    k: v
                    for k, v in (payload.get("g3_oos") or {}).items()
                    if k not in ("r0", "r2")
                },
            }
            out_json.write_text(
                json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            out_md.write_text(render_r2_adoption_draft_md(payload), encoding="utf-8")
            print(payload.get("decision", {}).get("summary", ""))
            print(f"Wrote {out_json}\nWrote {out_md}")
            return 0

        want = {x.strip() for x in args.variants.split(",") if x.strip()}
        variants = [v for v in PHASE1_VARIANTS if v["id"] in want]
        if not variants:
            raise SystemExit(f"no variants matched: {args.variants}")

        payload = run_entry_rank_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            confirm_bars=args.confirm_bars,
            n_slots=args.n_slots,
            gate_cache_path=args.gate_cache if args.gate_cache.is_file() else None,
            variants=variants,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_entry_rank_sweep.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    slim = {
        **payload,
        "variants": [
            {k: v for k, v in item.items() if k != "periods"}
            for item in payload.get("variants") or []
        ],
    }
    out_json.write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_entry_rank_study_md(payload), encoding="utf-8")
    print(payload.get("verdict", {}).get("summary", ""))
    print(f"Wrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
