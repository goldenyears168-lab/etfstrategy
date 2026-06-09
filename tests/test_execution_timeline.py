"""execution_timeline：時間軸分層與下一階段 CLI。"""

from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from execution_timeline import (
    EXECUTION_LAYERS,
    layer_heading,
    next_step_lines,
    suggest_mode_by_clock,
)


class TestExecutionTimeline(unittest.TestCase):
    def test_layer_heading_pre_open(self) -> None:
        self.assertIn("08:25–08:40", layer_heading("pre_open"))
        self.assertIn("pre_open", layer_heading("pre_open"))

    def test_suggest_mode_by_clock(self) -> None:
        tz = ZoneInfo("Asia/Taipei")
        self.assertEqual(
            suggest_mode_by_clock(datetime(2026, 6, 5, 8, 30, tzinfo=tz)),
            "pre_open",
        )
        self.assertEqual(
            suggest_mode_by_clock(datetime(2026, 6, 5, 8, 50, tzinfo=tz)),
            "auction",
        )
        self.assertEqual(
            suggest_mode_by_clock(datetime(2026, 6, 5, 9, 2, tzinfo=tz)),
            "open",
        )
        self.assertEqual(
            suggest_mode_by_clock(datetime(2026, 6, 5, 10, 0, tzinfo=tz)),
            "intraday",
        )

    def test_next_step_from_pre_open(self) -> None:
        lines = next_step_lines("pre_open")
        self.assertTrue(any("--mode auction" in ln for ln in lines))

    def test_four_layers(self) -> None:
        self.assertEqual(len(EXECUTION_LAYERS), 4)


if __name__ == "__main__":
    unittest.main()
