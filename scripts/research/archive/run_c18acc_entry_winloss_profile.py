#!/usr/bin/env python3
"""C18acc · full-population entry WMA20/5/3 win vs loss profile.

用法：
  PYTHONPATH=src python3 scripts/research/archive/run_c18acc_entry_winloss_profile.py
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
from research.backtest.archive.c18acc_entry_winloss_profile import (  # noqa: E402
    render_c18acc_entry_winloss_profile_md,
    run_c18acc_entry_winloss_profile,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="C18acc · entry WMA win/loss profile (full population)"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2025-01-02")
    parser.add_argument("--date-end", default=None)
    parser.add_argument("--n-slots", type=int, default=3)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_c18acc_entry_winloss_profile(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            n_slots=args.n_slots,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_entry_winloss_profile.json"
    out_md = out_json.with_suffix(".md")
    export = {k: v for k, v in payload.items() if k != "all_legs"}
    export["n_all_legs"] = payload["n_legs"]
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_entry_winloss_profile_md(payload), encoding="utf-8")

    prof = payload["profile"]
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print(
        f"  legs={payload['n_legs']} win={prof['n_win']} loss={prof['n_loss']} "
        f"win_rate={prof['win_rate_pct']}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
