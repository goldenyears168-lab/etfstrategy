"""etf_snapshot_json · etf-daily-v1 contract."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from etf_snapshot_json import CONTRACT, build_etf_snapshot_json
from project_config import ETF_CODES_HOLDINGS
from stock_db import connect


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


if __name__ == "__main__":
    unittest.main()
