#!/usr/bin/env python3
"""ABC v3+F1 · gap_w20_w5 跨股票族群深挖（RP-3）.

  PYTHONPATH=src python3 scripts/research/run_abc_v3_f1_gap_stock_clustering.py \
    --date-start 2025-09-01 --date-end 2026-07-01 --hold-days 5
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
from research.backtest.abc_v3_f1_gap_stock_clustering_study import (  # noqa: E402
    render_gap_stock_clustering_md,
    run_gap_stock_clustering_study,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ABC v3+F1 gap_w20_w5 cross-stock clustering study (RP-3)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-09-01")
    ap.add_argument("--date-end", default="2026-07-01")
    ap.add_argument("--hold-days", type=int, default=5)
    ap.add_argument("--baseline-window", type=int, default=60)
    ap.add_argument("--min-trailing-days", type=int, default=10)
    ap.add_argument("--group-min-n", type=int, default=20)
    ap.add_argument("--min-events-per-group", type=int, default=30)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        payload = run_gap_stock_clustering_study(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            hold_days=args.hold_days,
            baseline_window=args.baseline_window,
            min_trailing_days=args.min_trailing_days,
            group_min_n=args.group_min_n,
            min_events_per_group=args.min_events_per_group,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_gap_stock_clustering.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_gap_stock_clustering_md(payload), encoding="utf-8")

    print(f"n_legs={payload.get('n_legs')} n_stocks={payload.get('n_stocks')}")
    deco = payload.get("decomposition_raw") or {}
    for comp in ("between_stock", "within_stock", "first_event_only"):
        d = deco.get(comp) or {}
        print(f"  {comp}: rho={d.get('spearman_rho')} n={d.get('n')} ci={d.get('spearman_ci95')}")
    for feature, spec in (payload.get("group_scans") or {}).items():
        if spec.get("skipped"):
            print(f"  [{feature}] skipped")
            continue
        parts = []
        for label, g in (spec.get("groups") or {}).items():
            raw = next(
                (r for r in g.get("scan") or [] if r.get("factor") == "gap_w20_w5_raw" and not r.get("skipped")),
                None,
            )
            parts.append(f"{label}: n={g['n_legs']} rho={raw.get('spearman_rho') if raw else 'NA'}")
        print(f"  [{feature}] " + " | ".join(parts))
    success = payload.get("success") or {}
    print(f"strict_success={success.get('strict_success')} soft_success={success.get('soft_success')}")
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
