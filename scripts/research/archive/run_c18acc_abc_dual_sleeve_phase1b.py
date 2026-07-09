#!/usr/bin/env python3
"""C18acc × ABC v3+f1 · Phase1b event-observe sleeve NAV."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.c18acc_abc_dual_sleeve_phase1b import (  # noqa: E402
    build_phase1b_from_comparison_json,
    render_c18acc_abc_dual_sleeve_phase1b_md,
    run_c18acc_abc_dual_sleeve_phase1b,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C18acc × ABC dual-sleeve Phase1b event-observe NAV")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2024-01-01")
    ap.add_argument("--date-end", default=None)
    ap.add_argument(
        "--from-comparison",
        type=Path,
        default=None,
        help="C18 NAV from comparison JSON · ABC event sleeve from DB or sidecar",
    )
    ap.add_argument(
        "--abc-periods-json",
        type=Path,
        default=None,
        help="optional ABC candidate periods sidecar (skip panel rebuild)",
    )
    ap.add_argument(
        "--write-abc-periods",
        type=Path,
        default=None,
        help="write ABC candidate periods sidecar after live ABC collection",
    )
    ap.add_argument(
        "--kbar-bar-minutes",
        type=int,
        default=5,
        help="intraday pricing bar size · 5=aggregated 5m K (default) · 0/omit path uses 1m",
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    kbar = int(args.kbar_bar_minutes) if args.kbar_bar_minutes else None
    conn = connect(args.db)
    try:
        if args.from_comparison:
            payload = build_phase1b_from_comparison_json(
                str(args.from_comparison),
                conn,
                abc_periods_json=str(args.abc_periods_json) if args.abc_periods_json else None,
                kbar_bar_minutes=kbar,
            )
        else:
            payload = run_c18acc_abc_dual_sleeve_phase1b(
                conn,
                date_start=args.date_start,
                date_end=args.date_end,
                kbar_bar_minutes=kbar,
            )
    finally:
        conn.close()

    if args.write_abc_periods:
        sidecar = {
            "schema": "abc_f1_candidate_periods-v1",
            "n_candidates": (payload.get("candidate_meta") or {}).get("n_candidates"),
            "periods": payload.get("abc_f1_candidate_periods")
            or (payload.get("candidate_meta") or {}).get("periods")
            or [],
        }
        args.write_abc_periods.parent.mkdir(parents=True, exist_ok=True)
        args.write_abc_periods.write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Wrote ABC periods sidecar {args.write_abc_periods}")

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_abc_dual_sleeve_phase1b.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_abc_dual_sleeve_phase1b_md(payload), encoding="utf-8")

    print(payload.get("verdict", {}).get("summary", ""))
    ev = payload.get("abc_event_observe") or {}
    print(
        f"ABC event sleeve · slots={ev.get('n_slots_auto')} · "
        f"executed={ev.get('n_executed')} · "
        f"mean_3d={((ev.get('event_executed') or {}).get('mean_ret_net_pct'))}% · "
        f"champion={ev.get('champion_reference', {}).get('mean_ret_3d_net_pct')}%"
    )
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
