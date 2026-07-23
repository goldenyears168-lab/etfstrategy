#!/usr/bin/env python3
"""C18acc · MV gap OR + RV3 decline interaction sweep (from timeline cache).

用法：
  PYTHONPATH=src python3 scripts/research/archive/run_c18acc_kinematic_gap_rv3_study.py \\
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
from research.backtest.archive.c18acc_kinematic_gap_rv3_study import (  # noqa: E402
    load_kinematic_cache,
    render_c18acc_kinematic_gap_rv3_study_md,
    run_c18acc_kinematic_gap_rv3_study,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc · gap OR + RV3 interaction sweep")
    parser.add_argument(
        "--from-cache",
        type=Path,
        default=RESEARCH_RRG / "20260709_c18acc_kinematic_timeline_n99.json",
    )
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--smoke", action="store_true", help="Reduced grid for quick test")
    args = parser.parse_args(argv)

    if not args.from_cache.is_file():
        raise SystemExit(f"cache not found: {args.from_cache}")

    cache = load_kinematic_cache(args.from_cache)
    payload = run_c18acc_kinematic_gap_rv3_study(cache, full_grid=not args.smoke)

    stamp = date.today().strftime("%Y%m%d")
    n_slots = cache.get("n_slots", 99)
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_kinematic_gap_rv3_n{n_slots}.json"
    out_md = out_json.with_suffix(".md")
    export = {k: v for k, v in payload.items() if k != "all_summaries"}
    export["n_summaries"] = len(payload.get("all_summaries") or [])
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_kinematic_gap_rv3_study_md(payload), encoding="utf-8")

    top = (payload.get("top_variants") or [{}])[0]
    t9 = payload.get("target_9pct_analysis") or {}
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print(
        f"  legs={payload['n_legs']} variants={payload['n_variants']} "
        f"top={top.get('variant_id')} mean={top.get('mean_all_legs_ret_pct')} "
        f"target9_feasible_rate={t9.get('implied_fire_rate_for_9pct')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
