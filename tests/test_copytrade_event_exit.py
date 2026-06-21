"""copytrade_event_exit · 轨 C 事件出场。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from flow_returns import BENCHMARK_CODE
from stock_db import (
    connect,
    upsert_daily_bars,
    upsert_etf_holdings,
    upsert_etf_holdings_meta,
    upsert_stock_daily_bars,
)


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


class TestCopytradeEventExit(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_reduce_triggers_early_exit(self) -> None:
        from research.backtest.copytrade_event_exit import collect_leg_exit_results, iter_holdings_events

        _seed_holdings(self.conn, snap="2026-06-02", stocks=[("2330", 1000.0)])
        _seed_holdings(self.conn, snap="2026-06-03", stocks=[("2330", 1000.0), ("2317", 500.0)])
        _seed_holdings(self.conn, snap="2026-06-05", stocks=[("2330", 1000.0), ("2317", 300.0)])
        _seed_bars(
            self.conn,
            "2317",
            [
                ("2026-06-04", 100.0, 102.0),
                ("2026-06-05", 102.0, 103.0),
                ("2026-06-06", 103.0, 104.0),
                ("2026-06-09", 104.0, 105.0),
                ("2026-06-10", 105.0, 106.0),
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
                for d in (
                    "2026-06-04",
                    "2026-06-05",
                    "2026-06-06",
                    "2026-06-09",
                    "2026-06-10",
                )
            ],
        )

        events = iter_holdings_events(self.conn, "00981A")
        reduce_ev = [e for e in events if e.stock_id == "2317" and e.action == "减码"]
        self.assertEqual(len(reduce_ev), 1)

        baseline = collect_leg_exit_results(
            self.conn, "00981A", policy_id="baseline_h20", baseline_h=5
        )
        early = collect_leg_exit_results(
            self.conn,
            "00981A",
            policy_id="exit_reduce_clear",
            baseline_h=5,
            baseline_alpha_map={
                (r.signal_date, r.stock_id): r.alpha_ntd for r in baseline
            },
        )
        leg = next(r for r in early if r.stock_id == "2317")
        self.assertTrue(leg.triggered)
        self.assertLess(leg.actual_exit_date, leg.planned_exit_date)
        self.assertEqual(leg.exit_reason, "减码")

    def test_run_event_exit_analysis(self) -> None:
        from research.backtest.copytrade_event_exit import run_event_exit_analysis

        _seed_holdings(self.conn, snap="2026-06-02", stocks=[("2330", 1000.0)])
        _seed_holdings(self.conn, snap="2026-06-03", stocks=[("2330", 1100.0)])
        _seed_bars(
            self.conn,
            "2330",
            [("2026-06-04", 900.0, 910.0), ("2026-06-05", 910.0, 920.0)],
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
        out = run_event_exit_analysis(
            self.conn,
            etf_code="00981A",
            batch_id="test-event-exit",
            baseline_h=2,
            rotation_capital_ntd=None,
            persist=True,
        )
        self.assertGreaterEqual(len(out["summaries"]), 1)


if __name__ == "__main__":
    unittest.main()
