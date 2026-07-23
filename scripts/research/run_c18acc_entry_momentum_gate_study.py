#!/usr/bin/env python3
"""C18acc · entry-time W3 MV/RV momentum gate study.

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/run_c18acc_entry_momentum_gate_study.py
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
from research.backtest.c18acc_entry_momentum_gate_study import (  # noqa: E402
    render_entry_momentum_gate_md,
    run_entry_momentum_gate_study,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C18acc entry W3 MV/RV momentum gates")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2024-01-02")
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--confirm-bars", type=int, default=2)
    ap.add_argument("--is-end", default="2025-12-31")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_entry_momentum_gate_study(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            confirm_bars=args.confirm_bars,
            is_end=args.is_end,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_entry_momentum_gate.json"
    out_md = out_json.with_suffix(".md")
    export = {k: v for k, v in payload.items() if k != "sample_legs"}
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_entry_momentum_gate_md(payload), encoding="utf-8")
    print(payload.get("verdict", {}).get("summary", ""))
    print(f"Wrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
