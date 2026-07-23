#!/usr/bin/env python3
"""ABC v3+F1 position poll · RETIRED from Order layer (2026-07-15).

Live Order sleeves: C18acc + Leading Dip. This entrypoint is a no-op and
does not submit. Library modules under src/order/abc_v3_f1_* remain for
historical ledger reads / unit tests / C18acc shared helpers.

  PYTHONPATH=src python3 scripts/order/run_abc_v3_f1_position_poll.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Taipei")


def main() -> int:
    now = datetime.now(tz=_TZ)
    payload = {
        "status": "retired",
        "reason": "ABC removed from Order layer 2026-07-15 · use Leading Dip / C18acc",
        "session_date": now.strftime("%Y-%m-%d"),
        "poll_minute": now.strftime("%H:%M"),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
