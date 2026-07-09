#!/usr/bin/env python3
"""ABC v3+f1 · overnight regime gate · Phase 0 annotation (SOX/SPY).

用法：
  PYTHONPATH=src python3 scripts/run_abc_v3_f1_overnight_regime_annot.py
  PYTHONPATH=src python3 scripts/run_abc_v3_f1_overnight_regime_annot.py \\
    --ledger-json reports/research/rrg/20260708_c18acc_abc_v3_f1_comparison.json
  PYTHONPATH=src python3 scripts/run_abc_v3_f1_overnight_regime_annot.py --rebuild-panel
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
from research.backtest.archive.abc_v3_f1_overnight_regime_annot import (  # noqa: E402
    load_abc_f1_trade_ledger_from_json,
    render_abc_v3_f1_overnight_regime_annot_md,
    run_abc_v3_f1_overnight_regime_annot,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _default_ledger_json() -> Path | None:
    candidates = sorted(RESEARCH_RRG.glob("*_c18acc_abc_v3_f1_comparison.json"))
    return candidates[-1] if candidates else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ABC v3+f1 · overnight regime Phase 0 annot")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2024-01-01")
    ap.add_argument("--date-end", default="2026-07-07")
    ap.add_argument("--ledger-json", type=Path, default=None, help="slot-executed ledger JSON")
    ap.add_argument(
        "--rebuild-panel",
        action="store_true",
        help="Rebuild intraday panel from DB (slow · ~90 min)",
    )
    ap.add_argument("--case-date", default="2026-07-07")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    ledger = None
    ledger_meta = None
    if not args.rebuild_panel:
        ledger_path = args.ledger_json or _default_ledger_json()
        if ledger_path is None or not ledger_path.exists():
            print(
                "BLOCKER: no ledger JSON · pass --ledger-json or --rebuild-panel",
                file=sys.stderr,
            )
            return 1
        ledger, ledger_meta = load_abc_f1_trade_ledger_from_json(ledger_path)
        print(f"Loaded {len(ledger)} legs from {ledger_path}")

    conn = connect(args.db)
    try:
        payload = run_abc_v3_f1_overnight_regime_annot(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            ledger=ledger,
            ledger_meta=ledger_meta,
            case_date=args.case_date,
            rebuild_panel=args.rebuild_panel,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_overnight_regime_annot.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_abc_v3_f1_overnight_regime_annot_md(payload), encoding="utf-8")

    sc = payload["signal_compare"]
    sox = sc["risk_off_sox"]
    spy = sc["risk_off_spy"]
    combo = sc["combo_risk_off"]
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print(
        f"  legs={payload['n_legs']} mean_excess={payload['summary'].get('mean_excess_pct')}% "
        f"sox_from={payload.get('effective_start_sox')}"
    )
    print(
        f"  risk_off_sox: flagged n={sox['flagged_n']} Δret={sox['delta_return_pp']}pp "
        f"Δex={sox['delta_excess_pp']}pp pass={sox['h_or_0_pass']}"
    )
    print(
        f"  risk_off_spy: flagged n={spy['flagged_n']} Δret={spy['delta_return_pp']}pp "
        f"Δex={spy['delta_excess_pp']}pp pass={spy['h_or_0_pass']}"
    )
    print(
        f"  combo_risk_off: flagged n={combo['flagged_n']} Δret={combo['delta_return_pp']}pp pass="
        f"{combo['h_or_0_pass']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
