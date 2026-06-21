"""operational_brief：隔日 checklist。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from operational_brief import (
    build_morning_checklist_items,
    build_operational_brief_block,
)
from score_engine import SCORE_VERSION
from stock_db import connect, upsert_pm_watchlist


class TestOperationalBrief(unittest.TestCase):
    def test_build_operational_brief_block_has_checklist_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            block = build_operational_brief_block(conn, ("00981A",))
            conn.close()
        self.assertIn("next_day_checklist", block)
        self.assertNotIn("news_verify", block)

    def test_morning_checklist_from_pm_watchlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            upsert_pm_watchlist(
                conn,
                [
                    {
                        "stock_id": "3008",
                        "as_of_date": "2026-06-04",
                        "score_version": SCORE_VERSION,
                        "stock_name": "大立光",
                        "investment_score": 70.0,
                        "watchlist": "一般觀察",
                        "entry_signal": "突破",
                        "entry_tags_json": "[]",
                        "chip_tag": "外資、投信同步買超",
                        "pm_bucket": "突破",
                        "flow_score": 74.0,
                        "chip_score": 80.0,
                        "tech_score": 88.0,
                        "catalyst_score": 45.0,
                        "fundamental_score": 55.0,
                        "note": "",
                    }
                ],
            )
            items = build_morning_checklist_items(conn, ("00981A",))
            conn.close()
        texts = [it.text for it in items]
        self.assertTrue(any("3008" in t for t in texts))
        self.assertTrue(any("名單基準日" in t for t in texts))


if __name__ == "__main__":
    unittest.main()
