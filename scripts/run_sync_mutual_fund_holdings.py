#!/usr/bin/env python3
"""Wrapper: sync mutual fund holdings (SITCA backfill + MoneyDJ latest)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from sync_mutual_fund_holdings import main

if __name__ == "__main__":
    raise SystemExit(main())
