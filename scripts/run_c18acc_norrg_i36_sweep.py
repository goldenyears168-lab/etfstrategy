#!/usr/bin/env python3
"""No intraday RRG × I36 · 2×2 factorial (close vs poll_5m).

用法：
  PYTHONPATH=src python scripts/run_c18acc_norrg_i36_sweep.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.c18acc_intraday_1m_hold import (  # noqa: E402
    render_norrg_i36_report_md,
    run_norrg_i36_analysis,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc close timing × I36 factorial")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-26")
    parser.add_argument("--oos-start", default="2026-01-01")
    parser.add_argument("--n-boot", type=int, default=10000)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--md", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_norrg_i36_analysis(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            oos_start=args.oos_start,
            n_boot=args.n_boot,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_c18acc_norrg_i36_sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    md_path = args.md or RESEARCH_RRG / f"{stamp}_c18acc_norrg_i36_report.md"
    md_path.write_text(render_norrg_i36_report_md(payload), encoding="utf-8")
    print(f"Wrote {md_path}")

    decomp = payload.get("factor_decomposition") or {}
    boot = (payload.get("bootstrap") or {}).get("S1_vs_S0") or {}
    pf = boot.get("full") or {}
    print(
        f"S3-S0 poll marginal={decomp.get('intraday_rrg_marginal_pp')}pp · "
        f"I36 close={decomp.get('i36_marginal_close_pp')}pp · "
        f"I36 poll={decomp.get('i36_marginal_poll_pp')}pp · "
        f"retention={decomp.get('i36_retention_ratio')}"
    )
    print(
        f"S1 vs S0 bootstrap: mean Δ={pf.get('mean')}pp P(>0)={pf.get('p_mean_gt_zero')}"
    )
    for hid, r in (payload.get("hypothesis_results") or {}).items():
        print(f"  {hid}: {'PASS' if r.get('pass') else 'FAIL'}")
    print(f"Recommend: {payload.get('recommended', {}).get('action')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
