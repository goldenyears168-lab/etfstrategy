"""vcp_screen_scores_v2 round-trip."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from stock_db import connect, load_vcp_screen_v2_for_date, upsert_vcp_screen_scores_v2


class VcpScreenScoresV2Tests(unittest.TestCase):
    def test_upsert_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            try:
                upsert_vcp_screen_scores_v2(
                    conn,
                    [
                        {
                            "stock_id": "2330",
                            "as_of_date": "2026-06-16",
                            "model_id": "vcp-tm",
                            "stock_name": "台積電",
                            "composite_score": 82.5,
                            "rating": "Strong VCP",
                            "execution_state": "Pre-breakout",
                            "entry_ready": 1,
                            "pattern_type": "VCP-adjacent",
                            "pivot_price": 1000.0,
                            "distance_from_pivot_pct": -2.0,
                            "stop_loss": 950.0,
                            "risk_pct": 5.0,
                            "valid_vcp": 1,
                            "metadata_json": json.dumps({"test": True}),
                        }
                    ],
                )
                rows = load_vcp_screen_v2_for_date(
                    conn,
                    "2026-06-16",
                    model_id="vcp-tm",
                    execution_states=("Pre-breakout", "Breakout"),
                )
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["stock_id"], "2330")
                self.assertEqual(rows[0]["execution_state"], "Pre-breakout")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
