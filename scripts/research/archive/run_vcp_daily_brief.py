#!/usr/bin/env python3
"""VCP 研究 daily brief 入口。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from stock_db import connect  # noqa: E402
from vcp_screen import run_vcp_screen, write_vcp_brief  # noqa: E402


def main() -> int:
    conn = connect(ROOT / "data" / "stocks.db")
    try:
        as_of, candidates = run_vcp_screen(conn)
        if not as_of:
            print("VCP: 略過（無足夠資料）")
            return 0
        path = write_vcp_brief(conn, as_of_date=as_of, candidates=candidates)
    finally:
        conn.close()
    print(f"VCP daily brief → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
