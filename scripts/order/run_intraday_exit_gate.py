#!/usr/bin/env python3
"""09:05 組合閘門 · dry-run · optional notify."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from order.intraday_exit_gate import (  # noqa: E402
    append_gate_daily_log,
    evaluate_portfolio_gate_with_sync,
    format_portfolio_gate_report,
    gate_evaluation_final,
    persist_portfolio_gate,
    write_portfolio_gate_report,
)
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _send_notify(*, session_date: str, log_path: Path, exit_code: int) -> None:
    import os

    stamp = session_date.replace("-", "")
    env = dict(os.environ)
    env["ROOT"] = str(ROOT)
    subprocess.run(
        [str(ROOT / "scripts" / "intraday_exit_gate_notify.sh"), str(exit_code), stamp],
        cwd=ROOT,
        env=env,
        check=False,
    )


def main() -> int:
    load_project_dotenv()
    parser = argparse.ArgumentParser(description="09:05 portfolio exit gate")
    parser.add_argument("--date", metavar="YYYY-MM-DD")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--minute", default="09:05")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--persist", action="store_true", help="寫入 order_intraday_exit_log")
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使已有 gate_on/gate_off 仍重跑（除錯用）",
    )
    parser.add_argument(
        "--no-sync-kbar",
        action="store_true",
        help="略過 gate 標的 1m K sync（除錯用）",
    )
    args = parser.parse_args()

    import os
    from datetime import datetime
    from zoneinfo import ZoneInfo

    session = args.date or datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat()
    sync_kbar = not args.no_sync_kbar and os.environ.get("INTRADAY_GATE_KBAR_SYNC", "1").strip() not in (
        "0",
        "false",
        "False",
    )
    conn = connect(args.db)
    try:
        if not args.force and gate_evaluation_final(conn, session):
            print(f"skip: gate already evaluated for {session}")
            return 0
        result = evaluate_portfolio_gate_with_sync(
            conn,
            session_date=session,
            minute=args.minute,
            dry_run=True,
            sync_kbar=sync_kbar,
        )
        if args.persist:
            persist_portfolio_gate(conn, result)
    finally:
        conn.close()

    text = format_portfolio_gate_report(result)
    print(text)
    log_path = append_gate_daily_log(result)
    print(f"\n已追加 log：{log_path}")
    if args.write or args.notify:
        path = write_portfolio_gate_report(result)
        print(f"已寫入：{path}")
    if args.notify and result.evaluated and result.portfolio_exit_mode:
        _send_notify(session_date=session, log_path=log_path, exit_code=0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
