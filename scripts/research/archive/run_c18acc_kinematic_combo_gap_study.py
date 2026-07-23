#!/usr/bin/env python3
"""C18acc · R2+R7 layered combo + MV gap expansion timing (from timeline cache).

用法：
  PYTHONPATH=src python3 scripts/research/archive/run_c18acc_kinematic_combo_gap_study.py \\
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
from research.backtest.archive.c18acc_kinematic_combo_gap_study import (  # noqa: E402
    load_kinematic_cache,
    render_c18acc_kinematic_combo_gap_study_md,
    run_c18acc_kinematic_combo_gap_study,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc · R2+R7 combo + gap expansion study")
    parser.add_argument(
        "--from-cache",
        type=Path,
        default=RESEARCH_RRG / "20260709_c18acc_kinematic_timeline_n99.json",
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    if not args.from_cache.is_file():
        raise SystemExit(f"cache not found: {args.from_cache}")

    cache = load_kinematic_cache(args.from_cache)
    payload = run_c18acc_kinematic_combo_gap_study(cache)

    stamp = date.today().strftime("%Y%m%d")
    n_slots = cache.get("n_slots", 99)
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_kinematic_combo_gap_n{n_slots}.json"
    out_md = out_json.with_suffix(".md")
    export = {k: v for k, v in payload.items() if k not in ("gap_expansion",)}
    gap = payload.get("gap_expansion") or {}
    export["gap_expansion"] = {
        k: v for k, v in gap.items() if k != "all_rows"
    }
    export["gap_expansion"]["n_sweep_rows"] = len(gap.get("all_rows") or [])
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_kinematic_combo_gap_study_md(payload), encoding="utf-8")

    best = (payload.get("layer_combo") or {}).get("layer_summaries") or []
    best_layer = max(
        (s for s in best if s.get("variant_id") != "hold"),
        key=lambda s: float(s.get("mean_all_legs_ret_pct") or 0),
        default={},
    )
    top_gap = (payload.get("gap_expansion") or {}).get("top_portfolio") or [{}]
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print(
        f"  legs={payload['n_legs']} best_layer={best_layer.get('variant_id')} "
        f"mean={best_layer.get('mean_all_legs_ret_pct')} "
        f"best_gap={top_gap[0].get('gap_pair')}_{top_gap[0].get('gap_mode')}"
        f">={top_gap[0].get('threshold')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
