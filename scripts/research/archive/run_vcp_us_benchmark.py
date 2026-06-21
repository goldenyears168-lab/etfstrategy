#!/usr/bin/env python3
"""美股 VCP 金标准 walk-forward（读 config/vcp_us_cases.yaml）。"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
VENV_PY = ROOT / ".venv" / "bin" / "python"
PYTHON = str(VENV_PY if VENV_PY.is_file() else sys.executable)

if __name__ == "__main__":
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    cmd = [PYTHON, str(SRC / "research" / "archive" / "vcp_calibration" / "vcp_us_benchmark.py"), *sys.argv[1:]]
    raise SystemExit(subprocess.call(cmd, cwd=str(ROOT), env=env))
