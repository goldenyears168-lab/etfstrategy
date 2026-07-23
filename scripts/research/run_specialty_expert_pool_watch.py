#!/usr/bin/env python3
"""Thin wrapper → run_expert_pool_watch.py（預設 8046+8358；相容舊路徑）."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[2] / "scripts" / "research" / "run_expert_pool_watch.py"
extra = sys.argv[1:]
if "--stock" in sys.argv:
    raise SystemExit(subprocess.call([sys.executable, str(TARGET), *extra]))
rc = 0
for sid in ("8046", "8358"):
    rc = rc or subprocess.call([sys.executable, str(TARGET), "--stock", sid, *extra])
raise SystemExit(rc)
