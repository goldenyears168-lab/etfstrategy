"""market_analytics：RS、籌碼連續、盈餘 proxy、回測、R:R。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from market_analytics import (
    _period_return_pct,
    _percentile_rank,
    _streak_positive,
    build_stock_analytics,
    compute_chip_verify,
    compute_rs_percentile_map,
    detect_breakout_retest,
)
from market_labels import ENTRY_TAG_RETEST
from stock_context import TechnicalSnapshot
from stock_db import (
    connect,
    upsert_stock_daily_bars,
    upsert_stock_financial_history,
    upsert_stock_institutional_daily,
)
from market_labels import VOL_FLAT


class TestMarketAnalyticsMath(unittest.TestCase):
    def test_period_return(self) -> None:
        closes = [100.0, 102.0, 105.0, 110.0]
        self.assertAlmostEqual(_period_return_pct(closes, 2), 7.84, places=1)

    def test_percentile_rank(self) -> None:
        self.assertEqual(_percentile_rank(50.0, [10.0, 20.0, 50.0, 80.0]), 62.5)

    def test_streak_positive(self) -> None:
        rows = [
            {"foreign_net": -1.0},
            {"foreign_net": 2.0},
            {"foreign_net": 1.0},
        ]
        self.assertEqual(_streak_positive(rows, "foreign_net"), 2)


class TestMarketAnalyticsDb(unittest.TestCase):
    def _tech(self, **kw) -> TechnicalSnapshot:
        base = dict(
            stock_id="2330",
            trade_date="2026-06-04",
            close=1000.0,
            ma20=990.0,
            ma60=950.0,
            dist_ma20_pct=1.0,
            dist_ma60_pct=5.0,
            high_52w=1050.0,
            low_52w=800.0,
            position_52w_pct=80.0,
            dist_from_52w_high_pct=-4.0,
            volume=1000,
            vol_avg_5d=900.0,
            vol_ratio_5d=1.0,
            vol_label=VOL_FLAT,
        )
        base.update(kw)
        return TechnicalSnapshot(**base)

    def test_chip_verify_dual_streak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            upsert_stock_institutional_daily(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "trade_date": f"2026-06-0{i}",
                        "close_price": 1000.0,
                        "foreign_net": 10_000_000.0,
                        "investment_trust_net": 5_000_000.0,
                        "dealer_self_net": 0.0,
                        "three_institution_net": 15_000_000.0,
                        "source": "finmind",
                    }
                    for i in range(1, 5)
                ],
            )
            f, t, label = compute_chip_verify(conn, "2330")
            self.assertEqual(f, 4)
            self.assertEqual(t, 4)
            self.assertIn("外資投信連", label or "")
            conn.close()

    def test_eps_revision_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            upsert_stock_financial_history(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "period_date": "2026-03-31",
                        "period_type": "quarter",
                        "metric": "eps",
                        "value": 11.0,
                        "source": "finmind",
                    },
                    {
                        "stock_id": "2330",
                        "period_date": "2025-12-31",
                        "period_type": "quarter",
                        "metric": "eps",
                        "value": 10.0,
                        "source": "finmind",
                    },
                ],
            )
            a = build_stock_analytics(
                conn, "2330", tech=self._tech(), entry_signal="拉回"
            )
            self.assertEqual(a.eps_revision, "上修")
            self.assertAlmostEqual(a.eps_qoq_pct, 10.0)
            conn.close()

    def test_retest_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            bars = []
            from datetime import date, timedelta

            start = date(2026, 4, 1)
            for i in range(30):
                d = (start + timedelta(days=i)).isoformat()
                close = 900.0 + i * 5.0
                if i >= 25:
                    close = 1040.0 - (29 - i) * 2
                bars.append(
                    {
                        "stock_id": "2330",
                        "trade_date": d,
                        "open": close,
                        "high": close + 5,
                        "low": close - 5,
                        "close": close,
                        "volume": 1000,
                        "source": "finmind",
                    }
                )
            upsert_stock_daily_bars(conn, bars)
            tech = self._tech(dist_ma20_pct=2.0, position_52w_pct=75.0, close=1030.0)
            self.assertTrue(detect_breakout_retest(conn, "2330", tech))
            from market_analytics import analytics_entry_tags

            a = build_stock_analytics(conn, "2330", tech=tech, entry_signal="拉回")
            self.assertIn(ENTRY_TAG_RETEST, analytics_entry_tags(a))
            conn.close()


if __name__ == "__main__":
    unittest.main()
