#!/usr/bin/env python3
"""C18acc+S2 · W-EXIT save 子樣本分析（依 W-annot JSON）。

用法：
  PYTHONPATH=src python3 scripts/run_c18acc_s2_dual_wma_exit_save.py
  PYTHONPATH=src python3 scripts/run_c18acc_s2_dual_wma_exit_save.py \\
    --annot reports/research/rrg/20260704_c18acc_s2_dual_wma_annot.json
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
from research.backtest.c18acc_s2_dual_wma_exit_save import (  # noqa: E402
    load_annot_payload,
    render_w_exit_save_md,
    run_w_exit_save_analysis,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc+S2 W-EXIT save analysis")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--annot",
        type=Path,
        default=RESEARCH_RRG / "20260704_c18acc_s2_dual_wma_annot.json",
        help="W-annot JSON from run_c18acc_s2_dual_wma_annot.py",
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    if not args.annot.is_file():
        raise SystemExit(f"annot not found: {args.annot} — run run_c18acc_s2_dual_wma_annot.py first")

    annot = load_annot_payload(args.annot)
    conn = connect(args.db)
    try:
        payload = run_w_exit_save_analysis(conn, annot)
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_s2_dual_wma_exit_save.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_w_exit_save_md(payload), encoding="utf-8")

    w5 = payload["summaries"]["w5_only"]
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print(
        f"  w5_only n={w5.get('n')} median_save={w5.get('median_save_vs_orig_pct')}% "
        f"pct_positive={w5.get('pct_save_positive')}%"
    )
    for hid, h in payload.get("hypotheses", {}).items():
        print(f"  {hid}: {'pass' if h.get('pass') else 'fail'} ({h.get('value')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
