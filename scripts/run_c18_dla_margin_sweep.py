#!/usr/bin/env python3
"""C18-dla margin sweep · step_down_left（a=Δv/Δt）· seg_last · margin 0.06–0.10。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_mono_score_swap_c import C18_DLA_MARGIN_SWEEP, run_score_swap_c_sweep
from report_paths import RESEARCH_RRG
from stock_db import DEFAULT_DB_PATH, connect

WINDOWS = [
    ("full", "2024-01-01", "2026-06-22"),
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
    ("2026_h1", "2026-01-01", "2026-06-22"),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18-dla margin sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    windows: list[dict] = []
    try:
        for label, ds, de in WINDOWS:
            print(f"window {label} ({ds} .. {de})", flush=True)
            windows.append(
                run_score_swap_c_sweep(
                    conn,
                    date_start=ds,
                    date_end=de,
                    configs=C18_DLA_MARGIN_SWEEP,
                )
            )
    finally:
        conn.close()

    for w in windows:
        w["label"] = w.get("label") or "full"  # run_score_swap_c_sweep has no label

    # re-run with labels by wrapping summaries
    labeled: list[dict] = []
    for (label, ds, de), payload in zip(WINDOWS, windows):
        labeled.append(
            {
                "label": label,
                "date_start": ds,
                "date_end": de,
                "reference_c0_hold7": payload.get("reference_c0_hold7"),
                "summaries": payload.get("summaries") or [],
            }
        )

    full = labeled[0]
    ranked = sorted(
        full["summaries"],
        key=lambda s: (-(s.get("mean_excess_pct") or -999.0), -(s.get("n_periods") or 0)),
    )
    payload = {
        "hypothesis": "C18-dla · step_down_left（a=Δv/Δt on RRG plane）· seg_last margin 0.06–0.10",
        "windows": labeled,
        "champion": ranked[0] if ranked else None,
    }

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_c18_dla_margin_sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    dls1 = next((s for s in full["summaries"] if s["variant_id"] == "C18-dls1"), {})
    c18 = next((s for s in full["summaries"] if s["variant_id"] == "C18"), {})
    print(f"C18={c18.get('mean_excess_pct')}% · dls1={dls1.get('mean_excess_pct')}%")
    for s in sorted(
        [x for x in full["summaries"] if str(x.get("variant_id", "")).startswith("C18-dla")],
        key=lambda x: x.get("effective_margin") or 0,
    ):
        print(
            f"  {s['variant_id']} margin={s.get('effective_margin')} "
            f"excess={s.get('mean_excess_pct')}% swaps={s.get('swaps_total')} "
            f"Δc0={s.get('delta_vs_c0_hold7_pp')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
