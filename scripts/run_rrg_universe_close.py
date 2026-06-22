#!/usr/bin/env python3
"""RRG universe close snapshot → SQLite + optional Supabase."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from project_dotenv import load_project_dotenv
from rrg_universe_snapshot import run_close_universe_snapshot
from stock_db import connect
from supabase_rrg_universe_sync import maybe_sync_rrg_universe_to_supabase


def main(argv: list[str] | None = None) -> int:
    load_project_dotenv()
    conn = connect()
    try:
        n, session = run_close_universe_snapshot(conn)
        if not session:
            print("RRG universe close: skipped（無足夠當日 K 線）")
            return 0
        print(f"RRG universe close: session={session} rows={n}")
        synced = maybe_sync_rrg_universe_to_supabase(conn, session, "close")
        if synced is not None:
            print(f"RRG universe close Supabase: rows={synced}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
