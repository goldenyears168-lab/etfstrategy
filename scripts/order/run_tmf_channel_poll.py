#!/usr/bin/env python3
"""TMF micro-channel Order poll · must run under .venv-fubon.

Examples:
  .venv-fubon/bin/python scripts/order/run_tmf_channel_poll.py --json
  # --force is refused unless explicitly unlocked (do not dual-run with launchd):
  ORDER_TMF_CHANNEL_FORCE_OK=1 ORDER_TMF_CHANNEL_DRY_RUN=1 \\
    .venv-fubon/bin/python scripts/order/run_tmf_channel_poll.py --force --json

Live submit requires ALL of:
  ORDER_MASTER_ENABLED=1
  ORDER_TMF_CHANNEL_ENABLED=1
  ORDER_TMF_CHANNEL_AUTO_SUBMIT=1
  ORDER_TMF_CHANNEL_DRY_RUN=0
and launchd job ``com.jackm4.goldenstocks.tmf-channel-poll`` enabled
(``scripts/install-launchd.sh`` installs + enables it).

Production path is **launchd only** — never nohup/night_loop alongside it.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from project_dotenv import load_project_dotenv  # noqa: E402

load_project_dotenv()

from order.tmf_channel_order import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
