#!/usr/bin/env python3
"""RRG · Optuma Signal Test（官網路径 · 大宇宙 · 相对 IX0001 超额）。

Universe：all_kbar + 13:00 scale（全成分有 1m kbar 者）；不与策略池耦合。

用法：
  PYTHONPATH=src python scripts/run_overnight_rrg_signal_test.py
  PYTHONPATH=src python scripts/run_overnight_rrg_signal_test.py \\
    --date-start 2024-01-01 --date-end 2026-06-26
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.overnight_rrg_signal_test import (  # noqa: E402
    render_signal_test_md,
    run_large_universe_signal_test,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RRG Optuma Signal Test · 大宇宙 vs IX0001")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-26")
    parser.add_argument("--oos-train-ratio", type=float, default=0.7)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_large_universe_signal_test(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            oos_train_ratio=args.oos_train_ratio,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_overnight_rrg_signal_test.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out.with_suffix(".md")
    md_path.write_text(render_signal_test_md(payload), encoding="utf-8")
    print(f"Wrote {out}")
    print(f"Wrote {md_path}")

    st = payload.get("signal_tests") or {}
    for kind in ("lagging_up_left", "leading", "seg_last_q4"):
        s = (st.get(kind) or {}).get("overnight", {})
        if s and not s.get("insufficient"):
            print(f"{kind} overnight", s)
    for c in payload.get("pair_comparisons") or []:
        if c.get("horizon") == "overnight" and not c.get("insufficient"):
            print("compare", c.get("label"), c)
    bt = payload.get("backtests") or {}
    print("backtest overnight", bt.get("overnight_ew_compound"))
    print("backtest hold7", bt.get("hold7_nonoverlap_ew"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
