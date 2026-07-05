#!/usr/bin/env python3
"""11:00 盤中持倉脈動 · 合併寄信。"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from intraday_holdings_digest import fetch_holdings_pulse_digest  # noqa: E402
from intraday_midday_digest import (  # noqa: E402
    format_intraday_midday_digest,
    load_midday_digest_input,
    write_midday_digest,
)
from project_dotenv import load_project_dotenv  # noqa: E402


def _send_notify(*, session_date: str, text: str, exit_code: int) -> None:
    stamp = session_date.replace("-", "")
    log_dir = ROOT / "logs" / "intraday"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"intraday_midday_digest_{stamp}.log"
    log_path.write_text(text + "\n", encoding="utf-8")
    env = dict(os.environ)
    env["ROOT"] = str(ROOT)
    env["JOB_NOTIFY_EXTRA"] = text
    subprocess.run(
        [str(ROOT / "scripts" / "intraday_midday_digest_notify.sh"), str(exit_code), stamp],
        cwd=ROOT,
        env=env,
        check=False,
    )


def main(argv: list[str] | None = None) -> int:
    load_project_dotenv()

    parser = argparse.ArgumentParser(description="11:00 盤中持倉脈動")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="Session date（預設今日）")
    parser.add_argument("--write", action="store_true", help="寫入 reports/daily/")
    parser.add_argument("--notify", action="store_true", help="寄信（需 RUN_INTRADAY_MIDDAY_DIGEST_EMAIL=1）")
    args = parser.parse_args(argv)

    if os.environ.get("RUN_INTRADAY_MIDDAY_DIGEST", "1").strip() in ("0", "false", "False"):
        print("SKIP: RUN_INTRADAY_MIDDAY_DIGEST=0")
        return 0

    session = args.date or date.today().isoformat()
    inp = load_midday_digest_input(session_date=session)
    holdings_digest, holdings_error = fetch_holdings_pulse_digest(session_date=session)
    text = format_intraday_midday_digest(
        inp,
        holdings_digest=holdings_digest,
        holdings_error=holdings_error,
    )
    print(text)

    if args.write or args.notify:
        out_path = write_midday_digest(text, session_date=session)
        print(f"\nWrote {out_path}")

    if args.notify:
        _send_notify(session_date=session, text=text, exit_code=0)
        print("notify dispatched (if RUN_INTRADAY_MIDDAY_DIGEST_EMAIL=1)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
