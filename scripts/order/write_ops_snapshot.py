#!/usr/bin/env python3
"""Write reports/order/snapshots/*_ops.json（唯讀 ops 健康快照）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from order.ops_snapshot import write_order_ops_snapshot  # noqa: E402


def main() -> int:
    path = write_order_ops_snapshot()
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
