#!/usr/bin/env python3
"""C18acc · hold cap 4/5d × Gap OR + RV3 · target 10% (truncate timeline cache).

用法：
  PYTHONPATH=src python3 scripts/research/archive/run_c18acc_kinematic_hold_cap_study.py \\
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
from research.backtest.archive.c18acc_kinematic_exit_sweep import load_kinematic_cache  # noqa: E402
from research.backtest.archive.c18acc_kinematic_gap_rv3_study import (  # noqa: E402
    render_hold_cap_gap_rv3_study_md,
    run_hold_cap_gap_rv3_study,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc · hold cap 4/5d gap+RV3 study")
    parser.add_argument(
        "--from-cache",
        type=Path,
        default=RESEARCH_RRG / "20260709_c18acc_kinematic_timeline_n99.json",
    )
    parser.add_argument("--target", type=float, default=10.0)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    if not args.from_cache.is_file():
        raise SystemExit(f"cache not found: {args.from_cache}")

    cache = load_kinematic_cache(args.from_cache)
    payload = run_hold_cap_gap_rv3_study(cache, target_pct=args.target)

    stamp = date.today().strftime("%Y%m%d")
    n_slots = cache.get("n_slots", 99)
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_kinematic_hold_cap_n{n_slots}.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_hold_cap_gap_rv3_study_md(payload), encoding="utf-8")

    best = 0.0
    for block in (payload.get("by_hold_days") or {}).values():
        v = float((block.get("best_rule") or {}).get("mean_all_legs_ret_pct") or 0)
        best = max(best, v)
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print(f"  target={args.target}% best_portfolio_mean={best:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
