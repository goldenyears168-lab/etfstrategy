#!/usr/bin/env python3
"""VCP 策略回測入口（v1 · chunge-funnel · 盤中）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.archive.vcp_calibration.vcp_strategy_benchmark import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
