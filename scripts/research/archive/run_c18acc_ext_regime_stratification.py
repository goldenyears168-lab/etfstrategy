#!/usr/bin/env python3
"""C18acc extension overlay · Regime layer 分層（G3）。

用法：
  PYTHONPATH=src python3 scripts/run_c18acc_ext_regime_stratification.py
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
from research.backtest.archive.c18acc_ext_regime_stratification import (  # noqa: E402
    render_ext_regime_stratification_md,
    run_ext_regime_stratification,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc extension · Regime stratification G3")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-30")
    parser.add_argument("--oos-start", default="2026-01-01")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_ext_regime_stratification(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            oos_start=args.oos_start,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_ext_regime_stratification.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    slim = {k: v for k, v in payload.items() if k != "legs"}
    slim["legs_n"] = {
        "full": len(payload.get("legs", {}).get("full") or []),
        "oos": len(payload.get("legs", {}).get("oos") or []),
    }
    out_json.write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_ext_regime_stratification_md(payload), encoding="utf-8")

    full = payload["stratification"]["full"]
    hypo = payload["hypotheses"]["full"]
    print(f"G3 ext regime: n={full.get('n_ext')} · verdict={payload.get('g3_regime_stratification')}")
    print(f"  H-G3-1: {hypo.get('H-G3-1', {}).get('pass')}")
    print(f"  H-G3-2: {hypo.get('H-G3-2', {}).get('pass')}")
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
