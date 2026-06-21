"""copytrade_leg_attribution · gap × p5d 归因。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from flow_returns import BENCHMARK_CODE
from stock_db import connect, upsert_daily_bars, upsert_etf_holdings, upsert_etf_holdings_meta, upsert_stock_daily_bars


def _seed_holdings(conn, snap: str, stocks: list[tuple[str, float]]) -> None:
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


class TestCopytradeLegAttribution(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_gap_and_p5d_bands(self) -> None:
        from research.backtest.copytrade_leg_attribution import _gap_band, _interaction_band, _p5d_band

        self.assertEqual(_gap_band(-8.0), "deep_down_lt_-6")
        self.assertEqual(_p5d_band(11.0), "hot_ge_8")
        self.assertEqual(_interaction_band(-8.0, 2.0), "deep_gap_cool_p5")

    def test_run_on_minimal_run(self) -> None:
        from research.backtest.copytrade_backtest import persist_copytrade_run, run_copytrade_backtest
        from research.backtest.copytrade_leg_attribution import run_leg_attribution_analysis

        _seed_holdings(self.conn, snap="2026-06-02", stocks=[("2330", 1000.0)])
        _seed_holdings(self.conn, snap="2026-06-03", stocks=[("2330", 1100.0)])
        dates = [("2026-06-04", 900.0, 910.0), ("2026-06-05", 910.0, 920.0)]
        upsert_stock_daily_bars(
            self.conn,
            [
                {
                    "stock_id": "2330",
                    "trade_date": d,
                    "open": op,
                    "high": max(op, cl) * 1.01,
                    "low": min(op, cl) * 0.99,
                    "close": cl,
                    "volume": 1000,
                    "source": "finmind",
                }
                for d, op, cl in dates
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
                for d in ("2026-06-04", "2026-06-05")
            ],
        )
        result = run_copytrade_backtest(
            self.conn,
            "00981A",
            strategy_id="L1H2",
            strategy_label="test",
            entry_lag_days=0,
            hold_trading_days=2,
            capital_ntd=10_000.0,
            run_id="test-run-l1h2-attrib",
        )
        persist_copytrade_run(self.conn, result)
        out = run_leg_attribution_analysis(
            self.conn,
            etf_code="00981A",
            strategy_id="L1H2",
            batch_id="test-leg-attrib",
            backfill_gaps=True,
            persist=True,
        )
        self.assertGreaterEqual(len(out["bucket_rows"]), 1)
        self.assertGreaterEqual(len(out["hypotheses"]), 1)


if __name__ == "__main__":
    unittest.main()
