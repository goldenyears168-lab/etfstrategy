"""etf_entry_ta_study：獨立 DB 技術面分析。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from research.archive.etf_entry_ta_study import STUDY_EVENTS, analyze_event, connect_study, permutation_pvalue, upsert_events


class EtfEntryTaStudyTests(unittest.TestCase):
    def test_analyze_with_synthetic_bars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "study.db"
            with connect_study(db) as conn:
                upsert_events(conn)
                ev = STUDY_EVENTS[0]
                start = date(2026, 1, 1)
                price = 100.0
                for i in range(120):
                    td = (start + timedelta(days=i)).isoformat()
                    if td > ev.event_date:
                        break
                    price *= 1.003
                    conn.execute(
                        """
                        INSERT INTO study_daily_bars (
                            stock_id, trade_date, open, high, low, close, volume
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (ev.stock_id, td, price, price * 1.01, price * 0.99, price, 1000),
                    )
                conn.commit()
                row = conn.execute(
                    "SELECT id FROM study_events WHERE event_date=? AND stock_id=?",
                    (ev.event_date, ev.stock_id),
                ).fetchone()
                self.assertIsNotNone(row)
                result = analyze_event(conn, row["id"], ev.event_date, ev.stock_id, 12.0)
                self.assertIsNotNone(result)
                snap = conn.execute(
                    "SELECT * FROM study_ta_snapshot WHERE event_id=?",
                    (row["id"],),
                ).fetchone()
                self.assertIsNotNone(snap)
                self.assertIsNotNone(snap["return_2w_pct"])
                self.assertIn(snap["entry_pattern"], ("拉回", "觀望", "乖離過大", "突破"))

    def test_permutation_pvalue_extreme_groups(self) -> None:
        # 明顯分離的兩組應有低 p 值
        a = [10.0, 11.0, 12.0, 9.0]
        b = [1.0, 2.0, 0.5, 1.5]
        p = permutation_pvalue(a, b, iterations=2000)
        self.assertLess(p, 0.05)

    def test_permutation_pvalue_similar_groups(self) -> None:
        a = [1.0, 2.0, 1.5]
        b = [1.2, 1.8, 2.1]
        p = permutation_pvalue(a, b, iterations=2000)
        self.assertGreater(p, 0.05)


if __name__ == "__main__":
    unittest.main()
