#!/usr/bin/env python3
"""收盤後 Supabase 公開層健康檢查 · Readdy publish 是否 stale。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from project_dotenv import load_project_dotenv
from supabase_health_check import run_cli


def main(argv: list[str] | None = None) -> int:
    load_project_dotenv()
    parser = argparse.ArgumentParser(description="Supabase 公開層健康檢查")
    parser.add_argument("--date", help="trade_date YYYY-MM-DD（預設：最近交易日）")
    parser.add_argument(
        "--skip-1300",
        action="store_true",
        help="略過 13:00 brief 檢查（僅驗 16:30 收盤列）",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="FAIL 時 macOS 通知",
    )
    args = parser.parse_args(argv)
    return run_cli(
        trade_date=args.date,
        check_1300=not args.skip_1300,
        notify=args.notify,
    )


if __name__ == "__main__":
    sys.exit(main())
