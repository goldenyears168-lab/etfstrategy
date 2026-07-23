#!/usr/bin/env python3
"""ABC v3+F1 · prior-5d range × day-up (inductive → deductive / closeout).

  # Phase 1 (explore + deductive)
  PYTHONPATH=src python3 scripts/research/run_abc_v3_f1_range_dayup_study.py

  # Phase 2 closeout (de-fat; goal-aligned kill/keep)
  PYTHONPATH=src python3 scripts/research/run_abc_v3_f1_range_dayup_study.py --closeout
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
from research.backtest.abc_v3_f1_range_dayup_study import (  # noqa: E402
    render_range_dayup_closeout_md,
    render_range_dayup_md,
    run_range_dayup_closeout,
    run_range_dayup_study,
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
    ap = argparse.ArgumentParser(description="ABC v3+F1 range5 × day-up study")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument(
        "--legs-cache",
        type=Path,
        default=RESEARCH_RRG / "20260709_abc_f1_legs_5d_full.json",
    )
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument(
        "--closeout",
        action="store_true",
        help="Run H-ENTRY-RANGE-DAYUP-2 de-fat closeout (no new thr search)",
    )
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
        if args.closeout:
            payload = run_range_dayup_closeout(conn, legs, legs_source=src)
        else:
            payload = run_range_dayup_study(conn, legs, legs_source=src)
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    if args.closeout:
        out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_range_dayup_closeout.json"
        out_md = out_json.with_suffix(".md")
        md = render_range_dayup_closeout_md(payload)
    else:
        out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_range_dayup.json"
        out_md = out_json.with_suffix(".md")
        md = render_range_dayup_md(payload)

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(md, encoding="utf-8")

    print(f"annotated={payload.get('n_annotated')} verdict={payload.get('verdict')}")
    if args.closeout:
        print(f"next_step={payload.get('next_step')}")
        for v in payload.get("variants") or []:
            stab = v.get("stability") or {}
            oos = v.get("oos_seg23") or {}
            print(
                f"  {v.get('id')}: n={v.get('n')} Δ={v.get('delta_vs_baseline_pp')} "
                f"drop3={v.get('delta_drop_top3_pp')} medOK={v.get('median_beats_baseline')} "
                f"segs={stab.get('positive_segments')}/3 oosΔ={oos.get('delta_vs_baseline_pp')} "
                f"robust={v.get('passes_robust_closeout')}"
            )
    else:
        ind = payload.get("inductive") or {}
        print(
            f"corr_range5={ind.get('corr_range5_vs_ret')} "
            f"corr_atr={ind.get('corr_range5_atr_vs_ret')} "
            f"corr|up={ind.get('corr_range5_vs_ret_given_day_up')}"
        )
        for v in (payload.get("deductive") or {}).get("variants") or []:
            stab = v.get("stability") or {}
            oos = v.get("oos_seg23") or {}
            print(
                f"  {v.get('id')}: n={v.get('n')} Δ={v.get('delta_vs_baseline_pp')} "
                f"drop3={v.get('delta_drop_top3_pp')} segs={stab.get('positive_segments')}/3 "
                f"oosΔ={oos.get('delta_vs_baseline_pp')} full={v.get('passes_full_protocol')} "
                f"robust={v.get('passes_robust_drop_top3')}"
            )
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
