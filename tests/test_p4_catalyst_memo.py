"""P4：catalyst_events 入庫與 memo 審計。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from catalyst_engine import event_to_row
from market_labels import WL_GENERAL, WL_PRIMARY
from event_ranking import CatalystEvent, catalyst_event_id, load_all_catalyst_events
from investment_memo import audit_memo_text, render_template_section
from stock_db import connect, upsert_catalyst_events, upsert_investment_scores


class TestCatalystEngine(unittest.TestCase):
    def test_event_id_stable(self) -> None:
        ev = CatalystEvent("2330", date.today(), "EARNINGS", "test headline")
        self.assertEqual(catalyst_event_id(ev), catalyst_event_id(ev))

    def test_sync_manual_to_db(self) -> None:
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
                            "headline": "法說摘要",
                            "confidence": 80,
                            "explains_etf_add": "HIGH",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        conn = connect(Path(tmp.name) / "t.db")
        try:
            ev = CatalystEvent(
                "2330",
                date.today(),
                "EARNINGS",
                "法說摘要",
                explains_etf_add="HIGH",
                confidence=80,
            )
            upsert_catalyst_events(conn, [event_to_row(ev)])
            loaded = load_all_catalyst_events(conn, events_file)
            self.assertGreaterEqual(len(loaded), 1)
        finally:
            conn.close()
            tmp.cleanup()


class TestMemoAudit(unittest.TestCase):
    def test_forbidden_buy_hold_trim(self) -> None:
        ok, notes = audit_memo_text("建議 BUY 此股，目標價 1000")
        self.assertFalse(ok)
        self.assertIn("BUY", notes)
        self.assertIn("目標價", notes)

    def test_template_passes_audit(self) -> None:
        ctx = {
            "stock_id": "2330",
            "stock_name": "台積電",
            "watchlist": WL_GENERAL,
            "investment_score": 80.0,
            "dimensions": {
                "smart_money": 90,
                "catalyst": 70,
                "expectation": 55,
                "fundamental": 74,
                "risk": 50,
            },
            "score_metadata": {},
            "catalyst_events": [],
            "pool_reason": "money",
            "position_intent": "ACCUMULATE",
        }
        text = render_template_section(ctx)
        ok, notes = audit_memo_text(text)
        self.assertTrue(ok, notes)


class TestMemoDb(unittest.TestCase):
    def test_generate_with_scores(self) -> None:
        from investment_memo import generate_memo_document

        tmp = tempfile.TemporaryDirectory()
        conn = connect(Path(tmp.name) / "t.db")
        try:
            upsert_investment_scores(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "as_of_date": "2026-06-04",
                        "score_version": "p4-v2",
                        "stock_name": "台積電",
                        "smart_money": 90,
                        "catalyst": 85,
                        "expectation": 55,
                        "fundamental": 74,
                        "risk": 50,
                        "investment_score": 86.0,
                        "watchlist": WL_PRIMARY,
                        "pool_reason": "both",
                        "money_rank": 1,
                        "event_rank": 1,
                        "position_intent": "ACCUMULATE",
                        "tech_risk_flag": None,
                        "metadata_json": "{}",
                    }
                ],
            )
            doc, rows, memo_date = generate_memo_document(
                conn, as_of_date="2026-06-04", use_llm=False
            )
            self.assertIn("2330", doc)
            self.assertEqual(len(rows), 1)
            self.assertEqual(memo_date, "2026-06-04")
            ok, _ = audit_memo_text(doc)
            self.assertTrue(ok)
        finally:
            conn.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
