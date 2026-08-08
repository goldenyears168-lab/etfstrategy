#!/usr/bin/env python3
"""dayflip-futures-short long-lived Order worker.

Production path: launchd ``com.jackm4.goldenstocks.dayflip-short-poll`` → this
script (KeepAlive). One-shot debug::

  .venv/bin/python scripts/order/run_dayflip_short_worker.py --once
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts/research"))

from project_dotenv import load_project_dotenv  # noqa: E402

load_project_dotenv()

from order.dayflip_short_worker_loop import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
