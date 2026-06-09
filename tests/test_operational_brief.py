"""Tests for operational_brief (news verify + morning checklist)."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from event_ranking import load_all_catalyst_events, manual_events_enabled
from operational_brief import (
    build_news_verify_items,
    google_search_url,
    yahoo_tw_news_url,
)
from stock_db import connect, upsert_pm_watchlist


class TestOperationalBrief(unittest.TestCase):
    def test_yahoo_url(self) -> None:
        self.assertIn("2330.TW", yahoo_tw_news_url("2330"))

    def test_google_search_url(self) -> None:
        url = google_search_url("台積電 2330 法說")
        self.assertIn("google.com/search", url)

    def test_manual_events_disabled_by_default(self) -> None:
        old = os.environ.get("USE_MANUAL_EVENTS")
        os.environ["USE_MANUAL_EVENTS"] = "0"
        try:
            self.assertFalse(manual_events_enabled())
            events = load_all_catalyst_events(None)
            self.assertEqual(events, [])
        finally:
            if old is None:
                os.environ.pop("USE_MANUAL_EVENTS", None)
            else:
                os.environ["USE_MANUAL_EVENTS"] = old

    def test_build_news_verify_flags_low_catalyst(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            upsert_pm_watchlist(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "as_of_date": "2026-06-04",
                        "score_version": "p4-v2",
                        "stock_name": "台積電",
                        "investment_score": 82.0,
                        "watchlist": "首要觀察",
                        "entry_signal": "突破",
                        "entry_tags_json": "[]",
                        "chip_tag": "外資、投信同步買超",
                        "pm_bucket": "突破",
                        "flow_score": 85.0,
                        "chip_score": 90.0,
                        "tech_score": 70.0,
                        "catalyst_score": 40.0,
                        "fundamental_score": 55.0,
                        "note": "",
                    }
                ],
            )
            items = build_news_verify_items(conn, ("00981A",))
            conn.close()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].stock_id, "2330")
        self.assertIn("2330", items[0].yahoo_news_url)


if __name__ == "__main__":
    unittest.main()
