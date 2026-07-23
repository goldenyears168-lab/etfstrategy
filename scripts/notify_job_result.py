#!/usr/bin/env python3
"""Send job completion email (Gmail SMTP → Mail.app inbox).

Optional parallel wall: when RUN_OPS_DIGEST_SYNC=1 (default) and Supabase
ops credentials exist, also upsert ``ops.digests`` — does not replace email.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from project_dotenv import load_project_dotenv
from notify_email import send_job_result

_TPE = ZoneInfo("Asia/Taipei")


def _maybe_ops_digest(
    *,
    subject_prefix: str,
    exit_code: int,
    log_path: Path,
    extra: str,
) -> None:
    if os.environ.get("RUN_OPS_DIGEST_SYNC", "1").strip() in ("0", "false", "False"):
        return
    try:
        from ops_console_sync import supabase_ops_configured, upsert_digest
    except Exception as exc:  # noqa: BLE001
        print(f"ops digest import skip: {exc}", file=sys.stderr)
        return
    if not supabase_ops_configured():
        return

    status = "成功" if exit_code == 0 else "失敗"
    subject = f"[股票研究] {subject_prefix} · {status}"
    day = datetime.now(_TPE).date().isoformat()
    key_src = f"{day}|{subject_prefix}|{exit_code}"
    digest_key = f"job:{hashlib.sha1(key_src.encode()).hexdigest()[:16]}"

    parts = [f"**{subject_prefix}** · {status}", ""]
    if extra.strip():
        parts.extend([extra.strip(), ""])
    path = Path(log_path)
    if path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
        tail = text[-3000:] if len(text) > 3000 else text
        if tail.strip():
            parts.extend(["```", tail, "```"])
    severity = "info" if exit_code == 0 else "warn"
    low = subject_prefix.lower()
    if "red" in low or "detach" in low or "拒單" in subject_prefix:
        severity = "red" if exit_code != 0 else "major"

    try:
        upsert_digest(
            digest_key=digest_key,
            body_md="\n".join(parts),
            subject=subject,
            channel="email",
            severity=severity,
            meta={
                "subject_prefix": subject_prefix,
                "exit_code": exit_code,
                "log_path": str(path),
            },
        )
        print(f"ops.digests ok · {digest_key}")
    except Exception as exc:  # noqa: BLE001 — never block email path
        print(f"ops digest skip: {exc}", file=sys.stderr)


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
        if os.environ.get(args.env_flag, "1").strip() in ("0", "false", "False"):
            return 0

    # Wall first (best-effort) so email failure still leaves a trail when possible
    _maybe_ops_digest(
        subject_prefix=args.subject_prefix,
        exit_code=args.exit_code,
        log_path=args.log_path,
        extra=args.extra,
    )

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
