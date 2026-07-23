"""Unit tests for Songshan research_5d evaluate helpers."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "run_songshan_follow_watch",
    ROOT / "scripts/research/run_songshan_follow_watch.py",
)
assert _SPEC and _SPEC.loader
m = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = m
_SPEC.loader.exec_module(m)


class TestEvaluateResearch5d(unittest.TestCase):
    def test_fires_on_candidate(self) -> None:
        cands = [
            {
                "stock_id": "2492",
                "buy_5d": 0.6e8,
                "sell_5d": 0.0,
                "net_ratio": 1.0,
                "buy_amt": 0.6e8,
                "buy_day": 0.6e8,
                "day_net": 1.0,
                "close": 100.0,
                "window_start": "2026-07-16",
                "window_end": "2026-07-22",
                "day_whale": True,
                "strength_1p5": False,
            }
        ]
        r = m.evaluate_research_5d(cands, top1=None)
        self.assertTrue(r["fired"])
        self.assertEqual(r["top1"]["stock_id"], "2492")
        self.assertTrue(r["day_whale"])
        self.assertFalse(r["strength_1p5"])
        body = m.format_body(
            asof="2026-07-22",
            result=r,
            tape_note="test",
            branch_key="songshan",
        )
        self.assertIn("五日累積淨比95", body)
        self.assertIn("entry_25m_nonfail", body)
        self.assertIn("0.60 億", body)

    def test_quiet_when_empty(self) -> None:
        r = m.evaluate_research_5d([], top1={"stock_id": "2330", "buy_amt": 0.3e8})
        self.assertFalse(r["fired"])
        self.assertIn("未達五日累積", r["reason"])


if __name__ == "__main__":
    unittest.main()
