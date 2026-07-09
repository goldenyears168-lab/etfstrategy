#!/usr/bin/env python3
"""Post-process TW100 WFA JSON · IC tear sheet + Breadth zone G3 stratification."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

REPORT_DIR = ROOT / "reports" / "research" / "tw100"


def main() -> int:
    from research.backtest.archive.tw100_wfa_analysis import (  # noqa: WPS433
        Tw100WfaAnalysisConfig,
        build_wfa_analysis_report,
        load_wfa_payload,
        write_wfa_analysis_artifact,
    )
    from stock_db import DEFAULT_DB_PATH, connect  # noqa: WPS433

    p = argparse.ArgumentParser(description="TW100 WFA analysis · tear sheet + G3 regime")
    p.add_argument(
        "--wfa-json",
        type=Path,
        default=REPORT_DIR / "tw100_alpha158_lgbm_wfa_20260629_tuned.json",
        help="Input walk-forward artifact",
    )
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument("--no-regime", action="store_true", help="Skip breadth zone stratification")
    p.add_argument("--json-out", type=Path, default=None)
    p.add_argument("--md-out", type=Path, default=None)
    args = p.parse_args()

    payload = load_wfa_payload(args.wfa_json)
    stamp = date.today().strftime("%Y%m%d")
    json_out = args.json_out or (REPORT_DIR / f"tw100_wfa_analysis_{stamp}.json")
    md_out = args.md_out or (REPORT_DIR / f"tw100_wfa_analysis_{stamp}.md")

    cfg = Tw100WfaAnalysisConfig(wfa_json=args.wfa_json)
    conn = None if args.no_regime else connect(args.db)
    try:
        report = build_wfa_analysis_report(payload, conn, cfg)
    finally:
        if conn is not None:
            conn.close()

    write_wfa_analysis_artifact(report, json_out, md_out)

    tear = report.get("ic_tear_sheet") or {}
    regime = report.get("regime_stratification") or {}
    agg = tear.get("aggregate_test_ic") or {}
    print(f"Wrote {json_out}")
    print(f"Wrote {md_out}")
    print(
        f"test_ic mean={agg.get('mean')} icir={agg.get('icir')} "
        f"decay={tear.get('valid_to_test_decay_mean')} "
        f"G3={regime.get('g3_regime_stratification', regime.get('status'))}"
    )
    g3 = regime.get("g3_regime_stratification")
    if g3 == "failed":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
