"""etf_flow_factor_screen：持股變動事前因子檢定。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from research.archive.etf_flow_factor_screen import (
    FACTOR_SPECS,
    FeatureRow,
    FlowLeg,
    collect_flow_legs,
    screen_factors,
    unique_stock_days,
)


class EtfFlowFactorScreenTests(unittest.TestCase):
    def test_screen_factors_numeric_and_bool(self) -> None:
        events = [
            FeatureRow(
                event_date="2026-06-10",
                stock_id="2330",
                side="add",
                values={"ret14": 10.0, "ma20_rising": 1},
            ),
            FeatureRow(
                event_date="2026-06-10",
                stock_id="2454",
                side="reduce",
                values={"ret14": -2.0, "ma20_rising": 0},
            ),
        ]
        ctrl = [
            FeatureRow(
                event_date="2026-06-10",
                stock_id="2317",
                side="control",
                values={"ret14": 2.0, "ma20_rising": 0},
            ),
            FeatureRow(
                event_date="2026-06-10",
                stock_id="2303",
                side="control",
                values={"ret14": 4.0, "ma20_rising": 1},
            ),
        ]
        effects = screen_factors(events, ctrl)
        ret = next(e for e in effects if e.key == "ret14")
        self.assertEqual(ret.mean_add, 10.0)
        self.assertEqual(ret.mean_reduce, -2.0)
        self.assertEqual(ret.mean_ctrl, 3.0)
        self.assertEqual(ret.delta_add_ctrl, 7.0)

        ma = next(e for e in effects if e.key == "ma20_rising")
        self.assertEqual(ma.pct_add, 100.0)
        self.assertEqual(ma.pct_reduce, 0.0)
        self.assertEqual(ma.pct_ctrl, 50.0)

    def test_unique_stock_days_keeps_higher_etf_count(self) -> None:
        legs = [
            FlowLeg("2026-06-10", "2330", "台積電", "00981A", "add", "加码", 1),
            FlowLeg("2026-06-10", "2330", "台積電", "00980A", "add", "加码", 2, frozenset({"00981A", "00980A"})),
        ]
        out = unique_stock_days(legs)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].n_etf_same_day, 2)

    def test_collect_flow_legs_empty_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "empty.db"
            from stock_db import connect

            with connect(db) as conn:
                self.assertEqual(collect_flow_legs(conn), [])


if __name__ == "__main__":
    unittest.main()
