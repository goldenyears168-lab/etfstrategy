"""copytrade_etf_compare · §4.4 跟單 vs ETF。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from research.backtest.copytrade_backtest import persist_copytrade_run, run_copytrade_backtest
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


class TestCopytradeEtfCompare(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_etf_return_and_paired_compare(self) -> None:
        from research.backtest.copytrade_etf_compare import (
            compare_copytrade_vs_etf,
            etf_return_entry_to_exit,
            run_etf_compare_analysis,
        )

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
                ("2026-06-04", 100.0, 110.0),
                ("2026-06-05", 110.0, 112.0),
            ],
        )
        _seed_bars(
            self.conn,
            "2330",
            [
                ("2026-06-04", 900.0, 900.0),
                ("2026-06-05", 900.0, 900.0),
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
        upsert_daily_bars(
            self.conn,
            [
                {
                    "code": "00981A",
                    "date": d,
                    "open": op,
                    "high": max(op, cl),
                    "low": min(op, cl),
                    "close": cl,
                    "volume": 1,
                    "spread": None,
                    "source": "tej",
                }
                for d, op, cl in (
                    ("2026-06-04", 10.0, 10.0),
                    ("2026-06-05", 10.0, 10.5),
                )
            ],
        )

        result = run_copytrade_backtest(
            self.conn,
            "00981A",
            strategy_id="L1H1",
            strategy_label="h1",
            entry_lag_days=0,
            hold_trading_days=1,
            run_id="etf-compare-h1",
        )
        persist_copytrade_run(self.conn, result)

        etf_ret = etf_return_entry_to_exit(
            self.conn, "00981A", "2026-06-04", "2026-06-05"
        )
        self.assertAlmostEqual(etf_ret or 0, 5.0, places=2)

        days = [
            {
                "signal_date": d.signal_date,
                "entry_date": d.entry_date,
                "exit_date": d.exit_date,
                "return_pct": d.return_pct,
                "pnl_ntd": d.pnl_ntd,
                "alpha_ntd": d.alpha_ntd,
                "status": d.status,
            }
            for d in result.signal_days
            if d.status == "complete"
        ]
        row = compare_copytrade_vs_etf(
            self.conn,
            days,
            etf_code="00981A",
            per_signal_ntd=10_000.0,
        )
        self.assertGreater(row.diff_gross_ntd, 0)
        self.assertGreater(row.win_rate_pct or 0, 50.0)

        out = run_etf_compare_analysis(
            self.conn,
            etf_code="00981A",
            strategy_id="L1H1",
            batch_id="test-etf-compare",
            capital_ntd=10_000.0,
            slots_mode="unconstrained",
            persist=True,
        )
        self.assertIn(out["verdict"], ("support", "inconclusive"))
        all_row = out["all_signals"]
        self.assertGreater(all_row.diff_gross_ntd, 0)


if __name__ == "__main__":
    unittest.main()
