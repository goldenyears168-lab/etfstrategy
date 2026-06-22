#!/usr/bin/env python3
"""Deprecated wrapper — use scripts/order/fubon_login_test.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "order" / "fubon_login_test.py"
raise SystemExit(subprocess.call([sys.executable, str(_TARGET), *sys.argv[1:]]))
