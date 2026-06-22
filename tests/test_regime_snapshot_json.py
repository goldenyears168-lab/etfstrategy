"""Tests for regime_snapshot_json (Supabase / React payload)."""

from __future__ import annotations

import json
import unittest
from datetime import date

from regime_charts import RRG_SCATTER_SNAPSHOT_MAX
from regime_snapshot_json import SCHEMA_VERSION, build_regime_snapshot_json
from stock_db import DEFAULT_DB_PATH, connect


class RegimeSnapshotJsonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not DEFAULT_DB_PATH.is_file():
            raise unittest.SkipTest("stocks.db missing")
        cls.conn = connect(DEFAULT_DB_PATH)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def test_build_20260617(self) -> None:
        payload = build_regime_snapshot_json(self.conn, "2026-06-17")
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["as_of"], "2026-06-17")
        self.assertIn("breadth_zone_200", payload["axes"])
        self.assertTrue(payload["axes"]["breadth_zone_200"].get("available"))
        self.assertIn("synopsis", payload["interpretations"])
        self.assertIn("breadth", payload["chart_series"])
        self.assertGreater(len(payload["chart_series"]["breadth"]), 10)
        rrg = payload["chart_series"].get("rrg_scatter")
        if rrg:
            self.assertIn("points", rrg)
            n_pts = len(rrg["points"])
            self.assertGreater(n_pts, 100)
            self.assertLessEqual(n_pts, RRG_SCATTER_SNAPSHOT_MAX)
            ranked = payload["axes"]["rrg_rotation"].get("ranked_symbols") or []
            self.assertGreater(len(ranked), 0)
        json.dumps(payload, ensure_ascii=False)

    def test_json_roundtrip(self) -> None:
        payload = build_regime_snapshot_json(self.conn, "2026-06-17")
        raw = json.dumps(payload, ensure_ascii=False)
        parsed = json.loads(raw)
        self.assertEqual(parsed["schema_version"], SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
