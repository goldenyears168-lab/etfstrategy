#!/usr/bin/env python3
"""ABC v3 / v3+f1 × RH50 regime entry gate · slot backtest.

用法：
  PYTHONPATH=src python3 scripts/run_abc_v3_rh50_gate.py
  PYTHONPATH=src python3 scripts/run_abc_v3_rh50_gate.py --f1
  PYTHONPATH=src python3 scripts/run_abc_v3_rh50_gate.py --wma-length-study
  PYTHONPATH=src python3 scripts/run_abc_v3_rh50_gate.py --wma-length-study --rebuild-panel
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.abc_v3_rh50_gate import (  # noqa: E402
    render_abc_v3_f1_rh50_wma_length_study_md,
    render_abc_v3_rh50_gate_md,
    run_abc_v3_f1_rh50_wma_length_study,
    run_abc_v3_rh50_gate,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _default_ledger_json() -> Path | None:
    candidates = sorted(RESEARCH_RRG.glob("*_c18acc_abc_v3_f1_comparison.json"))
    return candidates[-1] if candidates else None


def _load_ledger_as_candidates(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("abc_f1_trade_ledger") or []
    if not rows:
        raise ValueError(f"no abc_f1_trade_ledger in {path}")
    # Ledger legs already passed 5-slot fill · reuse as R0 candidate set for gate overlay.
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "stock_id": r.get("stock_id"),
                "stock_name": r.get("stock_name") or "",
                "entry_date": r.get("entry_date"),
                "exit_date": r.get("exit_date"),
                "entry_minute": r.get("entry_minute"),
                "entry_px": r.get("entry_px"),
                "exit_px": r.get("exit_px"),
                "entry_poll_idx": r.get("entry_poll_idx"),
                "net_pct": r.get("net_pct"),
                "excess_pct": r.get("excess_pct"),
                "rank_score": r.get("rank_score"),
                "hold_days": r.get("hold_days") or 3,
                "features": {},
                "source_strategy": "abc_v3_f1_ledger",
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ABC v3 / v3+f1 × RH50 entry gate")
    ap.add_argument("--f1", action="store_true", help="Use ABC v3+f1 matcher (default: v3 base)")
    ap.add_argument(
        "--wma-length-study",
        action="store_true",
        help="Compare RH WMA20 vs WMA5 on ABC v3+f1 (default: ledger · no 1m rebuild)",
    )
    ap.add_argument(
        "--ledger-json",
        type=Path,
        default=None,
        help="ABC f1 trade ledger JSON (default: latest c18acc_abc_v3_f1_comparison)",
    )
    ap.add_argument(
        "--rebuild-panel",
        action="store_true",
        help="Rebuild dual-WMA intraday panel from 1m kbar (slow)",
    )
    ap.add_argument(
        "--rh-wma-length",
        type=int,
        default=20,
        help="RRG WMA length for rotation_health (default 20 · research: 5)",
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2024-01-01")
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--is-end", default="2025-12-31")
    ap.add_argument("--oos-start", default="2026-01-01")
    ap.add_argument("--oos-end", default="2026-06-30")
    ap.add_argument("--n-slots", type=int, default=5)
    ap.add_argument("--score-spec", default="v0")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    t0 = time.perf_counter()
    stamp = date.today().strftime("%Y%m%d")
    conn = connect(args.db)
    try:
        if args.wma_length_study:
            candidates = None
            if not args.rebuild_panel:
                ledger_path = args.ledger_json or _default_ledger_json()
                if ledger_path is None or not ledger_path.exists():
                    print(
                        "BLOCKER: no ledger JSON · pass --ledger-json or --rebuild-panel",
                        file=sys.stderr,
                    )
                    return 1
                candidates = _load_ledger_as_candidates(ledger_path)
                print(f"Loaded {len(candidates)} ledger legs from {ledger_path}")
            payload = run_abc_v3_f1_rh50_wma_length_study(
                conn,
                date_start=args.date_start,
                date_end=args.date_end,
                is_end=args.is_end,
                oos_start=args.oos_start,
                oos_end=args.oos_end,
                n_slots=args.n_slots,
                score_spec=args.score_spec,
                candidates=candidates,
                rebuild_panel=args.rebuild_panel,
            )
            out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_rh50_wma5_study.json"
            out_md = out_json.with_suffix(".md")
            out_json.parent.mkdir(parents=True, exist_ok=True)
            out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            out_md.write_text(
                render_abc_v3_f1_rh50_wma_length_study_md(payload), encoding="utf-8"
            )
            cmp_ = payload.get("compare") or {}
            print(f"Wrote {out_json}")
            print(f"Wrote {out_md}")
            print(
                f"  W20 pass={cmp_.get('W20_pass')} OOSΔex={cmp_.get('W20_OOS_delta_excess_pp')}pp "
                f"capture={cmp_.get('W20_capture_pct')}%"
            )
            print(
                f"  W5  pass={cmp_.get('W5_pass')} OOSΔex={cmp_.get('W5_OOS_delta_excess_pp')}pp "
                f"capture={cmp_.get('W5_capture_pct')}%"
            )
            print(f"Total elapsed: {(time.perf_counter() - t0) / 60:.1f} min")
            return 0

        payload = run_abc_v3_rh50_gate(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            oos_start=args.oos_start,
            oos_end=args.oos_end,
            n_slots=args.n_slots,
            score_spec=args.score_spec,
            matcher_mode="f1" if args.f1 else "v3",
            rh_wma_length=args.rh_wma_length,
        )
    finally:
        conn.close()

    suffix = "abc_v3_f1_rh50_gate" if args.f1 else "abc_v3_rh50_gate"
    if args.rh_wma_length != 20:
        suffix = f"{suffix}_w{args.rh_wma_length}"
    out_json = args.out or RESEARCH_RRG / f"{stamp}_{suffix}.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_abc_v3_rh50_gate_md(payload), encoding="utf-8")

    gated_variant_id = "ABC-V3-F1+RH50" if args.f1 else "ABC-V3+RH50"
    if args.rh_wma_length != 20:
        gated_variant_id = f"{gated_variant_id}-W{args.rh_wma_length}"
    ev = (payload.get("evaluation") or {}).get(gated_variant_id) or {}
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print(
        f"  {gated_variant_id} pass={ev.get('pass')} capture={ev.get('capture_rate_full_pct')}% "
        f"OOS Δex={ev.get('OOS_delta_excess_pp')}pp Δdaily={ev.get('OOS_delta_daily_pp')}pp "
        f"n={ev.get('OOS_n_legs')} · RH WMA={args.rh_wma_length}"
    )
    print(f"Total elapsed: {(time.perf_counter() - t0) / 60:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
