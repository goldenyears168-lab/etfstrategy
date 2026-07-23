#!/usr/bin/env python3
"""ABC v3+f1 · hold-period price peak · WMA morphology + exit-rule study.

  PYTHONPATH=src python3 scripts/research/archive/run_abc_v3_f1_peak_exit_study.py --long-window
  PYTHONPATH=src python3 scripts/research/archive/run_abc_v3_f1_peak_exit_study.py --intraday-tail-days 90
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.abc_v3_f1_peak_exit_study import (  # noqa: E402
    DEFAULT_DATE_START,
    render_abc_v3_f1_peak_exit_study_md,
    run_abc_v3_f1_peak_exit_study,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ABC v3+f1 peak WMA + exit-rule study")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default=DEFAULT_DATE_START)
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--intraday-tail-days", type=int, default=90)
    ap.add_argument("--long-window", action="store_true", help="Full date_start..date_end (not tail)")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    t0 = time.perf_counter()
    conn = connect(args.db)
    try:
        payload = run_abc_v3_f1_peak_exit_study(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            intraday_tail_days=args.intraday_tail_days,
            long_window=args.long_window,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    suffix = "long" if args.long_window else f"{args.intraday_tail_days}d"
    out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_peak_exit_study_{suffix}.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_abc_v3_f1_peak_exit_study_md(payload), encoding="utf-8")

    v = payload.get("verdict") or {}
    s = payload.get("summary") or {}
    pg = payload.get("profit_gated_rules") or []
    print(v.get("summary", ""))
    print(
        f"n={s.get('n')} · MFE={s.get('mean_mfe_pct')}% · giveback={s.get('mean_giveback_pct')}% · "
        f"oracle={s.get('oracle_peak_exit_mean_pct')}%"
    )
    if pg:
        top = pg[0]
        print(
            f"best profit-gated: {top.get('rule_id')} mean={top.get('mean_net_pct')}% "
            f"Δ={top.get('delta_vs_baseline_pp')}pp trigger={top.get('trigger_rate_pct')}%"
        )
    for r in v.get("reasons") or []:
        print(f"  · {r}")
    print(f"\nRuntime: {(time.perf_counter() - t0) / 60:.1f} min")
    print(f"Wrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
