#!/usr/bin/env python3
"""Overnight gap · seg_last level 分布校準（日 K + 13:00 盤中 · 分布预测专用）。

用法：
  PYTHONPATH=src python scripts/calibrate_overnight_gap_seg.py
  PYTHONPATH=src python scripts/calibrate_overnight_gap_seg.py \\
    --date-start 2024-01-01 --date-end 2026-06-26 --universe fresh_mono
  PYTHONPATH=src python scripts/calibrate_overnight_gap_seg.py \\
    --intraday-seg-mode full_rrg --universe fresh_mono
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
from research.backtest.archive.overnight_gap_seg_calibration import (  # noqa: E402
    calibrate_overnight_gap_seg,
    render_calibration_md,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Overnight gap seg_last distribution calibration")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-26")
    parser.add_argument(
        "--universe",
        choices=("all_kbar", "fresh_mono"),
        default="all_kbar",
        help="all_kbar=有 1m K 的全體；fresh_mono=昨收 PIT fresh mono 池",
    )
    parser.add_argument(
        "--intraday-seg-mode",
        choices=("scale", "full_rrg"),
        default="scale",
        help="S_i@13:00：scale=C0 價格縮放；full_rrg=重算 panel（慢 · 建議 fresh_mono）",
    )
    parser.add_argument(
        "--oos-train-ratio",
        type=float,
        default=0.7,
        help="OOS 時間切分 train 比例（0–1）；0=關閉",
    )
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--md", type=Path, default=None)
    args = parser.parse_args(argv)

    oos_ratio = args.oos_train_ratio if args.oos_train_ratio > 0 else None
    conn = connect(args.db)
    try:
        payload = calibrate_overnight_gap_seg(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            universe=args.universe,
            intraday_seg_mode=args.intraday_seg_mode,
            oos_train_ratio=oos_ratio,
        )
    finally:
        conn.close()

    if payload.get("n_observations", 0) == 0:
        print("ERROR: no observations", file=sys.stderr)
        return 1

    stamp = date.today().strftime("%Y%m%d")
    tag = f"{args.universe}_{args.intraday_seg_mode}"
    out = args.out or RESEARCH_RRG / f"{stamp}_overnight_gap_seg_{tag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    md_path = args.md or out.with_suffix(".md")
    md_path.write_text(render_calibration_md(payload), encoding="utf-8")
    print(f"Wrote {md_path}")

    ht = payload.get("hypothesis_tests") or {}
    for hid in ("H1_sd_quintile", "H2_si_quintile", "H3_kruskal_sd", "H4_partial_si"):
        block = ht.get(hid)
        if block:
            print("in_sample", hid, block)
    oos = payload.get("oos") or {}
    if oos:
        print("oos cut", oos.get("cut_date"), "prob_calibration", oos.get("prob_calibration"))
        te_ht = (oos.get("test") or {}).get("hypothesis_tests") or {}
        for hid in ("H1_sd_quintile", "H2_si_quintile"):
            if te_ht.get(hid):
                print("oos_test", hid, te_ht[hid])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
