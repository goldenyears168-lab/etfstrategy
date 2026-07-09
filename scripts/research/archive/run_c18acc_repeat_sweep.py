#!/usr/bin/env python3
"""C18acc 加碼補滿研究 · repeat vs no-repeat × 3/5/7 槽 · Research layer sweep。

fill_repeat=True：空槽可重複買同一支股票（加碼補滿），資金永遠 100% 部署。
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
    render_c18acc_repeat_matrix_md,
    run_c18acc_repeat_matrix,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="C18acc repeat fill sweep (S3/S5/S7 × fresh vs repeat)"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-22")
    parser.add_argument("--notional", type=float, default=100_000.0)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_c18acc_repeat_matrix(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            comparison_notional_ntd=args.notional,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_repeat_matrix.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_repeat_matrix_md(payload), encoding="utf-8")
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")

    best = payload.get("best") or {}
    print(
        f"Best {best.get('variant_id')}: mean_excess={best.get('mean_excess_pct')}% "
        f"Sharpe={best.get('sharpe_ratio')} util={best.get('slot_util_pct')}%"
    )
    cmp = payload.get("comparison") or {}
    s3f = cmp.get("S3_fresh") or {}
    s3r = cmp.get("S3_repeat") or {}
    print(
        f"S3_fresh  : {s3f.get('mean_excess_pct')}% · Sharpe={s3f.get('sharpe_ratio')} · util={s3f.get('slot_util_pct')}%"
    )
    print(
        f"S3_repeat : {s3r.get('mean_excess_pct')}% · Sharpe={s3r.get('sharpe_ratio')} "
        f"· util={s3r.get('slot_util_pct')}% · 加碼天數={s3r.get('fill_repeat_days')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
