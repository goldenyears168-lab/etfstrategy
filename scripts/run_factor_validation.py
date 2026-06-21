#!/usr/bin/env python3
"""Factor validation layer (alphalens-style) — config/factor_validation.yaml."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

runpy.run_path(str(SRC / "factor_validation.py"), run_name="__main__")
