#!/usr/bin/env python3
"""開盤前持倉 brief · 富邦 snapshot + 昨收觸發價 + 台指 gap。

  .venv-fubon/bin/python scripts/order/morning_holdings_brief.py
  .venv-fubon/bin/python scripts/order/morning_holdings_brief.py --write
  .venv-fubon/bin/python scripts/order/morning_holdings_brief.py --date 2026-06-26 --simulate --write --notify
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from order.fubon_session import check_python_version  # noqa: E402
from order.morning_holdings_brief import (  # noqa: E402
    build_morning_brief,
    default_report_path,
    format_morning_brief,
    sync_morning_brief_to_db,
    write_morning_brief,
)
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _send_notify(*, session_date: str, text: str, exit_code: int) -> None:
    stamp = session_date.replace("-", "")
    log_dir = ROOT / "logs" / "intraday"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"morning_holdings_brief_{stamp}.log"
    log_path.write_text(text + "\n", encoding="utf-8")
    env = dict(**{k: v for k, v in __import__("os").environ.items()})
    env["ROOT"] = str(ROOT)
    env["JOB_NOTIFY_EXTRA"] = text
    subprocess.run(
        [str(ROOT / "scripts" / "morning_holdings_notify.sh"), str(exit_code), stamp],
        cwd=ROOT,
        env=env,
        check=False,
    )


def main() -> int:
    try:
        check_python_version()
    except RuntimeError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2

    load_project_dotenv()

    parser = argparse.ArgumentParser(description="開盤前持倉 brief")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="Session date（預設今日台北）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--no-fubon", action="store_true", help="略過富邦 API")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="PIT 復盤：結構截至 session 開盤前；不拉即時期貨",
    )
    parser.add_argument(
        "--sync-futures",
        action="store_true",
        help="拉 FinMind 期貨 snapshot 並寫入 morning_risk_snapshot",
    )
    parser.add_argument(
        "--no-futures",
        action="store_true",
        help="不嘗試刷新台指 gap（僅讀 DB 最新）",
    )
    parser.add_argument(
        "--sync-db",
        action="store_true",
        help="寫入 order_holdings_snapshot（需有富邦持倉）",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="寫入 reports/order/snapshots/morning_brief_YYYYMMDD.md",
    )
    parser.add_argument("-o", "--output", type=Path, help="自訂輸出路徑（需搭配 --write）")
    parser.add_argument(
        "--notify",
        action="store_true",
        help="寄送通知信（需 RUN_MORNING_HOLDINGS_EMAIL=1 · GMAIL_*）",
    )
    args = parser.parse_args()

    simulate = args.simulate or bool(
        args.date and args.date != datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat()
    )

    conn = connect(args.db)
    try:
        brief = build_morning_brief(
            conn,
            session_date=args.date,
            use_fubon=not args.no_fubon,
            sync_futures=args.sync_futures and not simulate,
            refresh_futures=not args.no_futures and not simulate,
            simulate=simulate,
        )
        if args.sync_db and brief.holdings:
            n = sync_morning_brief_to_db(conn, brief)
            print(f"\norder_holdings_snapshot：{n} 檔 · {brief.session_date}")
    finally:
        conn.close()

    text = format_morning_brief(brief)
    print(text)

    out_path: Path | None = None
    if args.write:
        out_path = write_morning_brief(brief, args.output)
        print(f"\n已寫入：{out_path}")
    elif args.notify:
        out_path = default_report_path(brief.session_date)
        write_morning_brief(brief, out_path)

    exit_code = 1 if brief.fubon_error and not args.no_fubon else 0

    if args.notify:
        if out_path is None:
            out_path = default_report_path(brief.session_date)
            write_morning_brief(brief, out_path)
        _send_notify(session_date=brief.session_date, text=text, exit_code=exit_code)
        print(f"\n已寄信（若 GMAIL 已設定）· 報告：{out_path}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
