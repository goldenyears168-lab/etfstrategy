#!/usr/bin/env python3
"""ABC v3+f1 · TP/SL split study · hold 5d · gap + RV.

  PYTHONPATH=src python3 scripts/research/archive/run_abc_v3_f1_kinematic_tp_sl_study.py \\
    --legs-cache reports/research/rrg/20260709_abc_f1_legs_5d_90d.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.abc_v3_f1_kinematic_gap_rv3_study import (  # noqa: E402
    build_abc_f1_kinematic_legs,
)
from research.backtest.archive.abc_v3_f1_kinematic_leadlag_study import (  # noqa: E402
    find_latest_comparison_json,
)
from research.backtest.archive.abc_v3_f1_overnight_regime_annot import (  # noqa: E402
    load_abc_f1_trade_ledger_from_json,
)
from research.backtest.archive.abc_v3_f1_kinematic_tp_sl_study import (  # noqa: E402
    load_legs_cache,
    render_abc_f1_tp_sl_study_md,
    run_abc_f1_tp_sl_study,
    save_legs_cache,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ABC v3+f1 TP/SL split study")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--ledger-json", type=Path, default=None)
    ap.add_argument("--date-start", default="2026-04-01")
    ap.add_argument("--date-end", default="2026-07-07")
    ap.add_argument("--hold-days", type=int, default=5)
    ap.add_argument("--target", type=float, default=8.0)
    ap.add_argument("--legs-cache", type=Path, default=None)
    ap.add_argument("--build-cache-only", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    stamp = date.today().strftime("%Y%m%d")
    cache_path = args.legs_cache or RESEARCH_RRG / f"{stamp}_abc_f1_legs_{args.hold_days}d_90d.json"

    t0 = time.perf_counter()
    legs = None
    if cache_path.is_file() and not args.build_cache_only:
        legs = load_legs_cache(cache_path)
        print(f"Loaded {len(legs)} legs from {cache_path}")

    if legs is None:
        ledger_path = args.ledger_json or find_latest_comparison_json()
        if ledger_path is None:
            raise SystemExit("ledger JSON not found")
        conn = connect(args.db)
        try:
            ledger, _ = load_abc_f1_trade_ledger_from_json(ledger_path)
            ledger = [
                r
                for r in ledger
                if args.date_start <= str(r.get("entry_date") or "") <= args.date_end
            ]
            legs = build_abc_f1_kinematic_legs(conn, ledger, hold_days=args.hold_days)
        finally:
            conn.close()
        save_legs_cache(legs, cache_path)
        print(f"Built and cached {len(legs)} legs → {cache_path}")
        if args.build_cache_only:
            return 0

    payload = run_abc_f1_tp_sl_study(legs, target_pct=args.target, hold_days=args.hold_days)
    payload["date_start"] = args.date_start
    payload["date_end"] = args.date_end
    payload["legs_cache"] = str(cache_path)

    out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_tp_sl_hold{args.hold_days}d.json"
    out_md = out_json.with_suffix(".md")
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_abc_f1_tp_sl_study_md(payload), encoding="utf-8")

    best = payload.get("best_combo") or {}
    print(f"Wrote {out_json}\nWrote {out_md}")
    print(
        f"  hold={best.get('hold_baseline_mean_pct')}% → best={best.get('mean_all_legs_ret_pct')}% "
        f"target={args.target}% runtime={(time.perf_counter()-t0)/60:.1f}min"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
