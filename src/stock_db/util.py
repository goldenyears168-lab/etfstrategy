"""Paths and small helpers shared across stock_db submodules."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# GOLDENSTOCKS_DATA_DIR lets the mutable state root (.env, data/, logs/) live
# outside the git working tree (e.g. ~/goldenstocks-data on mini) instead of
# always being ${PROJECT_ROOT}. Unset = unchanged historical behavior (data/
# as a sibling of src/ inside the repo).
_STATE_ROOT = (
    Path(os.environ["GOLDENSTOCKS_DATA_DIR"])
    if os.environ.get("GOLDENSTOCKS_DATA_DIR")
    else PROJECT_ROOT
)
DATA_DIR = _STATE_ROOT / "data"
LOGS_DIR = _STATE_ROOT / "logs"
DEFAULT_DB_PATH = DATA_DIR / "stocks.db"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
