#!/usr/bin/env python3
"""VCP Pivot Gate / Coil Close · 13:00 盤中 daily brief。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from vcp_funnel_specs_daily import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
