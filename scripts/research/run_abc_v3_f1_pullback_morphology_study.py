#!/usr/bin/env python3
"""ABC v3+F1 · same-day pullback morphology study.

  PYTHONPATH=src python3 scripts/research/run_abc_v3_f1_pullback_morphology_study.py \
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
from research.backtest.abc_v3_f1_pullback_morphology_study import (  # noqa: E402
    render_pullback_morphology_md,
    run_pullback_morphology_study,
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
    ap = argparse.ArgumentParser(description="ABC v3+F1 same-day pullback morphology")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument(
        "--legs-cache",
        type=Path,
        default=RESEARCH_RRG / "20260709_abc_f1_legs_5d_full.json",
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1
    if not args.legs_cache.exists():
        print(f"BLOCKER: legs cache not found: {args.legs_cache}", file=sys.stderr)
        return 1

    legs = _load_legs(args.legs_cache)
    src = (
        str(args.legs_cache.relative_to(ROOT))
        if args.legs_cache.is_relative_to(ROOT)
        else str(args.legs_cache)
    )
    conn = connect(args.db)
    try:
        payload = run_pullback_morphology_study(conn, legs, legs_source=src)
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_pullback_morphology.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_pullback_morphology_md(payload), encoding="utf-8")

    print(f"annotated={payload.get('n_annotated')} / input={payload.get('n_input_legs')}")
    print(f"baseline={payload.get('baseline_mean_tp_pct')}%")
    for row in payload.get("shapes") or []:
        print(
            f"  {row.get('id')}: n={row.get('n')} mean={row.get('mean_pct')} "
            f"Δ={row.get('delta_vs_baseline_pp')}pp pass={row.get('passes_gate')}"
        )
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
