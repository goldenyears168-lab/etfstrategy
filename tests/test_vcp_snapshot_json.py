"""vcp_snapshot_json · vcp-daily-v1 contract."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from stock_db import connect
from vcp_snapshot_json import CONTRACT, build_vcp_snapshot_json


class TestVcpSnapshotJson(unittest.TestCase):
    def test_funnel_specs_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            payload = build_vcp_snapshot_json(
                conn, "2026-06-20", "vcp_funnel_specs", schedule_slot="1630"
            )
            conn.close()

        self.assertEqual(payload["contract"], CONTRACT)
        self.assertEqual(payload["layer"], "research")
        self.assertEqual(payload["brief_type"], "vcp_funnel_specs")
        self.assertFalse(payload["intraday"])
        self.assertEqual(len(payload["variants"]), 2)
        self.assertIn("candidate_count", payload)

    def test_pivot_gate_strategy_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            payload = build_vcp_snapshot_json(
                conn, "2026-06-20", "vcp_pivot_gate", schedule_slot="1300"
            )
            conn.close()

        self.assertEqual(payload["layer"], "strategy")
        self.assertTrue(payload["intraday"])
        self.assertEqual(len(payload["variants"]), 1)
        self.assertEqual(payload["strategy_id"], "vcp-pivot-gate")


if __name__ == "__main__":
    unittest.main()
