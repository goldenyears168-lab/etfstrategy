#!/usr/bin/env python3
"""C18acc 隔日漲幅豁免 min_hold · Research diagnostic（非 preregister sweep）。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_mono_score_swap_c import (  # noqa: E402
    render_c18acc_gain_waiver_diagnostic_md,
    run_c18acc_gain_waiver_diagnostic,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc gain waiver diagnostic")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2026-01-01")
    parser.add_argument("--date-end", default="2026-06-22")
    parser.add_argument("--notional", type=float, default=60_000.0)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_c18acc_gain_waiver_diagnostic(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            comparison_notional_ntd=args.notional,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_gain_waiver_diagnostic.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_gain_waiver_diagnostic_md(payload), encoding="utf-8")
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")

    d1 = payload.get("day1_stats_baseline") or {}
    print(
        f"Day1≥6%: {d1.get('pct_ge_threshold')}% ({d1.get('count_ge_threshold')}/{d1.get('n')} legs) · "
        f"median day1={d1.get('median_day1_pct')}%"
    )
    for row in payload.get("vs_baseline") or []:
        print(
            f"  {row.get('variant_id')}: Δexcess={row.get('delta_mean_excess_pp')}pp "
            f"Δswaps={row.get('delta_swaps')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
