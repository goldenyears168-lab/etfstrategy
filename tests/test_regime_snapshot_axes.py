"""regime_snapshot · config-driven axes."""

from __future__ import annotations

import unittest

from regime_snapshot import build_regime_snapshot, configured_axis_ids
from stock_db import connect


class TestRegimeSnapshotAxes(unittest.TestCase):
    def test_configured_axis_ids_from_yaml(self) -> None:
        cfg = {
            "axes": {
                "breadth_zone_200": {"module": "market_breadth_ma"},
                "trend_posture": {"module": "stage_analysis"},
            }
        }
        self.assertEqual(configured_axis_ids(cfg), ["breadth_zone_200", "trend_posture"])

    def test_snapshot_includes_impulse_sub_block(self) -> None:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT MAX(date) AS d FROM daily_bars WHERE code='IX0001' AND source='tej'"
            ).fetchone()
            if not row or not row["d"]:
                self.skipTest("no IX0001 data")
            snap = build_regime_snapshot(conn, str(row["d"]))
            self.assertIn("axis_order", snap)
            self.assertIn("breadth_zone_200", snap)
            b = snap["breadth_zone_200"]
            if b.get("available"):
                self.assertIn("rhythm", b)
                self.assertIn("impulse", b)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
