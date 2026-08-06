#!/usr/bin/env python3
"""Materialize TX 1m JSON caches → $GOLDENSTOCKS_DATA_DIR/cache/tmf_channel/bars.sqlite."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from project_dotenv import load_project_dotenv  # noqa: E402

load_project_dotenv()

from tmf_channel.cache_store import materialize_json_to_sqlite  # noqa: E402


def main() -> int:
    stats = materialize_json_to_sqlite()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
