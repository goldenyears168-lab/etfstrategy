#!/usr/bin/env python3
"""C18acc POOL1+S2 · F1 Friday swap margin parameter sweep.

用法：
  PYTHONPATH=src python3 scripts/run_c18acc_friday_swap_margin_sweep.py
  PYTHONPATH=src python3 scripts/run_c18acc_friday_swap_margin_sweep.py \\
    --margins 0,0.02,0.03,0.04,0.05 --date-start 2020-01-02
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.c18acc_friday_swap_margin_sweep import (  # noqa: E402
    FRIDAY_SWAP_MARGIN_GRID,
    render_c18acc_friday_swap_margin_sweep_md,
    run_c18acc_friday_swap_margin_sweep,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _db_max_date(conn) -> str:
    from market_benchmark import load_benchmark_close

    bench = load_benchmark_close(conn)
    return str(bench.index.max())[:10]


def _parse_margins(raw: str) -> tuple[float, ...]:
    vals = tuple(float(v.strip()) for v in raw.split(",") if v.strip())
    if not vals:
        raise ValueError("margins must not be empty")
    return vals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc F1 Friday swap margin sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2020-01-02")
    parser.add_argument("--date-end", default="auto")
    parser.add_argument("--is-end", default="2025-12-31")
    parser.add_argument("--oos-start", default="2026-01-01")
    parser.add_argument("--n-slots", type=int, default=3)
    parser.add_argument(
        "--margins",
        default=",".join(str(m) for m in FRIDAY_SWAP_MARGIN_GRID),
        help="Comma-separated Friday swap margins (default: preregistered grid)",
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    margins = _parse_margins(args.margins)
    conn = connect(args.db)
    try:
        date_end = _db_max_date(conn) if args.date_end == "auto" else args.date_end
        payload = run_c18acc_friday_swap_margin_sweep(
            conn,
            date_start=args.date_start,
            date_end=date_end,
            is_end=args.is_end,
            oos_start=args.oos_start,
            n_slots=args.n_slots,
            margins=margins,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_friday_swap_margin_sweep.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_friday_swap_margin_sweep_md(payload), encoding="utf-8")

    w0 = payload.get("w0_baseline") or {}
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print(
        f"  W0 FULL excess={(w0.get('FULL') or {}).get('mean_excess_pct')}% "
        f"OOS={(w0.get('OOS') or {}).get('mean_excess_pct')}%"
    )
    best = payload.get("best_passing")
    if best:
        print(
            f"  best={best.get('variant_id')} margin={best.get('friday_swap_margin')} "
            f"FULL Δ={best.get('full_delta_pp')}pp OOS Δ={best.get('oos_delta_pp')}pp"
        )
    else:
        print("  best=none (no variant passed FULL+OOS threshold)")
    for r in (payload.get("grid_ranked") or [])[:5]:
        print(
            f"  {r.get('variant_id')} m={r.get('friday_swap_margin')} "
            f"FULL Δ={r.get('full_delta_pp')} OOS Δ={r.get('oos_delta_pp')} pass={r.get('pass_threshold')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
