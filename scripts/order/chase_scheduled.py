#!/usr/bin/env python3
"""開盤窗追價：每分鐘一輪 · 限價追賣一 · 最多 5 輪（由 launchd 9:00–9:04 觸發）。

只處理 user_def=chase_open 的程式單；不撤銷人工掛單。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from project_dotenv import load_project_dotenv  # noqa: E402
from order.chase_runner import run_chase_from_spec_file  # noqa: E402
from order.fubon_session import check_python_version  # noqa: E402


def main() -> int:
    load_project_dotenv()
    try:
        check_python_version()
    except RuntimeError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser(description="Intraday odd chase · limit at ask")
    parser.add_argument(
        "spec_file",
        nargs="?",
        default=str(ROOT / "reports/order/intents/scheduled/open_market_10000.json"),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if os.environ.get("ORDER_LAUNCHD_ENABLED", "").strip() != "1":
        print("⚠ ORDER_LAUNCHD_ENABLED≠1，略過追價", file=sys.stderr)
        return 0

    try:
        payload = run_chase_from_spec_file(args.spec_file, dry_run=bool(args.dry_run))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
