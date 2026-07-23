#!/usr/bin/env python3
"""Thin wrapper → run_expert_pool_watch.py --stock 2344（相容舊路徑）."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[2] / "scripts" / "research" / "run_expert_pool_watch.py"
cmd = [sys.executable, str(TARGET), *sys.argv[1:]]
if "--stock" not in sys.argv:
    cmd[2:2] = ["--stock", "2344"]
raise SystemExit(subprocess.call(cmd))
