#!/usr/bin/env python3
"""C18acc extension overlay · 獨立盤中 screen。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from c18acc_extension_screen import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
