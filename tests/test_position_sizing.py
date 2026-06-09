"""Phase 3：risk_budget sizing（停損距離邊界）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from investment_policy import InvestmentPolicy, compute_risk_budget_qty
from order_intent_engine import build_intent_drafts
from stock_db import (
    connect,
    upsert_pm_watchlist,
    upsert_portfolio_weights,
    upsert_stock_daily_bars,
)


def _seed(conn, *, as_of: str = "2026-06-04", suggested_ntd: float = 20_000) -> None:
    upsert_pm_watchlist(
        conn,
        [
            {
                "stock_id": "2330",
                "as_of_date": as_of,
                "score_version": "p4-v2",
                "stock_name": "台積電",
                "investment_score": 72.0,
                "watchlist": "首要觀察",
                "entry_signal": "突破",
                "entry_tags_json": "[]",
                "chip_tag": "法人中性",
                "pm_bucket": "突破",
                "flow_score": 70,
                "chip_score": 50,
                "tech_score": 60,
                "catalyst_score": 0,
                "fundamental_score": 50,
                "note": "",
            },
        ],
    )
    upsert_portfolio_weights(
        conn,
        [
            {
                "stock_id": "2330",
                "as_of_date": as_of,
                "score_version": "p4-v2",
                "stock_name": "台積電",
                "watchlist": "首要觀察",
                "position_score": 70,
                "risk_score": 60,
                "portfolio_weight_pct": 20,
                "suggested_ntd": suggested_ntd,
                "capital_ntd": 100_000,
                "entry_signal": "突破",
                "entry_tags_json": "[]",
                "pm_bucket": "突破",
                "note": "",
            },
        ],
    )
    upsert_stock_daily_bars(
        conn,
        [
            {
                "stock_id": "2330",
                "trade_date": as_of,
                "open": 1000.0,
                "high": 1010.0,
                "low": 990.0,
                "close": 1000.0,
                "volume": 1000,
                "source": "finmind",
            },
        ],
    )


class TestRiskBudgetQty(unittest.TestCase):
    def test_use_stop_distance_flag_enables_risk(self) -> None:
        ips = InvestmentPolicy.from_dict(
            {
                "sizing_mode": "equal_cap",
                "use_stop_distance_for_qty": True,
                "risk_budget_pct_per_trade": 1.0,
            }
        )
        qty = compute_risk_budget_qty(
            suggested_ntd=20_000,
            ref_price=1_000.0,
            stop_price=100.0,
            size_scale=1.0,
            ips=ips,
        )
        self.assertEqual(qty, 1)

    def test_equal_cap_ignores_stop(self) -> None:
        ips = InvestmentPolicy.from_dict({"sizing_mode": "equal_cap"})
        qty = compute_risk_budget_qty(
            suggested_ntd=20_000,
            ref_price=1_000.0,
            stop_price=970.0,
            size_scale=1.0,
            ips=ips,
        )
        self.assertEqual(qty, 20)

    def test_tiny_stop_distance_capped_by_equal_cap(self) -> None:
        """停損極近 → qty_risk 很大，但不得超過等權 cap。"""
        ips = InvestmentPolicy.from_dict(
            {
                "capital_ntd": 100_000,
                "sizing_mode": "risk_budget",
                "risk_budget_pct_per_trade": 1.0,
                "min_risk_reward": 1.0,
            }
        )
        ref = 1_000.0
        stop = 999.0
        qty_cap = 20
        qty = compute_risk_budget_qty(
            suggested_ntd=20_000,
            ref_price=ref,
            stop_price=stop,
            size_scale=1.0,
            ips=ips,
        )
        self.assertEqual(qty, qty_cap)
        self.assertLess(qty * (ref - stop), ips.capital_ntd)

    def test_wide_stop_distance_reduces_qty(self) -> None:
        """停損極遠 → qty_risk 小於等權 cap。"""
        ips = InvestmentPolicy.from_dict(
            {
                "capital_ntd": 100_000,
                "sizing_mode": "risk_budget",
                "risk_budget_pct_per_trade": 1.0,
            }
        )
        ref = 1_000.0
        stop = 100.0
        qty_risk = int(1_000 // (ref - stop))
        qty = compute_risk_budget_qty(
            suggested_ntd=20_000,
            ref_price=ref,
            stop_price=stop,
            size_scale=1.0,
            ips=ips,
        )
        self.assertEqual(qty_risk, 1)
        self.assertEqual(qty, 1)
        self.assertLess(qty, 20)

    def test_build_intent_drafts_risk_budget_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "t.db")
            _seed(conn)
            ips = InvestmentPolicy.from_dict(
                {
                    "min_risk_reward": 1.0,
                    "sizing_mode": "risk_budget",
                    "risk_budget_pct_per_trade": 1.0,
                }
            )
            drafts = build_intent_drafts(
                conn,
                trade_date="2026-06-06",
                ips=ips,
            )
            self.assertEqual(len(drafts), 1)
            d = drafts[0]
            self.assertGreater(d.qty, 0)
            self.assertLessEqual(d.qty, int(20_000 // d.ref_price))
            conn.close()


if __name__ == "__main__":
    unittest.main()
