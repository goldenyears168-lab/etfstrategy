#!/usr/bin/env python3
"""Run TW–NQ early-alert 5m horizon sweep.

  PYTHONPATH=src .venv/bin/python scripts/research/run_us_tw_early_alert_5m_study.py
  PYTHONPATH=src .venv/bin/python scripts/research/run_us_tw_early_alert_5m_study.py \\
      --date-end 2026-07-14 --event-threshold 3.0
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.us_tw_early_alert_5m_study import run_early_alert_study  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="US–TW early alert 5m study")
    ap.add_argument("--date-start", default=None)
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--is-end", default=None)
    ap.add_argument("--event-threshold", type=float, default=3.0)
    ap.add_argument("--out-dir", type=Path, default=None)
    args = ap.parse_args(argv)

    stamp = date.today().strftime("%Y%m%d")
    out_dir = args.out_dir or (RESEARCH_RRG / f"{stamp}_us_tw_early_alert_5m")
    result = run_early_alert_study(
        date_start=args.date_start,
        date_end=args.date_end,
        is_end=args.is_end,
        event_threshold=args.event_threshold,
        out_dir=out_dir,
        case_dates=("2026-07-14", "2026-06-08", "2026-07-07"),
    )
    payload = result["payload"]
    out_json = RESEARCH_RRG / f"{stamp}_us_tw_early_alert_5m.json"
    out_md = RESEARCH_RRG / f"{stamp}_us_tw_early_alert_5m.md"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    out_md.write_text(result["markdown"], encoding="utf-8")

    meta = payload["meta"]
    user = payload.get("user_rule_10m_m0p6") or {}
    print(
        f"days={meta['n_days']} events={meta['n_events_primary']} · "
        f"user10m status={user.get('status')} "
        f"full_prec={(user.get('full') or {}).get('precision')} "
        f"full_rec={(user.get('full') or {}).get('recall')} "
        f"full_fpr={(user.get('full') or {}).get('false_alarm_rate')}"
    )
    print(f"Wrote {out_md}")
    print(f"Wrote {out_json}")
    print(f"Bundle {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
