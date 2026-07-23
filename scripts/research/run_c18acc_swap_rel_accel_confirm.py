#!/usr/bin/env python3
"""C18acc · H-BUY-1 / H-BUY-3 · relative accel margin + swap confirm hold-out.

用法：
  PYTHONPATH=src python3 scripts/research/run_c18acc_swap_rel_accel_confirm.py
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
from research.backtest.c18acc_swap_rel_accel_confirm_study import (  # noqa: E402
    render_rel_accel_confirm_md,
    run_rel_accel_confirm_study,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

DEFAULT_GATE = (
    RESEARCH_RRG
    / "20260711_c18acc_avoid_mixed_gate_live_poll5m_2025-01-02_2026-07-09.json"
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C18acc rel-accel + swap confirm hold-out")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-01-02")
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--is-end", default="2025-12-31")
    ap.add_argument("--confirm-bars", type=int, default=2)
    ap.add_argument("--n-slots", type=int, default=3)
    ap.add_argument("--gate-cache", type=Path, default=DEFAULT_GATE)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_rel_accel_confirm_study(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            confirm_bars=args.confirm_bars,
            n_slots=args.n_slots,
            gate_cache_path=args.gate_cache if args.gate_cache.is_file() else None,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_swap_rel_accel_confirm.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_rel_accel_confirm_md(payload), encoding="utf-8")
    print((payload.get("decision") or {}).get("summary", ""))
    print(f"Wrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
