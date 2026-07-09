#!/usr/bin/env python3
"""WMA2 RV/MV sweet-spot grid · structural W20+W5 high.

  PYTHONPATH=src python3 scripts/run_w2_rv_mv_sweep.py
  PYTHONPATH=src python3 scripts/run_w2_rv_mv_sweep.py --min-n 25
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
from research.backtest.triple_wma_pullback_sweep import (  # noqa: E402
    DEFAULT_W2_MV_MIN_GRID,
    DEFAULT_W2_RV_MAX_GRID,
    DEFAULT_W2_RV_MIN_GRID,
    render_w2_rv_mv_sweep_md,
    run_w2_rv_mv_sweep,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _parse_optional_floats(raw: str) -> tuple[float | None, ...]:
    out: list[float | None] = []
    for x in raw.split(","):
        x = x.strip()
        if not x or x.lower() in {"none", "-"}:
            out.append(None)
        else:
            out.append(float(x))
    return tuple(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WMA2 RV/MV sweet-spot sweep")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-01-01")
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--intraday-tail-days", type=int, default=60)
    ap.add_argument("--min-n", type=int, default=25)
    ap.add_argument("--rv-max", default=",".join(str(x) for x in DEFAULT_W2_RV_MAX_GRID))
    ap.add_argument("--rv-min", default=",".join("none" if x is None else str(x) for x in DEFAULT_W2_RV_MIN_GRID))
    ap.add_argument("--mv-min", default=",".join(str(x) for x in DEFAULT_W2_MV_MIN_GRID))
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        payload = run_w2_rv_mv_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            intraday_tail_days=args.intraday_tail_days,
            rv_max_grid=tuple(float(x.strip()) for x in args.rv_max.split(",") if x.strip()),
            rv_min_grid=_parse_optional_floats(args.rv_min),
            mv_min_grid=tuple(float(x.strip()) for x in args.mv_min.split(",") if x.strip()),
            min_n=args.min_n,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_w2_rv_mv_sweep.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_w2_rv_mv_sweep_md(payload), encoding="utf-8")

    verdict = payload.get("verdict") or {}
    print(verdict.get("summary", ""))
    for c in verdict.get("top5") or []:
        lo = c.get("rv_min")
        lo_s = "—" if lo is None else lo
        print(
            f"  · RV[{lo_s},{c.get('rv_max')}) MV>{c.get('mv_min')} "
            f"n={c.get('n')} ret={c.get('mean_ret_3d_net_pct')}% win={c.get('win_rate_pct')}%"
        )
    for r in verdict.get("reasons") or []:
        print(f"  · {r}")
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
