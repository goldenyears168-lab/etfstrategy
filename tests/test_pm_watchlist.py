"""pm_watchlist 分桶與資格規則。"""

from __future__ import annotations

import unittest

from entry_signal import EntryContext
from market_labels import (
    CHIP_FOREIGN_BUY,
    CHIP_NEUTRAL,
    CHIP_SYNC_BUY,
    ENTRY_BREAKOUT,
    ENTRY_OVEREXTENDED,
    ENTRY_TAG_VOLUME,
    ENTRY_WAIT,
    PM_AVOID,
    PM_BREAKOUT,
    PM_OBSERVE,
    WL_EXCLUDED,
    WL_GENERAL,
)
from pm_watchlist import (
    build_pm_entries,
    has_high_chip_resonance,
    pm_bucket_for,
    qualifies_pm_list,
)
from research_universe import UniverseEntry
from score_engine import DimensionScores, ScoredEntry


def _scored(
    stock_id: str,
    *,
    flow: float,
    chip: float,
    watchlist: str = WL_GENERAL,
    entry_signal: str = ENTRY_WAIT,
    entry_tags: tuple[str, ...] = (),
) -> ScoredEntry:
    entry = UniverseEntry(stock_id, "測", "money", 1, None, 1.0, None, None)
    return ScoredEntry(
        entry=entry,
        dimensions=DimensionScores(
            flow, chip, 50.0, 50.0, 50.0, 50.0, 55.0
        ),
        watchlist=watchlist,
        position_intent=None,
        tech_risk_flag=None,
        entry_signal=entry_signal,
        entry_tags=entry_tags,
        chip_tag=CHIP_SYNC_BUY,
        metadata={},
    )


class TestPmBucket(unittest.TestCase):
    def test_strong_trend_not_avoid(self) -> None:
        ctx = EntryContext(ENTRY_OVEREXTENDED, (ENTRY_TAG_VOLUME,))
        self.assertEqual(
            pm_bucket_for(on_list=True, entry_ctx=ctx, chip_tag=CHIP_SYNC_BUY),
            PM_OBSERVE,
        )

    def test_plain_overextended_avoid(self) -> None:
        ctx = EntryContext(ENTRY_OVEREXTENDED, ())
        self.assertEqual(
            pm_bucket_for(on_list=True, entry_ctx=ctx, chip_tag=CHIP_NEUTRAL),
            PM_AVOID,
        )

    def test_overextended_high_chip_research(self) -> None:
        ctx = EntryContext(ENTRY_OVEREXTENDED, ())
        self.assertTrue(has_high_chip_resonance(chip_tag=CHIP_SYNC_BUY, chip_score=50.0))
        self.assertEqual(
            pm_bucket_for(
                on_list=True,
                entry_ctx=ctx,
                chip_tag=CHIP_SYNC_BUY,
                chip_score=95.0,
            ),
            PM_OBSERVE,
        )
        s = _scored(
            "6223",
            flow=54.0,
            chip=95.0,
            watchlist=WL_EXCLUDED,
            entry_signal=ENTRY_OVEREXTENDED,
        )
        self.assertTrue(
            qualifies_pm_list(
                s,
                chip_tag=CHIP_SYNC_BUY,
                entry_ctx=ctx,
            )
        )

    def test_triple_resonance_qualifies(self) -> None:
        s = _scored("6223", flow=56.0, chip=92.0, watchlist=WL_EXCLUDED)
        self.assertTrue(
            qualifies_pm_list(
                s,
                chip_tag=CHIP_SYNC_BUY,
                entry_ctx=EntryContext(ENTRY_WAIT, ()),
            )
        )


class TestBuildPmEntries(unittest.TestCase):
    def test_overextended_triple_resonance_research_bucket(self) -> None:
        s = _scored(
            "6223",
            flow=54.0,
            chip=95.0,
            watchlist=WL_EXCLUDED,
            entry_signal=ENTRY_OVEREXTENDED,
        )
        rows = build_pm_entries(
            [s],
            as_of_date="2026-06-04",
            chip_by_id={"6223": CHIP_SYNC_BUY},
        )
        self.assertEqual(rows[0].pm_bucket, PM_OBSERVE)
        self.assertEqual(rows[0].watchlist, WL_EXCLUDED)

    def test_sort_breakout_first(self) -> None:
        s1 = _scored(
            "2330",
            flow=80,
            chip=70,
            entry_signal=ENTRY_BREAKOUT,
        )
        s2 = _scored("6223", flow=60, chip=90)
        rows = build_pm_entries(
            [s1, s2],
            as_of_date="2026-06-04",
            chip_by_id={"2330": CHIP_FOREIGN_BUY, "6223": CHIP_SYNC_BUY},
        )
        self.assertEqual(rows[0].pm_bucket, PM_BREAKOUT)


if __name__ == "__main__":
    unittest.main()
