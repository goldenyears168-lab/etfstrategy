#!/usr/bin/env python3
"""Minervini SEPA basket · 月末調倉 brief。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from minervini_sepa_daily import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
