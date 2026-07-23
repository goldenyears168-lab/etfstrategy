#!/usr/bin/env python3
"""stock_kbar_1m → stock_kbar_5m · 主線歷史遷移（單次或增量）。

  PYTHONPATH=src python scripts/tools/backfill_kbar_5m_from_1m.py --report-only
  PYTHONPATH=src python scripts/tools/backfill_kbar_5m_from_1m.py --date-start 2024-01-01
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402
from stock_db.kbar import backfill_stock_kbar_5m_from_1m, kbar_5m_backfill_complete  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill stock_kbar_5m from stock_kbar_1m")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default=None)
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        q = """
            SELECT DISTINCT stock_id, trade_date
            FROM stock_kbar_1m
            WHERE trade_date >= ?
        """
        params: list[str] = [args.date_start]
        if args.date_end:
            q += " AND trade_date <= ?"
            params.append(args.date_end)
        q += " ORDER BY trade_date, stock_id"
        pairs = conn.execute(q, params).fetchall()
        missing = [
            (str(r[0]), str(r[1]))
            for r in pairs
            if not kbar_5m_backfill_complete(conn, str(r[0]), str(r[1]))
        ]
        print(f"pairs_with_1m={len(pairs)} missing_5m={len(missing)}")
        if args.report_only:
            return 0
        n = 0
        bars = 0
        for sid, td in missing:
            bars += backfill_stock_kbar_5m_from_1m(conn, sid, td)
            n += 1
            if n % 500 == 0:
                print(f"  ... {n}/{len(missing)} days upserted={bars}")
        print(f"done sessions={n} bars_upserted={bars}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
