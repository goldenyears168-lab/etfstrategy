"""copytrade_leg_decay · Leg 级 forward α 衰减。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from flow_returns import BENCHMARK_CODE
from stock_db import connect, upsert_daily_bars, upsert_etf_holdings, upsert_etf_holdings_meta, upsert_stock_daily_bars


def _seed_holdings(
    conn: sqlite3.Connection,
    *,
    snap: str,
    stocks: list[tuple[str, float]],
) -> None:
    synced = "2026-06-01T00:00:00+00:00"
    upsert_etf_holdings_meta(
        conn,
        {
            "etf_code": "00981A",
            "snapshot_date": snap,
            "nav": 100.0,
            "holding_count": len(stocks),
            "source": "test",
            "source_edit_at": None,
            "synced_at": synced,
        },
    )
    upsert_etf_holdings(
        conn,
        [
            {
                "etf_code": "00981A",
                "snapshot_date": snap,
                "stock_id": sid,
                "stock_name": sid,
                "shares": shares,
                "weight_pct": 5.0,
                "amount": None,
                "source": "test",
                "source_edit_at": None,
                "synced_at": synced,
            }
            for sid, shares in stocks
        ],
    )


def _seed_bars(
    conn: sqlite3.Connection,
    stock_id: str,
    rows: list[tuple[str, float, float]],
) -> None:
    upsert_stock_daily_bars(
        conn,
        [
            {
                "stock_id": stock_id,
                "trade_date": d,
                "open": op,
                "high": max(op, cl) * 1.01,
                "low": min(op, cl) * 0.99,
                "close": cl,
                "volume": 1000,
                "source": "finmind",
            }
            for d, op, cl in rows
        ],
    )


class TestCopytradeLegDecay(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_collect_leg_horizon_observations(self) -> None:
        from research.backtest.copytrade_leg_decay import collect_leg_horizon_observations

        _seed_holdings(self.conn, snap="2026-06-02", stocks=[("2330", 1000.0)])
        _seed_holdings(
            self.conn,
            snap="2026-06-03",
            stocks=[("2330", 1000.0), ("2317", 500.0)],
        )
        _seed_bars(
            self.conn,
            "2317",
            [
                ("2026-06-04", 100.0, 105.0),
                ("2026-06-05", 105.0, 108.0),
                ("2026-06-06", 108.0, 110.0),
            ],
        )
        upsert_daily_bars(
            self.conn,
            [
                {
                    "code": BENCHMARK_CODE,
                    "date": d,
                    "open": 100.0,
                    "high": 100.0,
                    "low": 100.0,
                    "close": 100.0,
                    "volume": 1,
                    "spread": None,
                    "source": "tej",
                }
                for d in ("2026-06-04", "2026-06-05", "2026-06-06")
            ],
        )

        obs = collect_leg_horizon_observations(
            self.conn, "00981A", max_horizon=3, leg_capital_ntd=10_000.0
        )
        self.assertGreaterEqual(len(obs), 3)
        h1 = [o for o in obs if o.stock_id == "2317" and o.horizon == 1]
        self.assertEqual(len(h1), 1)
        self.assertAlmostEqual(h1[0].excess_pct, 5.0, places=2)
        self.assertAlmostEqual(h1[0].alpha_ntd, 500.0, places=0)

    def test_aggregate_and_knee(self) -> None:
        from research.backtest.copytrade_leg_decay import (
            LegHorizonObs,
            aggregate_leg_decay_curves,
            summarize_leg_decay_knees,
        )

        obs = [
            LegHorizonObs(
                signal_date="2026-06-03",
                stock_id="2317",
                action="新进",
                entry_date="2026-06-04",
                exit_date="2026-06-05",
                horizon=h,
                allocated_ntd=10_000.0,
                return_pct=float(h),
                bench_return_pct=0.0,
                excess_pct=float(h),
                alpha_ntd=100.0 * h,
                multi_leg_day=False,
            )
            for h in (1, 2, 3)
        ]
        curves = aggregate_leg_decay_curves(obs, etf_code="00981A", max_horizon=3)
        self.assertTrue(any(r["horizon"] == 3 for r in curves))
        knees = summarize_leg_decay_knees(curves, bucket_field="all")
        self.assertEqual(len(knees), 1)
        self.assertEqual(knees[0]["peak_mean_excess_h"], 3)


if __name__ == "__main__":
    unittest.main()
