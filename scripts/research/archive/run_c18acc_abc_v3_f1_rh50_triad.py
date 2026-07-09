#!/usr/bin/env python3
"""三者比較 · C18acc vs ABC v3+f1 vs ABC v3+f1+RH50（同契約 · 5m 政策 · 不重建 1m panel）。

  PYTHONPATH=src python3 scripts/run_c18acc_abc_v3_f1_rh50_triad.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.c18acc_abc_v3_f1_rh50_triad import (  # noqa: E402
    build_triad_comparison,
    load_json,
    render_triad_md,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

DEFAULT_COMPARISON = RESEARCH_RRG / "20260708_c18acc_abc_v3_f1_comparison.json"
DEFAULT_RH50_GATE = RESEARCH_RRG / "20260708_abc_v3_f1_rh50_gate.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C18acc / ABC f1 / ABC f1+RH50 triad")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--comparison", type=Path, default=DEFAULT_COMPARISON)
    ap.add_argument("--rh50-gate", type=Path, default=DEFAULT_RH50_GATE)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: db not found: {args.db}", file=sys.stderr)
        return 1
    if not args.comparison.exists():
        print(f"BLOCKER: comparison JSON not found: {args.comparison}", file=sys.stderr)
        return 1

    t0 = time.perf_counter()
    comparison = load_json(args.comparison)
    rh50_gate = load_json(args.rh50_gate) if args.rh50_gate.exists() else None
    if rh50_gate is None:
        print(f"WARN: RH50 gate JSON missing · triad without official ref: {args.rh50_gate}")

    conn = connect(args.db)
    try:
        payload = build_triad_comparison(
            conn,
            comparison_payload=comparison,
            rh50_gate_payload=rh50_gate,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_abc_v3_f1_rh50_triad.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_triad_md(payload), encoding="utf-8")

    cmp_ = payload.get("comparison") or {}
    print(
        f"Window {payload.get('date_start')}..{payload.get('date_end')} · "
        f"winner_daily={cmp_.get('winner_daily')}"
    )
    for row in cmp_.get("rank_by_mean_daily") or []:
        print(f"  #{row['rank']} {row['strategy_id']}: daily={row['mean_daily_return_pct']}%")
    print(f"\nWrote {out_json}\nWrote {out_md}")
    print(f"Elapsed: {(time.perf_counter() - t0):.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
