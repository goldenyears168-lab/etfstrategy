#!/usr/bin/env python3
"""C18acc 全槽率矩陣 · 3/5/7/9 槽 × fresh_only / fresh_then_tier2 / tier2_all · Research layer sweep。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_mono_score_swap_c import (  # noqa: E402
    render_c18acc_fullslot_matrix_md,
    run_c18acc_fullslot_matrix,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="C18acc fullslot matrix sweep (3/5/7/9 × 3 fill policies)"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-22")
    parser.add_argument("--notional", type=float, default=100_000.0)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_c18acc_fullslot_matrix(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            comparison_notional_ntd=args.notional,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_fullslot_matrix.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_fullslot_matrix_md(payload), encoding="utf-8")
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")

    best = payload.get("best") or {}
    print(
        f"Best {best.get('variant_id')} [{best.get('fill_policy')}]: "
        f"mean_excess={best.get('mean_excess_pct')}% "
        f"swaps={best.get('swaps_total')} util={best.get('slot_util_pct')}% "
        f"tier2_sup={best.get('tier2_supplement_days')}"
    )
    dc = payload.get("drift_check") or {}
    champ = dc.get("champion_C18acc") or {}
    s3 = dc.get("S3_fresh_only") or {}
    print(f"Drift champion C18acc: {champ.get('mean_excess_pct')}% · swaps={champ.get('swaps_total')}")
    print(
        f"Drift S3-fresh_only: {s3.get('mean_excess_pct')}% · util={s3.get('slot_util_pct')}% "
        f"· tier2_sup_days={s3.get('tier2_supplement_days')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
