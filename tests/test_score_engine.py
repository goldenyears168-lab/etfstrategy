"""P4-v2：score_engine 加權、Rule 觀察名單、風控閘門。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from score_engine import EntryContext
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
    risk_subscore,
    watchlist_tier,
)
from stock_db import connect, upsert_investment_scores, upsert_stock_beta


class TestWatchlistTier(unittest.TestCase):
    def test_a_requires_smart_money(self) -> None:
        sm = round(0.55 * 74 + 0.45 * 69, 1)
        self.assertEqual(
            watchlist_tier(
                76.0,
                sm,
                entry_ctx=EntryContext(ENTRY_WAIT, ()),
                flow_score=50.0,
            ),
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
        from unittest.mock import patch

        from project_config import SCORE_VERSION_P4
        import score_engine as se

        d = se.DimensionScores(
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
        with patch("project_config.active_score_version", return_value=SCORE_VERSION_P4):
            self.assertEqual(d.investment_score, expected)
        self.assertNotEqual(d.timing, d.risk)


class TestP5DimensionScores(unittest.TestCase):
    def test_p5v1_weighted_total(self) -> None:
        from unittest.mock import patch

        from project_config import SCORE_VERSION_P5
        import score_engine as se

        d = se.DimensionScores(
            flow=85,
            chip=75,
            catalyst=60,
            expectation=55,
            fundamental=50,
            risk=65,
            timing=70,
            crowd=78,
            short_favor=72,
        )
        expected = round(
            0.30 * 85
            + 0.20 * 75
            + 0.10 * 72
            + 0.10 * 78
            + 0.10 * 60
            + 0.10 * 55
            + 0.05 * 50
            + 0.05 * 65,
            1,
        )
        with patch("project_config.active_score_version", return_value=SCORE_VERSION_P5):
            self.assertEqual(d.investment_score, expected)


class TestP6DimensionScores(unittest.TestCase):
    def test_p6_tier_weighted_total(self) -> None:
        from unittest.mock import patch

        from project_config import SCORE_VERSION_P6
        import score_engine as se

        d = se.DimensionScores(
            flow=80,
            chip=95,
            catalyst=45,
            expectation=60,
            fundamental=70,
            risk=70,
            timing=78,
            crowd=28,
            short_favor=65,
        )
        expected = round(0.70 * 80 + 0.30 * 60, 1)
        with patch("project_config.active_score_version", return_value=SCORE_VERSION_P6):
            self.assertEqual(d.investment_score, expected)


class TestP6ChipGate(unittest.TestCase):
    def test_retail_follow_fails_gate(self) -> None:
        from score_engine import chip_gate_eval

        ok, flags = chip_gate_eval(
            crowd=28.0,
            short_favor=65.0,
            chip_ext={"crowd_label": "散戶跟風"},
        )
        self.assertFalse(ok)
        self.assertIn("Crowd偏低", flags)

    def test_clean_chip_passes(self) -> None:
        from score_engine import chip_gate_eval

        ok, flags = chip_gate_eval(crowd=78.0, short_favor=57.0, chip_ext={})
        self.assertTrue(ok)
        self.assertEqual(flags, [])


class TestP6WatchlistTier(unittest.TestCase):
    def test_chip_gate_blocks_primary(self) -> None:
        from unittest.mock import patch

        from project_config import SCORE_VERSION_P6
        from score_engine import watchlist_tier

        with patch("project_config.active_score_version", return_value=SCORE_VERSION_P6):
            tier = watchlist_tier(
                78.0,
                80.0,
                entry_ctx=EntryContext(ENTRY_WAIT, ()),
                flow_score=80.0,
                risk_score=70.0,
                crowd=28.0,
                short_favor=65.0,
                timing_score=78.0,
                chip_ext={"crowd_label": "散戶跟風"},
            )
        self.assertEqual(tier, WL_GENERAL)

    def test_weak_flow_caps_primary(self) -> None:
        from unittest.mock import patch

        from project_config import SCORE_VERSION_P6
        from score_engine import watchlist_tier

        with patch("project_config.active_score_version", return_value=SCORE_VERSION_P6):
            tier = watchlist_tier(
                76.0,
                70.0,
                entry_ctx=EntryContext(ENTRY_WAIT, ()),
                flow_score=20.0,
                risk_score=70.0,
                crowd=78.0,
                short_favor=57.0,
                timing_score=78.0,
            )
        self.assertEqual(tier, WL_CANDIDATE)


class TestCatalystSubscore(unittest.TestCase):
    def test_money_baseline(self) -> None:
        e = UniverseEntry("2330", "台積電", "money", 1, None, 1.2, None, None)
        self.assertEqual(catalyst_subscore(e), 45.0)

    def test_non_money_baseline(self) -> None:
        e = UniverseEntry("2330", "台積電", "event", None, 1, None, 0.85, None)
        self.assertEqual(catalyst_subscore(e), 40.0)


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
        self.assertEqual(SCORE_VERSION, "p6-tier")


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
