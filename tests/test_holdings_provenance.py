"""holdings_provenance：原始檔、hash、同日版本比對。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from holdings_provenance import record_holdings_fetch, save_raw_snapshot
from stock_db import connect, list_etf_holdings_fetch_log


class TestHoldingsProvenance(unittest.TestCase):
    def test_save_and_log_fetch(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        conn = connect(Path(tmp.name) / "t.db")
        holdings = [
            {
                "stock_id": "2330",
                "stock_name": "台積電",
                "shares": 1000.0,
                "weight_pct": 5.0,
                "amount": None,
            }
        ]
        try:
            fetch_id = record_holdings_fetch(
                conn,
                etf_code="00981A",
                snapshot_date="2026-06-09",
                source="ezmoney",
                source_edit_at="2026-06-09",
                nav=100.0,
                holdings=holdings,
                sync_status="synced",
            )
            self.assertGreater(fetch_id, 0)
            logs = list_etf_holdings_fetch_log(conn, "00981A", snapshot_date="2026-06-09")
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0]["sync_status"], "synced")
            self.assertEqual(logs[0]["diff_summary"], "first_fetch")

            holdings2 = [
                *holdings,
                {
                    "stock_id": "2317",
                    "stock_name": "鴻海",
                    "shares": 500.0,
                    "weight_pct": 2.0,
                    "amount": None,
                },
            ]
            fetch_id2 = record_holdings_fetch(
                conn,
                etf_code="00981A",
                snapshot_date="2026-06-09",
                source="ezmoney",
                source_edit_at="2026-06-09",
                nav=100.0,
                holdings=holdings2,
                sync_status="synced",
            )
            self.assertGreater(fetch_id2, fetch_id)
            logs = list_etf_holdings_fetch_log(conn, "00981A", snapshot_date="2026-06-09")
            self.assertEqual(len(logs), 2)
            self.assertEqual(logs[0]["sync_status"], "version_diff")
            self.assertEqual(logs[0]["rows_added"], 1)
        finally:
            conn.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
