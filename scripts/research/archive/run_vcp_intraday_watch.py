#!/usr/bin/env python3
"""VCP 盤中 watchlist 入口。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from vcp_intraday_watch import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
