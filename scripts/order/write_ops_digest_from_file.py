#!/usr/bin/env python3
"""Write ops.digests from a markdown/text file（並行上牆 · 不取代 email）.

Examples:
  PYTHONPATH=src .venv/bin/python scripts/order/write_ops_digest_from_file.py \\
    --key evening-watch-2026-07-23 --subject "夜間觀測" --file reports/daily/foo.md
  PYTHONPATH=src .venv/bin/python scripts/order/write_ops_digest_from_file.py \\
    --key smoke --subject "ops smoke" --body "hello" --severity info
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from project_dotenv import load_project_dotenv  # noqa: E402
from ops_console_sync import supabase_ops_configured, upsert_digest  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upsert ops.digests from file/body")
    parser.add_argument("--key", default="", help="digest_key · default hash of subject+body")
    parser.add_argument("--subject", default="")
    parser.add_argument("--file", type=Path, default=None)
    parser.add_argument("--body", default="")
    parser.add_argument("--channel", default="email")
    parser.add_argument("--severity", default="info", choices=("info", "warn", "major", "red"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    load_project_dotenv()
    body = args.body
    if args.file is not None:
        path = args.file if args.file.is_absolute() else ROOT / args.file
        if not path.is_file():
            print(f"錯誤：檔案不存在 {path}", file=sys.stderr)
            return 1
        body = path.read_text(encoding="utf-8", errors="replace")
    if not body.strip():
        print("錯誤：需要 --file 或 --body", file=sys.stderr)
        return 1

    key = args.key.strip()
    if not key:
        digest = hashlib.sha1(f"{args.subject}\n{body[:2000]}".encode()).hexdigest()[:16]
        key = f"manual:{digest}"

    if args.dry_run:
        print(f"dry-run digest_key={key} subject={args.subject!r} bytes={len(body)}")
        return 0

    if not supabase_ops_configured():
        print("錯誤：SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 未設定", file=sys.stderr)
        return 2

    upsert_digest(
        digest_key=key,
        body_md=body,
        subject=args.subject or key,
        channel=args.channel,
        severity=args.severity,
        meta={"source": "write_ops_digest_from_file"},
    )
    print(f"ops.digests ok · {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
