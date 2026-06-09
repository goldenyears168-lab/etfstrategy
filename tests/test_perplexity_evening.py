"""perplexity_evening：上下文組裝與審計（無 API）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from perplexity_client import audit_narrative, extract_json_payload
from catalyst_engine import event_to_row
from event_ranking import CatalystEvent
from perplexity_evening import VERIFY_STATUSES, audit_evening_no_index_mainline
from research_context import build_research_context
from stock_db import upsert_catalyst_events
from research_universe import DEFAULT_ETF_CODES
from stock_db import connect


class TestPerplexityClient(unittest.TestCase):
    def test_extract_checks_json(self) -> None:
        raw = '{"checks":[{"event_id":"abc","status":"CONFIRMED","note":"ok","confidence_delta":10}]}'
        data = extract_json_payload(raw)
        self.assertIsInstance(data, dict)
        assert isinstance(data, dict)
        self.assertEqual(len(data["checks"]), 1)

    def test_audit_blocks_rating(self) -> None:
        ok, notes = audit_narrative("建議 BUY 此股")
        self.assertFalse(ok)
        self.assertTrue(notes)


class TestEveningContext(unittest.TestCase):
    def test_build_context_empty_scores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            ctx = build_research_context(conn, DEFAULT_ETF_CODES)
            self.assertIn("as_of_date", ctx)
            self.assertIn("tech_risk", ctx)
            self.assertIn("appendix", ctx)
            self.assertIn("catalyst_events_note", ctx["appendix"])
            conn.close()

    def test_context_excludes_index_rebalance_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            ev = CatalystEvent(
                "2330",
                __import__("datetime").date(2026, 6, 4),
                "INDEX_REBALANCE",
                "MSCI半年度調整台積電權重下調",
                confidence=100,
            )
            upsert_catalyst_events(conn, [event_to_row(ev)])
            ctx = build_research_context(conn, DEFAULT_ETF_CODES)
            for item in ctx["catalyst_events"]:
                self.assertNotEqual(item["type"], "INDEX_REBALANCE")
                self.assertNotIn("MSCI", item["headline"])
            conn.close()


class TestEveningAudit(unittest.TestCase):
    def test_audit_flags_msci_catalyst_bullet(self) -> None:
        text = "- **催化/新聞要點**：MSCI 半年度調整為今日主線"
        ok, notes = audit_evening_no_index_mainline(text)
        self.assertFalse(ok)
        self.assertTrue(notes)


class TestVerifyStatuses(unittest.TestCase):
    def test_status_enum(self) -> None:
        self.assertIn("CONFIRMED", VERIFY_STATUSES)
        self.assertIn("RUMOR", VERIFY_STATUSES)


if __name__ == "__main__":
    unittest.main()
