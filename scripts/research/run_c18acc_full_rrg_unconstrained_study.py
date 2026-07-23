#!/usr/bin/env python3
"""C18acc · full_rrg vs PIT fresh · unconstrained all-signals event study.

用法：
  PYTHONPATH=src python scripts/research/run_c18acc_full_rrg_unconstrained_study.py
  PYTHONPATH=src python scripts/research/run_c18acc_full_rrg_unconstrained_study.py \\
    --date-start 2025-01-02 --date-end 2026-07-09 \\
    --gate-cache reports/research/rrg/20260711_c18acc_avoid_mixed_gate_live_poll5m_2025-01-02_2026-07-09.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.c18acc_full_rrg_unconstrained_study import (  # noqa: E402
    DEFAULT_GATE_CACHE,
    render_c18acc_full_rrg_unconstrained_md,
    run_c18acc_full_rrg_unconstrained_study,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

DEFAULT_GATE = RESEARCH_RRG / Path(DEFAULT_GATE_CACHE).name


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="C18acc full_rrg vs PIT fresh · unconstrained event study"
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-01-02")
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--is-end", default="2025-12-31")
    ap.add_argument("--hold-days", type=int, default=5)
    ap.add_argument("--entry-minute", default="09:35")
    ap.add_argument("--confirm-bars", type=int, default=2)
    ap.add_argument("--gate-cache", type=Path, default=DEFAULT_GATE)
    ap.add_argument("--no-gate", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    gate_path = None if args.no_gate else (str(args.gate_cache) if args.gate_cache.is_file() else None)

    conn = connect(args.db)
    try:
        payload = run_c18acc_full_rrg_unconstrained_study(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            hold_days=args.hold_days,
            entry_minute=args.entry_minute,
            confirm_bars=args.confirm_bars,
            gate_cache_path=gate_path,
            avoid_spread_mixed=not args.no_gate,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_full_rrg_unconstrained.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_full_rrg_unconstrained_md(payload), encoding="utf-8")
    cmp_ = payload.get("comparison") or {}
    print(
        f"V0 legs={cmp_.get('v0_n_legs')} V1 legs={cmp_.get('v1_n_legs')} "
        f"Δexcess={cmp_.get('v1_vs_v0_full_delta_excess_pp')}pp"
    )
    print(f"Wrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
