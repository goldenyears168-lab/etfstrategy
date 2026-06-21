"""analytics.bench：IX0001 基準價與超額檢定。"""

from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from analytics.bench import (
    bench_close,
    bench_return_entry_to_exit,
    compute_excess_significance,
)
from stock_db import connect


def _seed_bench(conn: sqlite3.Connection) -> None:
    synced = "2026-06-01T00:00:00Z"
    conn.executemany(
        """
        INSERT INTO daily_bars (code, date, open, high, low, close, volume, source, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, 1000, 'tej', ?)
        """,
        [
            ("IX0001", "2026-06-02", 100.0, 101.0, 99.0, 100.0, synced),
            ("IX0001", "2026-06-03", 102.0, 105.0, 101.0, 104.0, synced),
            ("IX0001", "2026-06-04", 104.0, 109.0, 103.0, 108.0, synced),
        ],
    )
    conn.commit()


@dataclass
class _Day:
    status: str
    return_pct: float
    bench_return_pct: float


class TestAnalyticsBench(unittest.TestCase):
    def test_bench_return_open_to_close(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        conn = connect(Path(tmp.name))
        try:
            _seed_bench(conn)
            self.assertAlmostEqual(
                bench_return_entry_to_exit(
                    conn, "2026-06-03", "2026-06-04", entry_price_mode="open"
                ),
                (108.0 - 102.0) / 102.0 * 100.0,
            )
            self.assertAlmostEqual(bench_close(conn, "2026-06-04"), 108.0)
        finally:
            conn.close()

    def test_compute_excess_significance_duck_type(self) -> None:
        if importlib.util.find_spec("scipy") is None:
            self.skipTest("scipy not installed")
        rows = [
            _Day("complete", 2.0, 1.0),
            _Day("complete", 0.5, 1.0),
            _Day("complete", 1.5, 1.0),
            _Day("skip", 99.0, 0.0),
        ]
        sig = compute_excess_significance(rows)
        self.assertAlmostEqual(sig["mean_excess_pct"], 0.3333, places=4)
        self.assertIsNotNone(sig["t_stat"])


if __name__ == "__main__":
    unittest.main()
