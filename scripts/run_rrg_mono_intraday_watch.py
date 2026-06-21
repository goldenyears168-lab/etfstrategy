#!/usr/bin/env python3
"""RRG mono 收盤前預警（13:00 · D4 收盤進場候選）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rrg_mono_intraday_watch import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
