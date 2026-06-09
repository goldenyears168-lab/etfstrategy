"""P4-v2：score_engine 加權、Rule 觀察名單、風控閘門。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from entry_signal import EntryContext
from market_labels import (
    CHIP_SYNC_BUY,
    ENTRY_OVEREXTENDED,
    ENTRY_TAG_VOLUME,
    ENTRY_WAIT,
    WL_CANDIDATE,
    WL_EXCLUDED,
    WL_GENERAL,
    WL_PRIMARY,
)
from research_universe import UniverseEntry
from score_engine import (
    SCORE_VERSION,
    DimensionScores,
    catalyst_subscore,
    catalyst_subscore_capped,
    risk_subscore,
    watchlist_tier,
)
from stock_db import connect, upsert_investment_scores, upsert_stock_beta


class TestWatchlistTier(unittest.TestCase):
    def test_a_requires_smart_money(self) -> None:
        sm = round(0.55 * 74 + 0.45 * 69, 1)
        self.assertEqual(
            watchlist_tier(76.0, sm, entry_ctx=EntryContext(ENTRY_WAIT, ())),
            WL_GENERAL,
        )
        self.assertEqual(
            watchlist_tier(76.0, 75.0, entry_ctx=EntryContext(ENTRY_WAIT, ())),
            WL_PRIMARY,
        )

    def test_overextended_capped_to_b_without_strong_trend(self) -> None:
        self.assertEqual(
            watchlist_tier(90.0, 90.0, entry_ctx=EntryContext(ENTRY_OVEREXTENDED, ())),
            WL_GENERAL,
        )

    def test_overextended_strong_trend_can_be_a(self) -> None:
        self.assertEqual(
            watchlist_tier(
                76.0,
                75.0,
                entry_ctx=EntryContext(ENTRY_OVEREXTENDED, (ENTRY_TAG_VOLUME,)),
            ),
            WL_PRIMARY,
        )

    def test_b_and_candidate(self) -> None:
        self.assertEqual(
            watchlist_tier(70.0, 50.0, entry_ctx=EntryContext(ENTRY_WAIT, ())),
            WL_GENERAL,
        )
        self.assertEqual(
            watchlist_tier(60.0, 50.0, entry_ctx=EntryContext(ENTRY_WAIT, ())),
            WL_CANDIDATE,
        )
        self.assertEqual(
            watchlist_tier(50.0, 90.0, entry_ctx=EntryContext(ENTRY_WAIT, ())),
            WL_EXCLUDED,
        )


class TestDimensionScores(unittest.TestCase):
    def test_p4v2_weighted_total(self) -> None:
        d = DimensionScores(
            flow=90,
            chip=80,
            catalyst=50,
            expectation=60,
            fundamental=55,
            risk=70,
            timing=32,
        )
        sm = d.smart_money
        expected = round(
            0.50 * sm + 0.15 * 60 + 0.15 * 55 + 0.10 * 70,
            1,
        )
        self.assertEqual(d.investment_score, expected)
        self.assertNotEqual(d.timing, d.risk)


class TestCatalystSubscore(unittest.TestCase):
    def test_event_maps_to_0_100(self) -> None:
        e = UniverseEntry("2330", "台積電", "event", None, 1, None, 0.85, None)
        self.assertEqual(catalyst_subscore(e), 85.0)

    def test_money_baseline(self) -> None:
        e = UniverseEntry("2330", "台積電", "money", 1, None, 1.2, None, None)
        self.assertEqual(catalyst_subscore(e), 45.0)

    def test_capped_on_low_confidence(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        conn = connect(Path(tmp.name) / "t.db")
        try:
            conn.execute(
                """
                INSERT INTO catalyst_events (
                    event_id, stock_id, event_date, catalyst_type, headline,
                    polarity, explains_etf_add, confidence, sources_json,
                    source, ingested_at
                ) VALUES (
                    'e1', '6223', '2026-06-01', 'CAPX', 'CoWoS擴產', 'POSITIVE',
                    'MED', 40, '[]', 'manual', '2026-06-01T00:00:00Z'
                )
                """
            )
            conn.commit()
            e = UniverseEntry("6223", "旺矽", "event", None, 1, None, 0.85, None)
            capped = catalyst_subscore_capped(e, conn)
            self.assertLessEqual(capped, 55.0)
        finally:
            conn.close()
            tmp.cleanup()


class TestRiskSubscore(unittest.TestCase):
    def test_tsm_penalty(self) -> None:
        tech = {"session_date": "2026-06-04", "tsm_daily_return_pct": -2.5}
        score, flag = risk_subscore("2330", beta_row=None, tech_risk=tech)
        self.assertLess(score, 70.0)
        self.assertEqual(flag, "TSM_ADR_LT_-2PCT")

    def test_high_beta(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        conn = connect(Path(tmp.name) / "t.db")
        try:
            upsert_stock_beta(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "name": "台積電",
                        "market": "TWSE",
                        "beta": 1.65,
                        "beta_window": "252d",
                        "benchmark": "^TWII",
                        "source": "test",
                        "as_of_date": "2026-06-01",
                    }
                ],
            )
            row = conn.execute(
                "SELECT * FROM stock_beta WHERE stock_id='2330'"
            ).fetchone()
            score, flag = risk_subscore("2330", beta_row=row, tech_risk=None)
            self.assertEqual(score, 45.0)
            self.assertEqual(flag, "HIGH_BETA")
        finally:
            conn.close()
            tmp.cleanup()


class TestScoreVersion(unittest.TestCase):
    def test_version_string(self) -> None:
        self.assertEqual(SCORE_VERSION, "p4-v2")


class TestInvestmentScoresDb(unittest.TestCase):
    def test_upsert_idempotent(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        conn = connect(Path(tmp.name) / "t.db")
        try:
            row = {
                "stock_id": "2330",
                "as_of_date": "2026-06-04",
                "score_version": SCORE_VERSION,
                "stock_name": "台積電",
                "smart_money": 85.0,
                "catalyst": 70.0,
                "expectation": 60.0,
                "fundamental": 50.0,
                "risk": 65.0,
                "investment_score": 78.0,
                "watchlist": WL_GENERAL,
                "pool_reason": "both",
                "money_rank": 1,
                "event_rank": 2,
                "position_intent": "ACCUMULATE",
                "tech_risk_flag": None,
                "metadata_json": json.dumps({"chip_tag": CHIP_SYNC_BUY}),
            }
            self.assertEqual(upsert_investment_scores(conn, [row]), 1)
            cnt = conn.execute(
                "SELECT expectation, risk FROM investment_scores WHERE stock_id='2330'"
            ).fetchone()
            self.assertEqual(cnt["expectation"], 60.0)
            self.assertEqual(cnt["risk"], 65.0)
        finally:
            conn.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
