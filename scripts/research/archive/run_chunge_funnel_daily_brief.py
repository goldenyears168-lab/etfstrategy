#!/usr/bin/env python3
"""春哥漏斗 VCP 研究 daily brief 入口。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from chunge_funnel_screen import run_chunge_funnel_screen, write_chunge_funnel_brief  # noqa: E402
from stock_db import connect  # noqa: E402


def main() -> int:
    conn = connect(ROOT / "data" / "stocks.db")
    try:
        as_of, results, layer_counts, cfg = run_chunge_funnel_screen(conn)
        if not as_of:
            print("Chunge funnel: 略過（無足夠資料）")
            return 0
        path = write_chunge_funnel_brief(
            conn,
            as_of_date=as_of,
            results=results,
            layer_counts=layer_counts,
            params=cfg,
        )
    finally:
        conn.close()
    print(f"Chunge funnel daily brief → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
