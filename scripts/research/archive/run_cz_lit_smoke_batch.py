#!/usr/bin/env python3
"""Smoke-test batch for three new intraday literature strategies.

Strategies tested:
  A. Zarattini §2 Noise Area for 0050   → cz_noise_area_0050.json
  B. Tsai §5 TORB probe-time scan       → cz_torb_probe_scan_0050.json
     + Lundström §4 Vol State filter

Reports written to: reports/research/intraday/
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.archive.cz_noise_area_0050 import run_noise_area_backtest, write_report as write_na  # noqa: E402
from research.backtest.archive.cz_torb_probe_scan import run_torb_probe_scan, write_report as write_torb  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

NOISE_AREA_REPORT = ROOT / "reports/research/intraday/cz_noise_area_0050_2026.json"
TORB_REPORT = ROOT / "reports/research/intraday/cz_torb_probe_scan_0050_2026.json"


def print_variant_table(variants: dict, top_n: int = 8) -> None:
    """Print top-N variants sorted by OOS avg_net."""
    rows = []
    for label, v in variants.items():
        oos = v.get("oos", {})
        rows.append((
            label,
            oos.get("avg_net_pct", -99),
            oos.get("n", 0),
            oos.get("sharpe", -99),
            oos.get("stop_rate_pct", -99),
            v.get("verdict", "FAIL"),
        ))
    rows.sort(key=lambda r: r[1], reverse=True)
    print(f"  {'Label':<40} {'OOS net%':>8} {'n':>5} {'Sharpe':>8} {'Stop%':>6} {'Verdict':>7}")
    print(f"  {'-'*40} {'-'*8} {'-'*5} {'-'*8} {'-'*6} {'-'*7}")
    for label, net, n, sh, stop, verd in rows[:top_n]:
        print(f"  {label:<40} {net:>8.3f} {n:>5} {sh:>8.2f} {stop:>6.1f} {verd:>7}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Literature smoke-test batch")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--noise-area-only", action="store_true")
    parser.add_argument("--torb-only", action="store_true")
    args = parser.parse_args()

    conn = connect(args.db)

    # ── A. Noise Area ──────────────────────────────────────────────────
    if not args.torb_only:
        print("\n=== A. Noise Area (Zarattini §2) for 0050 ===")
        t0 = time.time()
        na = run_noise_area_backtest(conn, stock_id="0050")
        write_na(na, NOISE_AREA_REPORT)
        print(f"  Variants: {na['summary']['total_variants']}")
        print(f"  Pass: {na['summary']['pass_count']}")
        print(f"  Best OOS avg_net: {na['summary']['best_oos_avg_net_pct']:+.3f}%  ({na['summary']['best_variant']})")
        print_variant_table(na["variants"])
        print(f"  → {NOISE_AREA_REPORT}  ({time.time()-t0:.1f}s)")

    # ── B. TORB probe scan + Vol State ─────────────────────────────────
    if not args.noise_area_only:
        print("\n=== B. TORB Probe-Time Scan + Vol State (Tsai §5 + Lundström §4) ===")
        t0 = time.time()
        torb = run_torb_probe_scan(conn, stock_id="0050")
        write_torb(torb, TORB_REPORT)
        print(f"  Variants: {torb['summary']['total_variants']}")
        print(f"  Pass: {torb['summary']['pass_count']}")
        print(f"  TAIEX optimal probe time: t_p={torb['summary']['taiex_optimal_tp']}min")
        print(f"  Best OOS avg_net: {torb['summary']['best_oos_avg_net_pct']:+.3f}%  ({torb['summary']['best_variant']})")
        print_variant_table(torb["variants"], top_n=12)
        print(f"  → {TORB_REPORT}  ({time.time()-t0:.1f}s)")

    print("\nDone.")


if __name__ == "__main__":
    main()
