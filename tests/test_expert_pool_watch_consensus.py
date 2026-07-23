"""Unit tests for expert-pool email consensus density helpers."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "run_expert_pool_watch",
    ROOT / "scripts/research/run_expert_pool_watch.py",
)
assert _SPEC and _SPEC.loader
m = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = m
_SPEC.loader.exec_module(m)


class TestPerCoreL1h7Stats(unittest.TestCase):
    def test_counts_trades_per_core_expert(self) -> None:
        core = {"A1": "甲券商", "B2": "乙券商", "C3": "丙券商"}
        trades = [
            {
                "r_pct": 10.0,
                "branches": [{"tid": "A1", "name": "甲券商"}, {"tid": "B2", "name": "乙券商"}],
            },
            {
                "r_pct": -5.0,
                "branches": [{"tid": "A1", "name": "甲券商"}],
            },
            {
                "r_pct": 2.0,
                "branches": [{"tid": "B2", "name": "乙券商"}],
            },
        ]
        rows = {r["tid"]: r for r in m.per_core_l1h7_stats(core, trades)}
        self.assertEqual(rows["A1"]["n"], 2)
        self.assertEqual(rows["A1"]["win_pct"], 50.0)
        self.assertEqual(rows["A1"]["med_r_pct"], 2.5)
        self.assertEqual(rows["B2"]["n"], 2)
        self.assertEqual(rows["B2"]["win_pct"], 100.0)
        self.assertEqual(rows["C3"]["n"], 0)
        self.assertIsNone(rows["C3"]["win_pct"])


class TestConsensusDensityLines(unittest.TestCase):
    def test_ratio_and_markers(self) -> None:
        spec = m.PoolSpec(
            stock_id="9999",
            stock_name="測試",
            core={"A1": "甲券商", "B2": "乙券商", "C3": "丙券商"},
            net_floor=1e8,
            floor_label="≥1億",
            playbook=("x",),
            state_name="x.json",
            origin_note="test",
            report_dir=ROOT / "tmp",
        )
        ledger = {
            "trades": [
                {
                    "r_pct": 8.0,
                    "branches": [{"tid": "A1", "name": "甲券商"}],
                },
                {
                    "r_pct": -1.0,
                    "branches": [{"tid": "A1", "name": "甲券商"}],
                },
            ]
        }
        hit = [
            {"tid": "A1", "name": "甲券商", "role": "今≥1億"},
            {"tid": "B2", "name": "乙券商", "role": "昨≥1億"},
        ]
        text = "\n".join(
            m.format_consensus_density_lines(
                spec=spec, hit_branches=hit, ledger=ledger
            )
        )
        self.assertIn("今日觸發：2/3 專家（67%）", text)
        self.assertIn("★ 甲券商 (A1) [今≥1億]", text)
        self.assertIn("L1H7 n=2 勝率50%", text)
        self.assertIn("· 丙券商 (C3) [未]", text)
        self.assertIn("尚無樣本", text)


if __name__ == "__main__":
    unittest.main()
