"""Perplexity 新聞同步：解析與正規化（mock API）。"""

from __future__ import annotations

import json
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from event_ranking import CatalystEvent
from sync_catalyst_news import (
    _dedupe_max_two,
    _normalize_event,
    _parse_events_payload,
    fetch_perplexity_events,
    sync_news_for_universe,
)


class TestParseEventsPayload(unittest.TestCase):
    def test_json_codeblock(self) -> None:
        raw = '```json\n{"events":[{"stock_id":"2330","event_date":"2026-06-01"}]}\n```'
        items = _parse_events_payload(raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["stock_id"], "2330")


class TestNormalizeEvent(unittest.TestCase):
    def test_valid_event(self) -> None:
        today = date.today().isoformat()
        ev = _normalize_event(
            {
                "stock_id": "2330",
                "event_date": today,
                "catalyst_type": "EARNINGS",
                "headline": "法說優於預期",
                "polarity": "BULL",
                "explains_etf_add": "HIGH",
                "confidence": 70,
                "sources": [{"title": "新聞", "date": today, "url": "https://x"}],
            },
            allowed_ids={"2330"},
        )
        self.assertIsNotNone(ev)
        assert ev is not None
        self.assertEqual(ev.stock_id, "2330")

    def test_rejects_rating_words(self) -> None:
        ev = _normalize_event(
            {
                "stock_id": "2330",
                "event_date": date.today().isoformat(),
                "headline": "建議買進",
            },
            allowed_ids={"2330"},
        )
        self.assertIsNone(ev)


class TestDedupeMaxTwo(unittest.TestCase):
    def test_caps_per_stock(self) -> None:
        d = date.today()
        events = [
            CatalystEvent("2330", d, "EARNINGS", "a", confidence=90),
            CatalystEvent("2330", d - timedelta(days=1), "CAPX", "b", confidence=80),
            CatalystEvent("2330", d - timedelta(days=2), "POLICY", "c", confidence=70),
        ]
        out = _dedupe_max_two(events)
        self.assertEqual(len(out), 2)
        self.assertTrue(all(e.stock_id == "2330" for e in out))


class TestFetchPerplexityMock(unittest.TestCase):
    @patch("sync_catalyst_news.chat_completion")
    def test_fetch_parses_response(self, mock_chat: MagicMock) -> None:
        today = date.today().isoformat()
        payload = {
            "events": [
                {
                    "stock_id": "2330",
                    "event_date": today,
                    "catalyst_type": "SUPPLY_CHAIN",
                    "headline": "CoWoS 擴產",
                    "polarity": "BULL",
                    "explains_etf_add": "MED",
                    "confidence": 65,
                    "sources": [],
                }
            ]
        }
        mock_chat.return_value = json.dumps(payload)

        class E:
            stock_id = "2330"
            stock_name = "台積電"
            pool_reason = "money"
            headline = None

        events = fetch_perplexity_events(
            [E()],
            lookback_days=7,
            api_key="test-key",
            model="sonar",
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].catalyst_type, "SUPPLY_CHAIN")


class TestSyncNewsDryRun(unittest.TestCase):
    def test_no_api_key_returns_zero(self) -> None:
        import os
        import sqlite3
        import tempfile
        from pathlib import Path

        from stock_db import connect

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            old = os.environ.pop("PERPLEXITY_API_KEY", None)
            try:
                n, evs = sync_news_for_universe(conn, ("00981A",), dry_run=True)
                self.assertEqual(n, 0)
                self.assertEqual(evs, [])
            finally:
                if old:
                    os.environ["PERPLEXITY_API_KEY"] = old
            conn.close()


if __name__ == "__main__":
    unittest.main()
