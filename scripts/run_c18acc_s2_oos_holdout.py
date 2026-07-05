#!/usr/bin/env python3
"""C18acc S2 selective force-exit · IS/OOS holdout vs S0."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.c18acc_s2_oos_holdout import (  # noqa: E402
    IS_END_DEFAULT,
    IS_END_H2_DEFAULT,
    OOS_H2_START_DEFAULT,
    OOS_START_DEFAULT,
    render_c18acc_s2_oos_holdout_md,
    run_c18acc_s2_oos_holdout,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _db_max_date(conn) -> str:
    from market_benchmark import load_benchmark_close

    bench = load_benchmark_close(conn)
    return str(bench.index.max())[:10]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc S2 OOS holdout")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="auto", help="auto = DB latest")
    parser.add_argument("--is-end", default=IS_END_DEFAULT)
    parser.add_argument("--oos-start", default=OOS_START_DEFAULT)
    parser.add_argument(
        "--oos-mode",
        choices=("ytd", "h2"),
        default="ytd",
        help="ytd: OOS from 2026-01-01; h2: IS through 2026-06-30, OOS from 2026-07-01",
    )
    parser.add_argument(
        "--include-h2-appendix",
        action="store_true",
        help="Also emit oos_h2 block when date_end >= 2026-07-01",
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        date_end = _db_max_date(conn) if args.date_end == "auto" else args.date_end
        is_end = args.is_end
        oos_start = args.oos_start
        if args.oos_mode == "h2":
            is_end = IS_END_H2_DEFAULT
            oos_start = OOS_H2_START_DEFAULT
        include_h2 = args.include_h2_appendix and args.oos_mode == "ytd"
        payload = run_c18acc_s2_oos_holdout(
            conn,
            date_start=args.date_start,
            date_end=date_end,
            is_end=is_end,
            oos_start=oos_start,
            include_h2_oos=include_h2,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_s2_oos_holdout.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_s2_oos_holdout_md(payload), encoding="utf-8")
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")

    oos_delta = (payload.get("oos") or {}).get("delta_s2_vs_s0") or {}
    grad = payload.get("graduation") or {}
    print(
        f"OOS Δ excess={oos_delta.get('mean_excess_pp')}pp · "
        f"Δ CAGR={oos_delta.get('cagr_pp')}pp · "
        f"adopt={grad.get('recommend_adopt')}"
    )
    return 0 if grad.get("recommend_adopt") else 1


if __name__ == "__main__":
    raise SystemExit(main())
