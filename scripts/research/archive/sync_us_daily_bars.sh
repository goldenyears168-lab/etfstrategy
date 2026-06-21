#!/usr/bin/env python3
"""Sync 金标准 universe 美股日 K → stocks.db us_daily_bars。"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
VENV_PY = ROOT / ".venv" / "bin" / "python"
PYTHON = str(VENV_PY if VENV_PY.is_file() else sys.executable)

if __name__ == "__main__":
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    cmd = [
        PYTHON,
        str(SRC / "research" / "archive" / "vcp_calibration" / "vcp_us_literature_audit.py"),
        "--sync-db",
        "--use-db",
        *sys.argv[1:],
    ]
    raise SystemExit(subprocess.call(cmd, cwd=str(ROOT), env=env))
