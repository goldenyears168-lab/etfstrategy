#!/usr/bin/env python3
"""Compare Fubon holdings vs local ledgers · report only."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from order.broker_reconcile import format_reconcile_plain, write_reconcile_report  # noqa: E402
from order.fubon_session import check_python_version, connect_fubon  # noqa: E402


def main() -> int:
    try:
        check_python_version()
        session = connect_fubon()
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    path = write_reconcile_report(session)
    report = json.loads(path.read_text(encoding="utf-8"))
    print(format_reconcile_plain(report))
    print(f"wrote {path}", file=sys.stderr)
    print(f"wrote {path.with_suffix('.md')}", file=sys.stderr)
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
