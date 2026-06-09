"""portfolio_engine：權重分配規則。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from entry_signal import EntryContext
from market_labels import (
    CHIP_FOREIGN_BUY,
    ENTRY_OVEREXTENDED,
    ENTRY_PULLBACK,
    PM_OBSERVE,
    WL_EXCLUDED,
    WL_GENERAL,
    WL_PRIMARY,
)
from investment_policy import DEFAULTS, InvestmentPolicy
from portfolio_engine import build_portfolio_rows, raw_weight_pct
from research_universe import UniverseEntry
from score_engine import DimensionScores, ScoredEntry
from stock_db import connect


def _ips_equal(**overrides) -> InvestmentPolicy:
    return InvestmentPolicy.from_dict({**DEFAULTS, "daily_weight_mode": "equal", **overrides})


def _scored(
    watchlist: str,
    *,
    stock_id: str = "2330",
    stock_name: str = "台積電",
    catalyst: float = 80.0,
) -> ScoredEntry:
    entry = UniverseEntry(stock_id, stock_name, "both", 1, 1, 1.0, 0.8, None)
    return ScoredEntry(
        entry=entry,
        dimensions=_dims(catalyst=catalyst),
        watchlist=watchlist,
        position_intent=None,
        tech_risk_flag=None,
        entry_signal=ENTRY_PULLBACK,
        entry_tags=(),
        chip_tag=CHIP_FOREIGN_BUY,
        metadata={},
    )


def _dims(**kwargs) -> DimensionScores:
    defaults = dict(
        flow=80,
        chip=75,
        catalyst=50,
        expectation=60,
        fundamental=55,
        risk=70,
        timing=78,
    )
    defaults.update(kwargs)
    return DimensionScores(**defaults)


class TestPortfolioWeight(unittest.TestCase):
    def test_a_has_positive_raw(self) -> None:
        s = _scored(WL_PRIMARY)
        w = raw_weight_pct(
            s,
            position_score=75,
            risk_score=30,
            pm_bucket=PM_OBSERVE,
            entry_ctx=EntryContext(ENTRY_PULLBACK, ()),
        )
        self.assertGreater(w, 0)

    def test_overextended_no_strong_zero(self) -> None:
        s = _scored(WL_GENERAL)
        w = raw_weight_pct(
            s,
            position_score=60,
            risk_score=50,
            pm_bucket=PM_OBSERVE,
            entry_ctx=EntryContext(ENTRY_OVEREXTENDED, ()),
        )
        self.assertEqual(w, 0.0)

    def test_build_equal_single_gets_20pct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            rows = build_portfolio_rows(
                [
                    _scored(WL_PRIMARY),
                    _scored(WL_EXCLUDED, stock_id="2454", stock_name="聯發科"),
                ],
                as_of_date="2026-06-04",
                conn=conn,
                pm_bucket_by_id={"2330": PM_OBSERVE, "2454": PM_OBSERVE},
                capital_ntd=100_000,
                ips=_ips_equal(),
            )
            alloc = [r for r in rows if r.portfolio_weight_pct > 0]
            self.assertEqual(len(alloc), 1)
            self.assertAlmostEqual(alloc[0].portfolio_weight_pct, 20.0, delta=0.1)
            self.assertAlmostEqual(alloc[0].suggested_ntd, 20_000, delta=1)
            conn.close()

    def test_build_equal_top5_caps_at_five(self) -> None:
        ids = [f"10{i:02d}" for i in range(6)]
        scored = [
            _scored(WL_PRIMARY, stock_id=sid, stock_name=f"股{sid}", catalyst=90 - i * 5)
            for i, sid in enumerate(ids)
        ]
        pm_map = {sid: PM_OBSERVE for sid in ids}
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            rows = build_portfolio_rows(
                scored,
                as_of_date="2026-06-04",
                conn=conn,
                pm_bucket_by_id=pm_map,
                capital_ntd=100_000,
                ips=_ips_equal(),
            )
            alloc = [r for r in rows if r.portfolio_weight_pct > 0]
            self.assertEqual(len(alloc), 5)
            self.assertTrue(all(r.portfolio_weight_pct == 20.0 for r in alloc))
            top_ids = {r.stock_id for r in alloc}
            self.assertEqual(top_ids, set(ids[:5]))
            conn.close()


if __name__ == "__main__":
    unittest.main()
