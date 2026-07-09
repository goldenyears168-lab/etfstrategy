#!/usr/bin/env python3
"""H-XEL-2 verdict · XEL-0 vs XEL-2 · IS + OOS H1 + Regime layer（環境層）分層."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.c18acc_intraday_exit_loop import (  # noqa: E402
    OOS_H1_END_DEFAULT,
    OOS_START_DEFAULT,
    render_h_xel2_verdict_md,
    run_intraday_exit_loop_sweep,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="H-XEL-2 · XEL-0 vs XEL-2 OOS + regime verdict")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-01-01", help="IS start · default 2025")
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--is-end", default="2025-12-31")
    ap.add_argument("--oos-h1-start", default=OOS_START_DEFAULT)
    ap.add_argument("--oos-h1-end", default=OOS_H1_END_DEFAULT)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_intraday_exit_loop_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            oos_h1_start=args.oos_h1_start,
            oos_h1_end=args.oos_h1_end,
            variant_ids=["XEL-0", "XEL-2"],
            window_labels=["IS", "OOS_H1"],
            hxel2_verdict=True,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_hxel2_verdict.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_h_xel2_verdict_md(payload), encoding="utf-8")

    hx = payload.get("hxel2_verdict") or {}
    print(payload.get("verdict", {}).get("summary", ""))
    print(f"H-XEL-2 status: {hx.get('status')} · OOS Δ={hx.get('delta_excess_oos_h1_pp')}pp")
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
