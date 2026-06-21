#!/usr/bin/env python3
"""春哥漏斗 L4 参数网格校准。"""

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
    cmd = [PYTHON, str(SRC / "research" / "archive" / "chunge_l4_calibration.py"), *sys.argv[1:]]
    raise SystemExit(subprocess.call(cmd, cwd=str(ROOT), env=env))
