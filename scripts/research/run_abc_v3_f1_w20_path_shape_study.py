#!/usr/bin/env python3
"""ABC v3+F1 · W20 T-2→T-1→T0 path shape observation (MV / RV).

  PYTHONPATH=src python3 scripts/research/run_abc_v3_f1_w20_path_shape_study.py \
    --legs-cache reports/research/rrg/20260709_abc_f1_legs_5d_full.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
assert (ROOT / "src").exists(), f"unexpected ROOT resolution: {ROOT}"

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.abc_v3_f1_w20_path_shape_study import (  # noqa: E402
    render_spread_dist_closeout_md,
    render_w20_path_shape_md,
    run_spread_dist_depth_closeout,
    run_w20_path_shape_study,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _load_legs(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("legs"), list):
        return raw["legs"]
    raise ValueError(f"unexpected legs cache shape: {path}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ABC v3+F1 W20 path / spread_dist closeout")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument(
        "--legs-cache",
        type=Path,
        default=RESEARCH_RRG / "20260709_abc_f1_legs_5d_full.json",
    )
    ap.add_argument(
        "--mode",
        choices=("path", "spread_dist", "closeout"),
        default="closeout",
        help="path=W20 T-2/T-1/T0; spread_dist/closeout=final entry observation",
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.legs_cache.exists():
        print(f"BLOCKER: legs cache not found: {args.legs_cache}", file=sys.stderr)
        return 1

    legs = _load_legs(args.legs_cache)
    src = (
        str(args.legs_cache.relative_to(ROOT))
        if args.legs_cache.is_relative_to(ROOT)
        else str(args.legs_cache)
    )
    stamp = date.today().strftime("%Y%m%d")

    if args.mode == "path":
        if not args.db.exists():
            print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
            return 1
        conn = connect(args.db)
        try:
            payload = run_w20_path_shape_study(conn, legs, legs_source=src)
        finally:
            conn.close()
        out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_w20_path_shape.json"
        render = render_w20_path_shape_md
    else:
        payload = run_spread_dist_depth_closeout(legs, legs_source=src)
        out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_entry_spread_dist_closeout.json"
        render = render_spread_dist_closeout_md

    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render(payload), encoding="utf-8")

    print(f"mode={args.mode} annotated={payload.get('n_annotated')} / input={payload.get('n_input_legs')}")
    if args.mode == "path":
        for key in ("mv_study", "rv_study"):
            block = payload.get(key) or {}
            print(f"\n{key}: baseline={block.get('baseline_mean_tp_pct')}%")
            for row in block.get("shapes") or []:
                print(
                    f"  {row.get('shape')}: n={row.get('n')} mean={row.get('mean_pct')} "
                    f"Δ={row.get('delta_vs_baseline_pp')}pp"
                )
    else:
        co = payload.get("closeout") or {}
        print(f"baseline={payload.get('baseline_mean_tp_pct')}%")
        print(f"corr(spread_dist,ret)={payload.get('corr_spread_dist_vs_ret')}")
        for row in payload.get("terciles") or []:
            print(
                f"  {row.get('id')}: n={row.get('n')} mean={row.get('mean_pct')} "
                f"Δ={row.get('delta_vs_baseline_pp')}pp"
            )
        print(
            f"verdict={co.get('verdict')} deep_lift={co.get('deep_lift_pp')}pp "
            f"passes_gate={co.get('spread_dist_passes_overlay_gate')}"
        )
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
