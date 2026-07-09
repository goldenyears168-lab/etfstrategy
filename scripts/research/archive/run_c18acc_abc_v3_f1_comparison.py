#!/usr/bin/env python3
"""C18acc vs ABC v3+f1 · 2024+ unified comparison + trade ledger report.

  PYTHONPATH=src python3 scripts/run_c18acc_abc_v3_f1_comparison.py
  PYTHONPATH=src python3 scripts/run_c18acc_abc_v3_f1_comparison.py --date-start 2024-01-01
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
from research.backtest.archive.c18acc_abc_v3_f1_comparison import (  # noqa: E402
    build_c18acc_abc_v3_f1_comparison_bundle,
    enrich_event_level,
    render_c18acc_abc_v3_f1_comparison_md,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C18acc vs ABC v3+f1 comparison (2024+)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2024-01-01")
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument(
        "--render-only",
        type=Path,
        default=None,
        help="Re-render markdown from existing JSON (no DB replay)",
    )
    args = ap.parse_args(argv)

    if args.render_only:
        payload = enrich_event_level(
            json.loads(args.render_only.read_text(encoding="utf-8"))
        )
        out_md = args.render_only.with_suffix(".md")
        out_md.write_text(render_c18acc_abc_v3_f1_comparison_md(payload), encoding="utf-8")
        ev = payload.get("event_level") or {}
        abc_all = ((ev.get("abc_f1") or {}).get("all_candidates")) or {}
        print(f"Re-rendered {out_md}")
        print(
            f"Event-level ABC all={abc_all.get('mean_ret_net_pct')}% n={abc_all.get('n')} · "
            f"champion={((ev.get('champion_pipeline') or {}).get('mean_ret_3d_net_pct'))}%"
        )
        return 0

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    t0 = time.perf_counter()
    conn = connect(args.db)
    try:
        payload = build_c18acc_abc_v3_f1_comparison_bundle(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_abc_v3_f1_comparison.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_abc_v3_f1_comparison_md(payload), encoding="utf-8")

    cmp_ = payload.get("comparison") or {}
    ev = payload.get("event_level") or {}
    abc_all = ((ev.get("abc_f1") or {}).get("all_candidates")) or {}
    print(
        f"Window {payload.get('date_start')} .. {payload.get('date_end')} · "
        f"{payload.get('n_trade_dates')} td · runtime {payload.get('total_runtime_s')}s"
    )
    print(
        f"Event ABC all={abc_all.get('mean_ret_net_pct')}% · "
        f"portfolio daily C18={cmp_.get('c18acc_mean_daily_pct')}% "
        f"ABC={cmp_.get('abc_f1_mean_daily_pct')}%"
    )
    print(
        f"C18acc daily={cmp_.get('c18acc_mean_daily_pct')}% · "
        f"ABC f1 daily={cmp_.get('abc_f1_mean_daily_pct')}% · "
        f"winner={cmp_.get('winner_daily')}"
    )
    print(f"\nWrote {out_json}\nWrote {out_md}")
    print(f"Total elapsed: {(time.perf_counter() - t0) / 60:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
