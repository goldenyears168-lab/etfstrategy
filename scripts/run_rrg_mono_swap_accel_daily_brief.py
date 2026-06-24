#!/usr/bin/env python3
"""Wrapper: RRG mono swap-accel（C18acc）16:30 收盤診斷 brief（Scheme A）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rrg_mono_swap_accel_daily_brief import main

if __name__ == "__main__":
    raise SystemExit(main())
