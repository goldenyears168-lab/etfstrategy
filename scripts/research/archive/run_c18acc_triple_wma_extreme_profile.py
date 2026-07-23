#!/usr/bin/env python3
"""C18acc · WMA20/5/3 entry vs hold peak/trough · top/bottom decile profile.

用法：
  PYTHONPATH=src python3 scripts/research/archive/run_c18acc_triple_wma_extreme_profile.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.c18acc_triple_wma_extreme_profile import (  # noqa: E402
    render_c18acc_triple_wma_extreme_profile_md,
    run_c18acc_triple_wma_extreme_profile,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="C18acc · triple WMA(20/5/3) extreme profile"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2025-01-02")
    parser.add_argument("--date-end", default=None)
    parser.add_argument("--n-slots", type=int, default=3)
    parser.add_argument("--decile-pct", type=float, default=0.10)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_c18acc_triple_wma_extreme_profile(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            n_slots=args.n_slots,
            decile_pct=args.decile_pct,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_triple_wma_extreme_profile.json"
    out_md = out_json.with_suffix(".md")
    # trim full leg list from JSON for size; keep decile samples
    export = {k: v for k, v in payload.items() if k != "all_legs"}
    export["n_all_legs"] = payload["n_legs"]
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_triple_wma_extreme_profile_md(payload), encoding="utf-8")

    wn = payload["winners_top_decile"]["entry"]["n"]
    ln = payload["losers_bottom_decile"]["entry"]["n"]
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print(f"  legs={payload['n_legs']} top_decile={wn} bottom_decile={ln}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
