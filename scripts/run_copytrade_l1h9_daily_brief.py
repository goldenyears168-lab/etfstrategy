#!/usr/bin/env python3
"""00981A L1H9 · 收盤後每日篩選 brief。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from copytrade_l1h9_daily import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
