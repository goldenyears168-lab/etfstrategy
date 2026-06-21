#!/usr/bin/env python3
"""Wrapper: RRG mono + seg_last 每日掃描（D4 收盤進場 / D11 收盤出場）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rrg_mono_daily_brief import main

if __name__ == "__main__":
    raise SystemExit(main())
