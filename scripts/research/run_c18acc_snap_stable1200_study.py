#!/usr/bin/env python3
"""C18acc · SNAP 12:00→12:30 stability vs SNAP@13:20 (expanded watchlist).

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/run_c18acc_snap_stable1200_study.py
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
from research.backtest.c18acc_open_timing_study import DEFAULT_GATE_CACHE  # noqa: E402
from research.backtest.c18acc_snap_stable1200_study import (  # noqa: E402
    render_snap_stable1200_md,
    run_snap_stable1200_study,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C18acc SNAP 12:00–12:30 stability study")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-01-02")
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--is-end", default="2025-06-30")
    ap.add_argument("--confirm-bars", type=int, default=1)
    ap.add_argument("--n-slots", type=int, default=3)
    ap.add_argument("--observe-start", default="12:00")
    ap.add_argument("--observe-end", default="12:30")
    ap.add_argument("--stability-top-n", type=int, default=3)
    ap.add_argument("--gate-cache", type=Path, default=Path(DEFAULT_GATE_CACHE))
    ap.add_argument(
        "--rebuild-gate",
        action="store_true",
        help="Rebuild avoid_mixed gate on current full watchlist (no passthrough)",
    )
    ap.add_argument(
        "--no-passthrough",
        action="store_true",
        help="Do not passthrough unscored fresh names (implied by --rebuild-gate)",
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_snap_stable1200_study(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            confirm_bars=args.confirm_bars,
            n_slots=args.n_slots,
            gate_cache_path=(
                None
                if args.rebuild_gate
                else (args.gate_cache if args.gate_cache.is_file() else None)
            ),
            observe_start=args.observe_start,
            observe_end=args.observe_end,
            stability_top_n=args.stability_top_n,
            rebuild_gate=bool(args.rebuild_gate),
            allow_passthrough=False if (args.rebuild_gate or args.no_passthrough) else None,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    suffix = "_u238_rebuildgate" if args.rebuild_gate else ""
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_snap_stable1200{suffix}.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_snap_stable1200_md(payload), encoding="utf-8")
    print((payload.get("verdict") or {}).get("summary", ""))
    print((payload.get("verdict") or {}).get("recommendation", ""))
    print(f"Wrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
