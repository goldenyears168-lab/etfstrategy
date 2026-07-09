#!/usr/bin/env python3
"""RRG lead3 funnel lanes · 3-day hold backtest runner."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_pre_release_funnel_lanes_lead3_3d import (  # noqa: E402
    CONFIG_PATH,
    render_lead3_funnel_markdown,
    run_lead3_funnel_3d_backtest,
    write_lead3_lanes_to_config,
)

REPORT_DIR = ROOT / "reports" / "research" / "rrg"


def main() -> int:
    ap = argparse.ArgumentParser(description="RRG lead3 funnel lane · 3-day hold backtest")
    ap.add_argument("--no-discovery", action="store_true")
    ap.add_argument("--write-lanes", action="store_true", help="Write discovered lanes to YAML SSOT")
    ap.add_argument("--config", type=Path, default=CONFIG_PATH)
    args = ap.parse_args()

    result = run_lead3_funnel_3d_backtest(
        config_path=args.config,
        run_discovery=not args.no_discovery,
    )

    if args.write_lanes:
        write_lead3_lanes_to_config(
            result.get("strict_defs", []),
            result.get("coverage_defs", []),
            args.config,
        )
        print(f"Updated lanes in {args.config}")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = date.today().strftime("%Y%m%d")
    json_path = REPORT_DIR / f"pre_release_funnel_lanes_lead3_3d_{stamp}.json"
    md_path = REPORT_DIR / f"pre_release_funnel_lanes_lead3_3d_{stamp}.md"

    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    md = render_lead3_funnel_markdown(result, cfg)

    payload = {
        "meta": result["meta"],
        "config": result["config"],
        "pool_baseline": result["pool_baseline"],
        "pass_threshold": result.get("pass_threshold"),
        "factor_lift": result.get("factor_lift"),
        "factor_phi_top": result.get("factor_phi_top"),
        "sweep_top_combos": result.get("sweep_top_combos"),
        "strict_lanes": result["strict_lanes"],
        "coverage_lanes": result["coverage_lanes"],
        "flip_leading_3d_by_lane": result["flip_leading_3d_by_lane"],
        "union_strict": result["union_strict"],
        "union_all": result["union_all"],
        "jaccard_matrix": result["jaccard_matrix"],
        "strict_defs": result.get("strict_defs", []),
        "coverage_defs": result.get("coverage_defs", []),
        "hypothesis": result.get("hypothesis"),
        "hypothesis_added_lanes": result.get("hypothesis_added_lanes", []),
        "case_feedback": result.get("case_feedback"),
        "case_signal_facts": result.get("case_signal_facts"),
        "suggested_hypotheses": result.get("case_signal_facts", {}).get("suggested_hypotheses"),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(md, encoding="utf-8")

    print(md)
    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
