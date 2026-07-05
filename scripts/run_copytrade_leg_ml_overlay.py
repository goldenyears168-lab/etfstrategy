#!/usr/bin/env python3
"""Phase 4 · Copytrade multi-leg ML allocation overlay (mom+fund ranking)."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

REPORT_DIR = ROOT / "reports" / "research" / "tw100"


def main() -> int:
    from research.backtest.copytrade_leg_ml_overlay import (  # noqa: WPS433
        CopytradeLegMlOverlayConfig,
        format_overlay_markdown,
        run_copytrade_leg_ml_overlay,
        write_overlay_artifact,
    )
    from stock_db import DEFAULT_DB_PATH, connect  # noqa: WPS433

    p = argparse.ArgumentParser(description="Copytrade leg ML overlay · Phase 4")
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument("--etf", default="00981A")
    p.add_argument("--strategy-id", default="L1H9")
    p.add_argument(
        "--feature-set",
        choices=("mom", "fund", "mom_fund", "mom_chip", "mom_fund_chip"),
        default="mom_fund",
    )
    p.add_argument(
        "--overlay-mode",
        choices=("all_multi_leg", "rrg_intersect"),
        default="all_multi_leg",
        help="all_multi_leg=P4-1 · rrg_intersect=P4-2",
    )
    p.add_argument("--start", default="2015-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--capital", type=float, default=10_000.0)
    p.add_argument("--oos-ratio", type=float, default=0.2)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--no-md", action="store_true")
    args = p.parse_args()

    stamp = date.today().strftime("%Y%m%d")
    mode_suffix = "rrg" if args.overlay_mode == "rrg_intersect" else "legs"
    fs_suffix = args.feature_set if args.feature_set != "mom_fund" else "mom_fund"
    out = args.out or (
        REPORT_DIR / f"copytrade_leg_ml_overlay_{mode_suffix}_{fs_suffix}_{stamp}.json"
    )

    cfg = CopytradeLegMlOverlayConfig(
        etf_code=args.etf,
        strategy_id=args.strategy_id,
        feature_set=args.feature_set,
        overlay_mode=args.overlay_mode,
        window_start=args.start,
        window_end=args.end,
        capital_ntd=args.capital,
        oos_holdout_ratio=args.oos_ratio,
    )

    print(
        f"Running copytrade leg ML overlay · {args.etf} {args.strategy_id} "
        f"· mode={args.overlay_mode} feature_set={args.feature_set} …"
    )
    conn = connect(args.db)
    try:
        payload = run_copytrade_leg_ml_overlay(conn, cfg)
    finally:
        conn.close()

    if payload.get("error"):
        print(f"ERROR: {payload['error']}", file=sys.stderr)
        return 1

    write_overlay_artifact(payload, out)
    print(f"Wrote {out}")
    print(
        f"Baseline α={payload['baseline']['total_alpha_ntd']:+,.0f} · "
        f"Overlay α={payload['overlay']['total_alpha_ntd']:+,.0f} · "
        f"Δ={payload['alpha_delta_ntd']:+,.0f}"
    )
    hyp = payload.get("hypothesis") or {}
    print(f"{hyp.get('id', 'H-TW100-6')}: {'PASS' if hyp.get('pass') else 'FAIL'}")

    if not args.no_md:
        md_path = out.with_suffix(".md")
        md_path.write_text(format_overlay_markdown(payload), encoding="utf-8")
        print(f"Wrote {md_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
