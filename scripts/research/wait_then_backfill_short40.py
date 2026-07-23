#!/usr/bin/env python3
"""Wait until no other backfill_broker_branch_tape is running, then fill ABC short40."""
from __future__ import annotations

import datetime as dt
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def other_backfills() -> list[str]:
    out = subprocess.check_output(["ps", "aux"], text=True)
    lines = []
    for ln in out.splitlines():
        if "backfill_broker_branch_tape.py" not in ln:
            continue
        if "wait_then_backfill_short40" in ln:
            continue
        if "_abc_short40" in ln:
            continue
        lines.append(ln)
    return lines


def main() -> int:
    print(f"[{dt.datetime.now()}] short40 waiter start", flush=True)
    while True:
        others = other_backfills()
        if not others:
            break
        print(f"[{dt.datetime.now()}] waiting other backfills n={len(others)}", flush=True)
        time.sleep(180)
    cmd = [
        str(ROOT / ".venv/bin/python"),
        "-u",
        str(ROOT / "scripts/research/backfill_broker_branch_tape.py"),
        "--universe-json",
        str(ROOT / "reports/research/branch-footprint-screen/_abc_short40_universe.json"),
        "--start",
        "2024-07-01",
        "--end",
        "2026-07-17",
        "--progress",
        str(ROOT / "reports/research/branch-footprint-screen/_abc_short40_2y_progress.json"),
        "--delay",
        "0.55",
        "--fetch-workers",
        "1",
    ]
    print(f"[{dt.datetime.now()}] starting: {' '.join(cmd)}", flush=True)
    return subprocess.call(cmd, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
