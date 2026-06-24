#!/usr/bin/env python3
"""Wrapper: RRG mono swap-accel（C18acc）live screen · 5m poll entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rrg_mono_swap_accel_screen import main

if __name__ == "__main__":
    raise SystemExit(main())
