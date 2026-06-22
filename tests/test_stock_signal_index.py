"""stock_signal_index · hit extraction."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from stock_signal_index import hits_from_brief
from supabase_research_sync import BriefRecord


class TestStockSignalIndex(unittest.TestCase):
    def test_etf_snapshot_hits(self) -> None:
        record = BriefRecord(
            trade_date=date(2026, 6, 20),
            schedule_slot="1630",
            brief_type="etf_daily",
            title="t",
            content_md="",
            source_path="x.md",
            snapshot_json={
                "contract": "etf-daily-v1",
                "sections": [
                    {
                        "etf_code": "00981A",
                        "changes": [
                            {
                                "stock_id": "2330",
                                "stock_name": "台積電",
                                "action": "加码",
                                "share_delta": 1000,
                            }
                        ],
                    }
                ],
            },
        )
        hits = hits_from_brief(None, record)  # type: ignore[arg-type]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["stock_id"], "2330")
        self.assertEqual(hits[0]["brief_type"], "etf_daily")
        self.assertEqual(hits[0]["tab"], "etf")

    def test_vcp_snapshot_hits(self) -> None:
        record = BriefRecord(
            trade_date=date(2026, 6, 20),
            schedule_slot="1630",
            brief_type="vcp_pivot_gate",
            title="t",
            content_md="",
            source_path="x.md",
            snapshot_json={
                "contract": "vcp-daily-v1",
                "variants": [
                    {
                        "spec_key": "pivot_gate",
                        "candidates": [
                            {
                                "stock_id": "2449",
                                "stock_name": "京元電",
                                "composite_score": 55.0,
                                "execution_state": "Pre-breakout",
                            }
                        ],
                    }
                ],
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            from stock_db import connect

            conn = connect(Path(tmp) / "t.db")
            hits = hits_from_brief(conn, record)
            conn.close()
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["stock_id"], "2449")


if __name__ == "__main__":
    unittest.main()
