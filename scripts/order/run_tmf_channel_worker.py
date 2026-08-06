#!/usr/bin/env python3
"""TMF micro-channel long-lived Order worker · .venv-fubon only.

Production path: launchd ``com.jackm4.goldenstocks.tmf-channel-poll`` → this
script (KeepAlive). One-shot debug::

  .venv-fubon/bin/python scripts/order/run_tmf_channel_poll.py --json
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from project_dotenv import load_project_dotenv  # noqa: E402

load_project_dotenv()

from tmf_channel.worker_loop import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
