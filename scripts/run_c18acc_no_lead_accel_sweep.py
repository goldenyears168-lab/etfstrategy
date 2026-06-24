#!/usr/bin/env python3
"""C18acc · 无 leading top10 · 加速度筛选参数 sweep。

基池 mono_up / mono_up_fresh（三轴 up_right + mono_up + disp∈[1,2) · 不要求 leading）
探索 top-N 排序、四日加速>0、v·a>0、margin、top3/5 等。

用法：
  PYTHONPATH=src python scripts/run_c18acc_no_lead_accel_sweep.py
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
    C18acc_NO_LEAD_ACCEL_SWEEP,
    CHAMPION_SCORE_SWAP_C_VARIANT_ID,
    run_score_swap_c_sweep,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc no-leading top10 accel sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-22")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_score_swap_c_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            configs=C18acc_NO_LEAD_ACCEL_SWEEP,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_c18acc_no_lead_accel_sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    champ_ex = next(
        (s.get("mean_excess_pct") for s in payload["summaries"] if s.get("variant_id") == CHAMPION_SCORE_SWAP_C_VARIANT_ID),
        None,
    )
    print(f"\n对照 C18acc fresh leading: {champ_ex}%")
    print(f"{'variant_id':<22} {'pool':<14} {'rank':<10} {'+acc':<4} {'gate':<12} margin  excess   swaps  Δpp")
    for s in sorted(payload["summaries"], key=lambda x: -(x.get("mean_excess_pct") or -999)):
        vid = str(s.get("variant_id", ""))
        delta = ""
        if champ_ex is not None and s.get("mean_excess_pct") is not None and vid != CHAMPION_SCORE_SWAP_C_VARIANT_ID:
            delta = f"{float(s['mean_excess_pct']) - float(champ_ex):+.2f}"
        print(
            f"{vid:<22} {str(s.get('candidate_pool', '')):<14} "
            f"{str(s.get('candidate_rank_key', '')):<10} "
            f"{str(s.get('candidate_require_positive_accel', '')):<4} "
            f"{str(s.get('challenger_gate', '')):<12} "
            f"{s.get('score_margin', '—'):<6} "
            f"{s.get('mean_excess_pct', '—'):>6}% {s.get('swaps_total', 0):>5}  {delta}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
