#!/usr/bin/env python3
"""H-PURE · 純 I36 · 固定 fresh mono 持倉 · 無 RRG 輪動。

用法：
  PYTHONPATH=src python scripts/run_c18acc_pure_i36_sweep.py
  PYTHONPATH=src python scripts/run_c18acc_pure_i36_sweep.py \\
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

from research.backtest.c18acc_intraday_1m_hold import (  # noqa: E402
    render_pure_i36_report_md,
    run_pure_i36_analysis,
)
from research.backtest.extension_peak_feature_analysis import (  # noqa: E402
    run_i36_combo_universality,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="H-PURE · pure I36 fixed hold sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-26")
    parser.add_argument("--oos-start", default="2026-01-01")
    parser.add_argument("--n-boot", type=int, default=10000)
    parser.add_argument("--skip-universality", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--md", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        universality = None
        if not args.skip_universality:
            print("H-PURE-2 · I36 combo universality scan ...", flush=True)
            universality = run_i36_combo_universality(
                conn,
                date_start=args.date_start,
                date_end=args.date_end,
            )
            print(
                f"  P(fade≥2%)={universality.get('P_fade_pct')}% "
                f"n={universality.get('n_trigger')} pass={universality.get('pass')}",
                flush=True,
            )

        print("H-PURE · P0/P1 backtest ...", flush=True)
        payload = run_pure_i36_analysis(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            oos_start=args.oos_start,
            n_boot=args.n_boot,
            universality=universality,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_c18acc_pure_i36_sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    md_path = args.md or RESEARCH_RRG / f"{stamp}_c18acc_pure_i36_report.md"
    md_path.write_text(render_pure_i36_report_md(payload), encoding="utf-8")
    print(f"Wrote {md_path}")

    rec = payload.get("recommended") or {}
    p1 = rec.get("P1") or {}
    print(
        f"P1 Δ P0={p1.get('delta_vs_baseline_pp')}pp · ext={p1.get('ext_exits')} · "
        f"recommend={rec.get('action')}"
    )
    for hid, r in (payload.get("hypothesis_results") or {}).items():
        print(f"  {hid}: {'PASS' if r.get('pass') else 'FAIL'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
