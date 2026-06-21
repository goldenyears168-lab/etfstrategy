#!/usr/bin/env python3
"""
春哥漏斗 L4 参数网格：确保 L7 候选 ≥ min_l7_candidates。

用法：
  PYTHONPATH=src python src/chunge_l4_calibration.py --use-db
"""

from __future__ import annotations

import argparse
import itertools
import sys
from dataclasses import replace
from pathlib import Path

import yaml

from vcp_funnel_screen import ChungeFunnelParams, load_chunge_funnel_params, run_vcp_funnel_screen
from stock_db import DEFAULT_DB_PATH, PROJECT_ROOT, connect
from report_paths import REPORTS_RESEARCH

DEFAULT_OUTPUT = REPORTS_RESEARCH / "chunge_l4_calibration.md"
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "chunge_funnel.yaml"

CHUNGE_L4_GRID: dict[str, tuple] = {
    "t1_depth_max": (60.0, 80.0, 100.0),
    "contraction_ratio": (0.85, 0.90, 0.95),
    "lookback_days": (90, 120),
}


def _grid_combos() -> list[dict]:
    keys = list(CHUNGE_L4_GRID.keys())
    values = [CHUNGE_L4_GRID[k] for k in keys]
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]


def search_l4_params(
    conn,
    base: ChungeFunnelParams,
    *,
    min_l7: int,
) -> tuple[ChungeFunnelParams | None, list[dict]]:
    rows: list[dict] = []
    best: ChungeFunnelParams | None = None
    best_l7 = -1
    for combo in _grid_combos():
        trial = replace(base, **combo)
        _, finalists, layer_counts, _ = run_vcp_funnel_screen(conn, params=trial)
        l7 = len(finalists)
        rows.append({**combo, "l7_count": l7, "l4_count": layer_counts.get("L4", 0)})
        if l7 >= min_l7 and l7 > best_l7:
            best = trial
            best_l7 = l7
        elif best is None and l7 > best_l7:
            best = trial
            best_l7 = l7
    rows.sort(key=lambda r: (-int(r["l7_count"]), -int(r["l4_count"])))
    return best, rows


def build_report(
    *,
    base: ChungeFunnelParams,
    best: ChungeFunnelParams | None,
    rows: list[dict],
    min_l7: int,
) -> str:
    lines = [
        "# Chunge funnel L4 calibration",
        "",
        f"- min_l7_candidates: **{min_l7}**",
        f"- base config: `{DEFAULT_CONFIG.relative_to(PROJECT_ROOT)}`",
        "",
        "| lookback | t1_depth_max | contraction_ratio | L4 | L7 |",
        "|----------|--------------|-------------------|----|----|",
    ]
    for r in rows[:20]:
        lines.append(
            f"| {r['lookback_days']} | {r['t1_depth_max']} | {r['contraction_ratio']} "
            f"| {r['l4_count']} | {r['l7_count']} |"
        )
    if best:
        lines.extend(
            [
                "",
                "## Recommended L4 params",
                "",
                f"- lookback_days: {best.lookback_days}",
                f"- t1_depth_max: {best.t1_depth_max}",
                f"- contraction_ratio: {best.contraction_ratio}",
                f"- L7 at calibration: {next(r['l7_count'] for r in rows if r['lookback_days'] == best.lookback_days and r['t1_depth_max'] == best.t1_depth_max and r['contraction_ratio'] == best.contraction_ratio)}",
            ]
        )
    return "\n".join(lines) + "\n"


def write_config(path: Path, params: ChungeFunnelParams, min_l7: int) -> None:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw["params"] = {
        "lookback_days": params.lookback_days,
        "min_contractions": params.min_contractions,
        "t1_depth_min": params.t1_depth_min,
        "t1_depth_max": params.t1_depth_max,
        "contraction_ratio": params.contraction_ratio,
    }
    raw["min_l7_candidates"] = min_l7
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="春哥漏斗 L4 grid calibration")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--use-db",
        action="store_true",
        help="兼容其他脚本；本模块始终读 --db（默认 data/stocks.db）",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--write-config", action="store_true", help="写回 config/chunge_funnel.yaml")
    args = parser.parse_args()

    base = load_chunge_funnel_params(args.config)
    conn = connect(args.db)
    try:
        best, rows = search_l4_params(conn, base, min_l7=base.min_l7_candidates)
    finally:
        conn.close()

    md = build_report(base=base, best=best, rows=rows, min_l7=base.min_l7_candidates)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(md, encoding="utf-8")

    if best and args.write_config:
        write_config(args.config, best, base.min_l7_candidates)

    top_l7 = rows[0]["l7_count"] if rows else 0
    print(f"  best L7={top_l7} (min={base.min_l7_candidates}) → {args.output.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
