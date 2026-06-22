#!/usr/bin/env python3
"""Fubon Neo API 連線測試（Order layer smoke test）。

請用 Python 3.13 專用 venv：
  .venv-fubon/bin/python scripts/order/fubon_login_test.py [--realtime] [--snapshot]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from order.fubon_session import account_label, check_python_version, connect_fubon  # noqa: E402


def main() -> int:
    try:
        check_python_version()
    except RuntimeError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser(description="Fubon Neo API login smoke test")
    parser.add_argument("--realtime", action="store_true", help="登入後 init_realtime()")
    parser.add_argument(
        "--snapshot", action="store_true", help="登入後輸出 account_snapshot JSON"
    )
    args = parser.parse_args()

    try:
        session = connect_fubon(realtime=args.realtime)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

    print("isSuccess: True")
    for i, acc in enumerate(session.accounts):
        print(f"  account[{i}]: {account_label(acc)}")

    if args.realtime and not args.snapshot:
        print("init_realtime(): OK")

    if args.snapshot:
        from order.fubon_account import account_snapshot

        snap = account_snapshot(session)
        print(json.dumps(snap, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
