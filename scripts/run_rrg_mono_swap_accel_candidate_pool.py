#!/usr/bin/env python3
"""rrg-mono-swap-accel（C18acc）· 候选池对照：不要求 leading（mono_up）vs fresh leading。

用法：
  PYTHONPATH=src python scripts/run_rrg_mono_swap_accel_candidate_pool.py
  PYTHONPATH=src python scripts/run_rrg_mono_swap_accel_candidate_pool.py \\
    --date-start 2024-01-01 --date-end 2026-06-22
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_mono_score_swap_c import (  # noqa: E402
    render_swap_accel_candidate_pool_markdown,
    run_swap_accel_candidate_pool_comparison,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc candidate pool · no leading vs fresh")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-22")
    parser.add_argument("--out-md", type=Path, default=None)
    parser.add_argument("--out-json", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        results = run_swap_accel_candidate_pool_comparison(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_md = args.out_md or RESEARCH_RRG / f"{stamp}_rrg_mono_swap_accel_candidate_pool.md"
    out_json = args.out_json or RESEARCH_RRG / f"{stamp}_rrg_mono_swap_accel_candidate_pool.json"
    out_md.parent.mkdir(parents=True, exist_ok=True)

    md = render_swap_accel_candidate_pool_markdown(results)
    out_md.write_text(md, encoding="utf-8")

    slim = {
        "slug": results["slug"],
        "short_name": results["short_name"],
        "date_start": results["date_start"],
        "date_end": results["date_end"],
        "pool_sizes": results["pool_sizes"],
        "gate_definitions": results["gate_definitions"],
        "champion": results["champion"],
        "delta_vs_champion_pp": results["delta_vs_champion_pp"],
        "best": results["best"],
        "summaries": [v["summary"] for v in results["by_variant"].values()],
    }
    out_json.write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out_md}")
    print(f"Wrote {out_json}")

    for s in sorted(
        slim["summaries"],
        key=lambda x: -(x.get("mean_excess_pct") or -999),
    ):
        vid = s.get("variant_id", "")
        delta = results["delta_vs_champion_pp"].get(vid)
        delta_s = f" Δ={delta:+.2f}pp" if delta is not None else ""
        print(
            f"  {vid:14} pool={s.get('candidate_pool'):14} "
            f"excess={s.get('mean_excess_pct')}% swaps={s.get('swaps_total')}{delta_s}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
