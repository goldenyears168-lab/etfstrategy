#!/usr/bin/env python3
"""Leading Dip · 1m / 1-min poll beat study (Research · Book).

  # pull recent Leading∪bench 1m (+ daily T−1, refresh 5m)
  PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_1m_beat_study.py --backfill

  # short window case compare (default 2026-07-13…15)
  PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_1m_beat_study.py --write

  # threshold backtest · 1m −0.90∩−6 vs 5m frozen cov
  PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_1m_beat_study.py --backtest --write

  # local-dip backtest · depth vs t−K (not yday/open) · deeper thr
  PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_1m_beat_study.py --lookback-backtest --write
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.leading_dip_1m_beat_study import (  # noqa: E402
    BACKTEST_END,
    BACKTEST_OOS_CUT,
    BACKTEST_START,
    BACKTEST_VARIANTS,
    DEFAULT_FOCUS_DAY,
    DEFAULT_WINDOW_END,
    DEFAULT_WINDOW_START,
    LOOKBACK_BACKTEST_VARIANTS,
    backfill_recent_1m,
    coverage_report,
    leading_ids_for_day,
    render_backtest_md,
    render_md,
    run_study,
    run_threshold_backtest,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

DEFAULT_STEM = RESEARCH_RRG / "20260716_leading_dip_1m_beat"
DEFAULT_BT_STEM = RESEARCH_RRG / "20260716_leading_dip_1m_threshold_backtest"
DEFAULT_LB_STEM = RESEARCH_RRG / "20260716_leading_dip_1m_lookback_backtest"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Leading Dip 1m beat study (research)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--focus-day", default=DEFAULT_FOCUS_DAY)
    ap.add_argument("--minute-hi", default=None, help="HH:MM cut (default: now if focus=today)")
    ap.add_argument("--backfill", action="store_true", help="Yahoo sync 1m (+daily/+5m)")
    ap.add_argument("--backtest", action="store_true", help="Threshold backtest vs 5m cov")
    ap.add_argument(
        "--lookback-backtest",
        action="store_true",
        help="Local-dip backtest: depth vs t-K (not yday/open)",
    )
    ap.add_argument("--oos-cut", default=BACKTEST_OOS_CUT)
    ap.add_argument("--no-daily", action="store_true")
    ap.add_argument("--no-5m", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.12)
    ap.add_argument("--coverage-only", action="store_true")
    ap.add_argument("--write", action="store_true", help="Write JSON+MD under reports/research/rrg/")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 2

    con = connect(args.db)
    try:
        if args.backtest or args.lookback_backtest:
            start = args.start or BACKTEST_START
            end = args.end or BACKTEST_END
            variants = (
                LOOKBACK_BACKTEST_VARIANTS if args.lookback_backtest else BACKTEST_VARIANTS
            )
            out = args.out or (
                DEFAULT_LB_STEM if args.lookback_backtest else DEFAULT_BT_STEM
            )
            kind = "lookback" if args.lookback_backtest else "threshold"
            print(
                f"{kind} backtest · {start}…{end} · oos_cut={args.oos_cut} · "
                f"n_variants={len(variants)}",
                flush=True,
            )
            payload = run_threshold_backtest(
                con,
                start=start,
                end=end,
                oos_cut=args.oos_cut,
                variants=variants,
            )
            if args.lookback_backtest:
                payload["schema"] = "leading_dip_1m_lookback_backtest-v1"
                payload["design"] = (
                    "trigger = lookback_ex(K)=(S/S_{t-K}-1 - B/B_{t-K}-1)*100 ≤ thr; "
                    "not yday close / not open; bench forced 5m; structure unchanged; "
                    "outcome ex3 still vs yday for fair T+3 compare"
                )
            md = render_backtest_md(payload)
            print(md)
            if args.write:
                out.parent.mkdir(parents=True, exist_ok=True)
                json_path = out.with_suffix(".json")
                md_path = out.with_suffix(".md")
                json_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                md_path.write_text(md + "\n", encoding="utf-8")
                print(f"Wrote {json_path}\nWrote {md_path}", flush=True)
            return 0

        start = args.start or DEFAULT_WINDOW_START
        end = args.end or DEFAULT_WINDOW_END
        out = args.out or DEFAULT_STEM

        if args.backfill:
            print(
                f"Backfill Leading∪bench 1m · {start}…{end} "
                f"(sleep={args.sleep}) …",
                flush=True,
            )
            stats = backfill_recent_1m(
                con,
                start=start,
                end=end,
                sleep_sec=args.sleep,
                sync_daily=not args.no_daily,
                sync_5m=not args.no_5m,
            )
            print(json.dumps(stats, ensure_ascii=False, indent=2), flush=True)

        lead = leading_ids_for_day(con, args.focus_day)
        cov = coverage_report(con, start=start, end=end, universe=lead)
        print(
            f"Coverage · leading_n={len(lead)} · days={list((cov.get('days') or {}).keys())}",
            flush=True,
        )
        print(json.dumps(cov, ensure_ascii=False, indent=2), flush=True)
        if args.coverage_only:
            return 0

        payload = run_study(
            con,
            start=start,
            end=end,
            focus_day=args.focus_day,
            minute_hi=args.minute_hi,
        )
        md = render_md(payload)
        print(md)
        if args.write:
            out.parent.mkdir(parents=True, exist_ok=True)
            json_path = out.with_suffix(".json")
            md_path = out.with_suffix(".md")
            json_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            md_path.write_text(md + "\n", encoding="utf-8")
            print(f"Wrote {json_path}\nWrote {md_path}", flush=True)
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
