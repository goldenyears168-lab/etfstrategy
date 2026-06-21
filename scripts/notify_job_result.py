#!/usr/bin/env python3
"""Send job completion email (Gmail SMTP → Mail.app inbox)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from project_dotenv import load_project_dotenv
from notify_email import send_job_result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send job completion email")
    parser.add_argument("--subject-prefix", required=True)
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--log-path", type=Path, required=True)
    parser.add_argument("--extra", default="")
    parser.add_argument(
        "--env-flag",
        default="",
        help="Skip when env var is 0 (e.g. RUN_EVENING_HOLDINGS_EMAIL)",
    )
    args = parser.parse_args(argv)

    load_project_dotenv()
    if args.env_flag:
        import os

        if os.environ.get(args.env_flag, "1").strip() in ("0", "false", "False"):
            return 0

    try:
        send_job_result(
            subject_prefix=args.subject_prefix,
            success=args.exit_code == 0,
            log_path=args.log_path,
            extra=args.extra,
        )
        return 0
    except Exception as exc:
        print(f"notify failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
