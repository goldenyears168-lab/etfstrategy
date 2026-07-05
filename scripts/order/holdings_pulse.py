#!/usr/bin/env python3
"""持倉即時脈動 · 富邦 snapshot + 現價 + RRG 收盤 vs 盤中 + exit playbook。

  .venv-fubon/bin/python scripts/order/holdings_pulse.py
  .venv-fubon/bin/python scripts/order/holdings_pulse.py --sync-rrg-intraday --write
  .venv-fubon/bin/python scripts/order/holdings_pulse.py --date 2026-06-26 --no-fubon
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from order.fubon_session import check_python_version  # noqa: E402
from order.holdings_pulse import (  # noqa: E402
    build_holdings_pulse,
    default_report_path,
    format_holdings_pulse,
    format_holdings_pulse_digest,
    write_holdings_pulse,
)
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main() -> int:
    try:
        check_python_version()
    except RuntimeError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2

    load_project_dotenv()

    parser = argparse.ArgumentParser(description="持倉即時脈動 · 富邦 + RRG + exit playbook")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="Session date（預設今日台北）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--no-fubon", action="store_true", help="略過富邦 API")
    parser.add_argument(
        "--sync-rrg-intraday",
        action="store_true",
        help="拉 FinMind tick 並寫入 rrg_universe_scores（screen_kind=intraday）",
    )
    parser.add_argument(
        "--no-futures",
        action="store_true",
        help="不刷新 morning_risk（僅讀 DB）",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="寫入 reports/order/snapshots/holdings_pulse_YYYYMMDD.md",
    )
    parser.add_argument(
        "--digest-only",
        action="store_true",
        help="僅輸出精簡 digest 段落（供盤中寄信合併）",
    )
    parser.add_argument("-o", "--output", type=Path, help="自訂輸出路徑（需搭配 --write）")
    args = parser.parse_args()

    conn = connect(args.db)
    try:
        pulse = build_holdings_pulse(
            conn,
            session_date=args.date,
            use_fubon=not args.no_fubon,
            sync_rrg_intraday=args.sync_rrg_intraday,
            refresh_morning_risk=not args.no_futures,
        )
    finally:
        conn.close()

    text = (
        format_holdings_pulse_digest(pulse)
        if args.digest_only
        else format_holdings_pulse(pulse)
    )
    print(text)

    out_path = write_holdings_pulse(pulse, args.output) if args.write else default_report_path(pulse.session_date)
    if args.write and not args.digest_only:
        print(f"\n已寫入：{out_path}")

    exit_code = 1 if pulse.fubon_error and not args.no_fubon else 0
    if not pulse.holdings and not args.no_fubon:
        exit_code = max(exit_code, 1)

    if args.date and args.date != datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat():
        print(f"\n報告路徑（未寫入）：{out_path}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
