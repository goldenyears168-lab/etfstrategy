"""Unit tests · expert-pool W0_hard sell observation helpers + holdings badge priority."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_WATCH = importlib.util.spec_from_file_location(
    "run_expert_pool_watch",
    ROOT / "scripts/research/run_expert_pool_watch.py",
)
assert _WATCH and _WATCH.loader
watch = importlib.util.module_from_spec(_WATCH)
sys.modules[_WATCH.name] = watch
_WATCH.loader.exec_module(watch)

_HOLD = importlib.util.spec_from_file_location(
    "run_holdings_branch_sell_monitor",
    ROOT / "scripts/order/run_holdings_branch_sell_monitor.py",
)
assert _HOLD and _HOLD.loader
hold = importlib.util.module_from_spec(_HOLD)
sys.modules[_HOLD.name] = hold
_HOLD.loader.exec_module(hold)


class TestFormatW0HardLines(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(watch.format_w0_hard_lines(None), [])

    def test_copy_is_observation_not_exit(self) -> None:
        w0 = {
            "tag": "W0_hard",
            "n_hard": 2,
            "floor_label": "≥0.5億",
            "branches": [
                {
                    "tid": "A",
                    "name": "甲",
                    "sell_amt": 6e7,
                    "net": -1000,
                    "close": 100.0,
                },
                {
                    "tid": "B",
                    "name": "乙",
                    "sell_amt": 5e7,
                    "net": -800,
                    "close": 100.0,
                },
            ],
        }
        text = "\n".join(watch.format_w0_hard_lines(w0))
        self.assertIn("非出場", text)
        self.assertIn("最不糟", text)
        self.assertNotIn("SELL NOW", text)
        self.assertNotIn("建議賣", text)


class TestHoldingsSubjectPriority(unittest.TestCase):
    def _row(
        self,
        *,
        sid: str,
        w0: bool = False,
        soft2: bool = False,
        hard_n: int = 0,
        soft_n: int = 0,
        panel_k: int = 0,
    ) -> hold.StockMonitor:
        hits: list[hold.SellHit] = []
        for i in range(hard_n):
            hits.append(
                hold.SellHit(
                    stock_id=sid,
                    stock_name=sid,
                    shares=100,
                    tid=f"H{i}",
                    branch_name=f"H{i}",
                    trade_date="2026-07-01",
                    net_shares=-1,
                    net_amt=-1e8,
                    sell_amt=1e8,
                    soft_floor=2.5e7,
                    hard_floor=5e7,
                    level="HARD",
                    floor_label="≥0.5億",
                )
            )
        for i in range(soft_n):
            hits.append(
                hold.SellHit(
                    stock_id=sid,
                    stock_name=sid,
                    shares=100,
                    tid=f"S{i}",
                    branch_name=f"S{i}",
                    trade_date="2026-07-01",
                    net_shares=-1,
                    net_amt=-3e7,
                    sell_amt=3e7,
                    soft_floor=2.5e7,
                    hard_floor=5e7,
                    level="SOFT",
                    floor_label="≥0.5億",
                )
            )
        return hold.StockMonitor(
            holding=hold.HoldingRow(stock_id=sid, shares=100, stock_name=sid),
            stock_name=sid,
            asof="2026-07-01",
            has_pool=True,
            expert_hits=hits,
            expert_w0_hard=w0,
            expert_soft2=soft2,
            panel_k=panel_k,
            panel_n=37,
        )

    def test_w0_beats_soft2_in_subject(self) -> None:
        rows = [self._row(sid="2327", w0=True, soft2=True, hard_n=2)]
        subj = hold.subject_line(asof="2026-07-01", rows=rows)
        self.assertIn("[W0_HARD]", subj)
        self.assertNotIn("[CONSENSUS]", subj)
        self.assertNotIn("[SOFT2]", subj)

    def test_soft2_when_no_w0(self) -> None:
        rows = [self._row(sid="2327", soft2=True, soft_n=2)]
        subj = hold.subject_line(asof="2026-07-01", rows=rows)
        self.assertIn("[SOFT2]", subj)

    def test_consensus_alias_is_w0(self) -> None:
        r = self._row(sid="2327", w0=True, hard_n=2)
        self.assertTrue(r.expert_consensus)


if __name__ == "__main__":
    unittest.main()
