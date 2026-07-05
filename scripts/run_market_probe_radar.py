#!/usr/bin/env python3
"""Market probe radar · T-1 intraday probes + Holmberg vol → meta routing.

  PYTHONPATH=src .venv/bin/python scripts/run_market_probe_radar.py
  PYTHONPATH=src .venv/bin/python scripts/run_market_probe_radar.py --date 2026-06-30
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from backtest_standard_config import load_backtest_standard_config  # noqa: E402
from research.backtest.market_probe_radar import run_probe_radar  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Market probe radar (Phase 1)")
    parser.add_argument(
        "--date",
        help="probe asof date YYYY-MM-DD (default: latest trading day)",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)

    cfg = load_backtest_standard_config()
    conn = connect(args.db)
    try:
        out = run_probe_radar(conn, asof_date=args.date, cfg=cfg)
    finally:
        conn.close()

    r = out["result"]
    print(f"Probe date: {r.asof_date} → signal for {r.signal_for_date}")
    print(f"Market type: {r.market_type_label} ({r.market_type_id})")
    print(f"Vol decile: {r.vol_decile} · team={r.suggested_team_id}")
    print(f"Wrote {out['json_path']}")
    print(f"Wrote {out['md_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
