#!/usr/bin/env python3
"""C18acc vs ABC v3+f1 · gap diagnostic from existing comparison JSON.

Uses offline NAV / trade ledgers only — no dual-WMA panel, no 1m poll rebuild.
Pricing preference for any follow-on work: 5m aggregated kbar helper.

  PYTHONPATH=src python3 scripts/run_c18acc_abc_v3_f1_gap_diagnostic.py
  PYTHONPATH=src python3 scripts/run_c18acc_abc_v3_f1_gap_diagnostic.py \\
    --comparison reports/research/rrg/20260708_c18acc_abc_v3_f1_comparison.json
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
from research.backtest.archive.c18acc_abc_v3_f1_gap_diagnostic import (  # noqa: E402
    build_gap_diagnostic,
    render_gap_diagnostic_md,
)

DEFAULT_COMPARISON = RESEARCH_RRG / "20260708_c18acc_abc_v3_f1_comparison.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Gap diagnostic · offline from comparison JSON")
    ap.add_argument("--comparison", type=Path, default=DEFAULT_COMPARISON)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.comparison.exists():
        print(f"BLOCKER: comparison JSON not found: {args.comparison}", file=sys.stderr)
        return 1

    payload = json.loads(args.comparison.read_text(encoding="utf-8"))
    diag = build_gap_diagnostic(payload)

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_abc_v3_f1_gap_diagnostic.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(diag, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_gap_diagnostic_md(diag), encoding="utf-8")

    dgn = diag.get("diagnosis") or {}
    last = diag.get("last_90d") or {}
    print(dgn.get("verdict_zh") or "")
    print(
        f"Driver={dgn.get('primary_driver')} · "
        f"last90 ABC={((last.get('abc_f1') or {}).get('period_return_pct'))}% "
        f"C18={((last.get('c18acc') or {}).get('period_return_pct'))}%"
    )
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
