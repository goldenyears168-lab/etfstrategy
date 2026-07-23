#!/usr/bin/env python3
"""C18acc · structural pyramid add study (RP-2 port · H-C18-PYRAMID-1).

  PYTHONPATH=src python3 scripts/research/archive/run_c18acc_structural_pyramid.py \\
    --from-cache reports/research/rrg/20260709_c18acc_kinematic_timeline_n99.json
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
from research.backtest.archive.c18acc_structural_pyramid_study import (  # noqa: E402
    load_kinematic_cache,
    render_c18acc_structural_pyramid_md,
    run_c18acc_structural_pyramid_study,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C18acc structural pyramid add study (RP-2 port)")
    ap.add_argument(
        "--from-cache",
        type=Path,
        default=RESEARCH_RRG / "20260709_c18acc_kinematic_timeline_n99.json",
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--min-trigger-n", type=int, default=30)
    ap.add_argument("--min-oos-trigger-n", type=int, default=15)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.from_cache.is_file():
        print(f"BLOCKER: cache not found: {args.from_cache}", file=sys.stderr)
        return 1
    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    cache = load_kinematic_cache(args.from_cache)
    n_slots = cache.get("n_slots", 99)
    print(
        f"pyramid: C18acc legs={cache.get('n_legs')} n_slots={n_slots} "
        f"({cache.get('date_start')} .. {cache.get('date_end')}) …",
        flush=True,
    )

    conn = connect(args.db)
    try:
        payload = run_c18acc_structural_pyramid_study(
            conn,
            cache,
            min_trigger_n=args.min_trigger_n,
            min_oos_trigger_n=args.min_oos_trigger_n,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_structural_pyramid_n{n_slots}.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_structural_pyramid_md(payload), encoding="utf-8")

    print(f"\nverdict: {payload.get('verdict')} · winner: {payload.get('winner_id')}")
    for c in payload.get("criteria") or []:
        print(f"  [{'PASS' if c.get('passed') else 'FAIL'}] {c.get('criterion')} — {c.get('detail')}")
    print("\nIS conditions:")
    for cid, r in (payload.get("is_results") or {}).items():
        da = r.get("delta_vs_A_pp") or {}
        db = r.get("delta_vs_B_same_legs_pp") or {}
        print(
            f"  {cid}: n={r.get('n_triggered')} ({r.get('trigger_rate_pct')}%) "
            f"blended={r.get('blended_sync_mean_pct')}% legA={r.get('leg1_only_mean_pct')}% "
            f"dA={da.get('mean')}pp(p~{da.get('p_approx')}) dB={db.get('mean')}pp(p~{db.get('p_approx')})"
        )
    print("\nOOS conditions:")
    for cid, r in (payload.get("oos_results") or {}).items():
        da = r.get("delta_vs_A_pp") or {}
        print(
            f"  {cid}: n={r.get('n_triggered')} blended={r.get('blended_sync_mean_pct')}% "
            f"legA={r.get('leg1_only_mean_pct')}% dA={da.get('mean')}pp(p~{da.get('p_approx')})"
        )
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
