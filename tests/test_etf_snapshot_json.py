"""etf_snapshot_json · etf-daily-v1 contract."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from etf_snapshot_json import CONTRACT, build_etf_snapshot_json
from project_config import ETF_CODES_HOLDINGS
from stock_db import connect, upsert_etf_holdings, upsert_etf_holdings_meta

SYNCED = "2026-06-01T00:00:00+00:00"


def _seed_two_day(conn, etf_code: str, prev: str, curr: str) -> None:
    for snap, rows in (
        (
            prev,
            [
                {
                    "etf_code": etf_code,
                    "snapshot_date": prev,
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "shares": 1000.0,
                    "weight_pct": 5.0,
                    "amount": 500_000.0,
                    "source": "t",
                    "source_edit_at": None,
                    "synced_at": SYNCED,
                }
            ],
        ),
        (
            curr,
            [
                {
                    "etf_code": etf_code,
                    "snapshot_date": curr,
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "shares": 1100.0,
                    "weight_pct": 5.5,
                    "amount": 605_000.0,
                    "source": "t",
                    "source_edit_at": None,
                    "synced_at": SYNCED,
                }
            ],
        ),
    ):
        upsert_etf_holdings_meta(
            conn,
            {
                "etf_code": etf_code,
                "snapshot_date": snap,
                "nav": 100.0,
                "holding_count": len(rows),
                "source": "t",
                "source_edit_at": None,
            },
        )
        upsert_etf_holdings(conn, rows)


class TestEtfSnapshotJson(unittest.TestCase):
    def test_empty_db_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            payload = build_etf_snapshot_json(conn, "2026-06-20", ETF_CODES_HOLDINGS)
            conn.close()

        self.assertEqual(payload["contract"], CONTRACT)
        self.assertEqual(payload["layer"], "facts")
        self.assertEqual(payload["as_of"], "2026-06-20")
        self.assertIn("sync", payload)
        self.assertEqual(payload["sync"]["sync_count"], f"0/{payload['sync']['listed_total']}")
        self.assertIn("summary", payload)
        self.assertIn("consensus_adds", payload)
        self.assertIn("sections", payload)
        self.assertIsInstance(payload["sections"], list)
        self.assertIn("meta", payload)
        self.assertIn("generated_at", payload["meta"])

    def test_pit_sections_anchor_on_as_of(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            _seed_two_day(conn, "00981A", "2026-06-17", "2026-06-18")
            _seed_two_day(conn, "00403A", "2026-06-18", "2026-06-22")
            payload = build_etf_snapshot_json(conn, "2026-06-17", ("00981A", "00403A"))
            conn.close()

        sections = {s["etf_code"]: s for s in payload["sections"]}
        self.assertEqual(sections["00981A"]["prev_date"], "2026-06-17")
        self.assertEqual(sections["00981A"]["curr_date"], "2026-06-18")
        self.assertEqual(sections["00403A"]["changes"], [])
        self.assertIn("note", sections["00403A"])
        self.assertIn("00403A", payload["summary"]["skipped_etfs"])


if __name__ == "__main__":
    unittest.main()
