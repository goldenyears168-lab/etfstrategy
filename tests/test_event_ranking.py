"""event_ranking：MSCI/指數調整排除。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from event_ranking import (
    CatalystEvent,
    is_index_rebalance_event,
    is_index_rebalance_headline,
    load_all_catalyst_events,
    purge_index_rebalance_from_db,
    rank_events,
    score_event,
)
from stock_db import connect, upsert_catalyst_events
from catalyst_engine import event_to_row


class TestIndexRebalance(unittest.TestCase):
    def test_headline_detect(self) -> None:
        self.assertTrue(is_index_rebalance_headline("MSCI 納入旺矽"))
        self.assertFalse(is_index_rebalance_headline("CoWoS 擴產"))

    def test_score_zero(self) -> None:
        ev = CatalystEvent(
            "6223",
            date.today(),
            "INDEX_REBALANCE",
            "MSCI納入",
            confidence=90,
            explains_etf_add="HIGH",
        )
        self.assertEqual(score_event(ev), 0.0)

    def test_rank_excludes_msci(self) -> None:
        today = date.today()
        events = [
            CatalystEvent(
                "6223",
                today,
                "CAPX",
                "先進封裝擴產",
                confidence=80,
                explains_etf_add="HIGH",
            ),
            CatalystEvent(
                "1303",
                today,
                "INDEX_REBALANCE",
                "MSCI納入南亞",
                confidence=95,
                explains_etf_add="HIGH",
            ),
        ]
        ranked = rank_events(events, top_n=5, as_of=today)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].stock_id, "6223")
        self.assertFalse(is_index_rebalance_event(ranked[0].event))

    def test_purge_index_rebalance_from_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            try:
                capx = CatalystEvent(
                    "6223",
                    date.today(),
                    "CAPX",
                    "擴產計畫",
                    confidence=70,
                )
                msci = CatalystEvent(
                    "2330",
                    date.today(),
                    "POLICY",
                    "MSCI半年度調整生效",
                    confidence=90,
                )
                upsert_catalyst_events(
                    conn,
                    [event_to_row(capx), event_to_row(msci)],
                )
                n = purge_index_rebalance_from_db(conn)
                self.assertEqual(n, 1)
                left = conn.execute(
                    "SELECT stock_id FROM catalyst_events"
                ).fetchall()
                self.assertEqual(len(left), 1)
                self.assertEqual(left[0][0], "6223")
            finally:
                conn.close()


class TestLoadAllCatalystEvents(unittest.TestCase):
    def test_explicit_path_without_env_flag(self) -> None:
        """測試或自訂 events 檔：不需 USE_MANUAL_EVENTS=1。"""
        tmp = tempfile.TemporaryDirectory()
        events_file = Path(tmp.name) / "events.json"
        events_file.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "stock_id": "2330",
                            "event_date": date.today().isoformat(),
                            "catalyst_type": "EARNINGS",
                            "headline": "test",
                            "confidence": 80,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"USE_MANUAL_EVENTS": "0"}, clear=False):
            loaded = load_all_catalyst_events(None, events_file)
        tmp.cleanup()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].stock_id, "2330")


if __name__ == "__main__":
    unittest.main()
