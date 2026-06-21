"""copytrade_backtest · 跟單回測核心邏輯。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from research.backtest.copytrade_backtest import (
    compute_signal_day,
    group_signals_by_date,
    iter_copytrade_signals,
    persist_copytrade_run,
    run_copytrade_backtest,
)
from stock_db import (
    connect,
    load_copytrade_legs_for_run,
    load_copytrade_runs,
    load_copytrade_signal_days_for_run,
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


class TestCopytradeBacktest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_signal_generation_and_persist(self) -> None:
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
                ("2026-06-04", 100.0, 102.0),
                ("2026-06-05", 102.0, 101.0),
            ],
        )
        _seed_bars(
            self.conn,
            "2330",
            [
                ("2026-06-04", 900.0, 910.0),
                ("2026-06-05", 910.0, 905.0),
            ],
        )

        signals = iter_copytrade_signals(self.conn, "00981A")
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].stock_id, "2317")
        self.assertEqual(signals[0].signal_date, "2026-06-03")

        result = run_copytrade_backtest(
            self.conn,
            "00981A",
            strategy_id="L1H1",
            strategy_label="test",
            entry_lag_days=0,
            hold_trading_days=1,
            capital_ntd=10_000.0,
            run_id="test-run-l1h1",
        )
        self.assertEqual(result.n_complete_days, 1)
        self.assertAlmostEqual(result.total_pnl_ntd, 200.0, places=1)
        self.assertIsNotNone(result.mean_excess_pct)

        persist_copytrade_run(self.conn, result)
        runs = load_copytrade_runs(self.conn, etf_code="00981A")
        self.assertEqual(len(runs), 1)
        days = load_copytrade_signal_days_for_run(self.conn, "test-run-l1h1")
        self.assertEqual(len(days), 1)
        legs = load_copytrade_legs_for_run(self.conn, "test-run-l1h1")
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0]["stock_id"], "2317")

    def test_equal_weight_two_legs(self) -> None:
        _seed_holdings(self.conn, snap="2026-06-02", stocks=[("2330", 1000.0)])
        _seed_holdings(
            self.conn,
            snap="2026-06-03",
            stocks=[("2330", 1000.0), ("2317", 500.0), ("2454", 300.0)],
        )
        for sid, ret in (("2317", 2.0), ("2454", 4.0)):
            _seed_bars(
                self.conn,
                sid,
                [("2026-06-04", 100.0, 100.0 * (1 + ret / 100))],
            )
        grouped = group_signals_by_date(
            iter_copytrade_signals(self.conn, "00981A")
        )
        day = compute_signal_day(
            self.conn,
            "2026-06-03",
            grouped["2026-06-03"],
            capital_ntd=10_000.0,
            entry_lag_days=0,
            hold_trading_days=1,
            cost_bps=0.0,
            entry_price_mode="open",
            beta_map={},
        )
        self.assertEqual(day.status, "complete")
        self.assertEqual(day.n_legs, 2)
        self.assertAlmostEqual(day.deployed_ntd, 10_000.0)
        # 5000*2% + 5000*4% = 100 + 200 = 300
        self.assertAlmostEqual(day.pnl_ntd, 300.0, places=1)

    def test_l0_same_day_open_and_close_entry(self) -> None:
        _seed_holdings(self.conn, snap="2026-06-02", stocks=[("2330", 1000.0)])
        _seed_holdings(
            self.conn,
            snap="2026-06-03",
            stocks=[("2330", 1000.0), ("2317", 500.0)],
        )
        _seed_bars(
            self.conn,
            "2317",
            [("2026-06-03", 100.0, 105.0)],
        )
        grouped = group_signals_by_date(
            iter_copytrade_signals(self.conn, "00981A")
        )
        beta_map = {}
        l0o = compute_signal_day(
            self.conn,
            "2026-06-03",
            grouped["2026-06-03"],
            capital_ntd=10_000.0,
            entry_lag_days=-1,
            hold_trading_days=1,
            cost_bps=0.0,
            entry_price_mode="open",
            beta_map=beta_map,
        )
        self.assertEqual(l0o.status, "complete")
        self.assertEqual(l0o.entry_date, "2026-06-03")
        self.assertAlmostEqual(l0o.pnl_ntd, 500.0, places=1)

        l0c = compute_signal_day(
            self.conn,
            "2026-06-03",
            grouped["2026-06-03"],
            capital_ntd=10_000.0,
            entry_lag_days=-1,
            hold_trading_days=1,
            cost_bps=0.0,
            entry_price_mode="close",
            beta_map=beta_map,
        )
        self.assertEqual(l0c.status, "complete")
        self.assertAlmostEqual(l0c.pnl_ntd, 0.0, places=1)

    def test_matrix_strategy_count(self) -> None:
        from research.backtest.copytrade_backtest import MATRIX_STRATEGIES, build_matrix_strategies

        self.assertEqual(len(MATRIX_STRATEGIES), 25)
        self.assertEqual(len(build_matrix_strategies(include_l0=False)), 15)
        self.assertEqual(len(build_matrix_strategies(include_l0=True, max_hold=20)), 100)
        self.assertEqual(len(build_matrix_strategies(include_l0=False, max_hold=20)), 60)

    def test_capital_recycling_skips_overlapping_signals(self) -> None:
        from research.backtest.copytrade_backtest import (
            simulate_capital_recycling,
            summarize_capital_cycle_insights,
        )

        _seed_holdings(self.conn, snap="2026-06-02", stocks=[("2330", 1000.0)])
        for snap, extra in (
            ("2026-06-03", [("2317", 500.0)]),
            ("2026-06-04", [("2454", 300.0)]),
        ):
            _seed_holdings(
                self.conn,
                snap=snap,
                stocks=[("2330", 1000.0), *extra],
            )
        for sid, rows in (
            (
                "2317",
                [
                    ("2026-06-04", 100.0, 102.0),
                    ("2026-06-05", 102.0, 103.0),
                    ("2026-06-06", 103.0, 104.0),
                ],
            ),
            (
                "2454",
                [
                    ("2026-06-05", 200.0, 204.0),
                    ("2026-06-06", 204.0, 208.0),
                ],
            ),
        ):
            _seed_bars(self.conn, sid, rows)

        result_h1 = run_copytrade_backtest(
            self.conn,
            "00981A",
            strategy_id="L1H1",
            strategy_label="h1",
            entry_lag_days=0,
            hold_trading_days=1,
            run_id="recycle-h1",
        )
        result_h2 = run_copytrade_backtest(
            self.conn,
            "00981A",
            strategy_id="L1H2",
            strategy_label="h2",
            entry_lag_days=0,
            hold_trading_days=2,
            run_id="recycle-h2",
        )
        days_h1 = [
            {
                "signal_date": d.signal_date,
                "entry_date": d.entry_date,
                "exit_date": d.exit_date,
                "alpha_ntd": d.alpha_ntd,
                "pnl_ntd": d.pnl_ntd,
                "status": d.status,
            }
            for d in result_h1.signal_days
        ]
        days_h2 = [
            {
                "signal_date": d.signal_date,
                "entry_date": d.entry_date,
                "exit_date": d.exit_date,
                "alpha_ntd": d.alpha_ntd,
                "pnl_ntd": d.pnl_ntd,
                "status": d.status,
            }
            for d in result_h2.signal_days
        ]
        sim_h1 = simulate_capital_recycling(self.conn, days_h1)
        sim_h2 = simulate_capital_recycling(self.conn, days_h2)
        self.assertEqual(sim_h1["recycled_n_cycles"], 2)
        self.assertEqual(sim_h2["recycled_n_cycles"], 1)
        self.assertGreater(
            float(sim_h1["recycled_total_alpha_ntd"] or 0),
            float(sim_h2["recycled_total_alpha_ntd"] or 0),
        )
        cycle_rows = [
            {
                "entry_row": "L1",
                "horizon": 1,
                "recycled_total_alpha_ntd": sim_h1["recycled_total_alpha_ntd"],
                "recycled_n_cycles": sim_h1["recycled_n_cycles"],
                "alpha_per_locked_day": sim_h1["alpha_per_locked_day"],
                "marginal_recycled_alpha_ntd": sim_h1["recycled_total_alpha_ntd"],
                "is_significant": 0,
            },
            {
                "entry_row": "L1",
                "horizon": 2,
                "recycled_total_alpha_ntd": sim_h2["recycled_total_alpha_ntd"],
                "recycled_n_cycles": sim_h2["recycled_n_cycles"],
                "alpha_per_locked_day": sim_h2["alpha_per_locked_day"],
                "marginal_recycled_alpha_ntd": (
                    float(sim_h2["recycled_total_alpha_ntd"] or 0)
                    - float(sim_h1["recycled_total_alpha_ntd"] or 0)
                ),
                "is_significant": 0,
            },
        ]
        ins = summarize_capital_cycle_insights(cycle_rows, "L1")
        self.assertEqual(ins["sweet_spot_h"], 1)

    def test_fixed_slots_allows_parallel_positions(self) -> None:
        from research.backtest.copytrade_backtest import simulate_capital_recycling, simulate_fixed_slots

        _seed_holdings(self.conn, snap="2026-06-02", stocks=[("2330", 1000.0)])
        for snap, extra in (
            ("2026-06-03", [("2317", 500.0)]),
            ("2026-06-04", [("2454", 300.0)]),
        ):
            _seed_holdings(
                self.conn,
                snap=snap,
                stocks=[("2330", 1000.0), *extra],
            )
        for sid, rows in (
            (
                "2317",
                [
                    ("2026-06-04", 100.0, 102.0),
                    ("2026-06-05", 102.0, 103.0),
                    ("2026-06-06", 103.0, 104.0),
                ],
            ),
            (
                "2454",
                [
                    ("2026-06-05", 200.0, 204.0),
                    ("2026-06-06", 204.0, 208.0),
                    ("2026-06-07", 208.0, 212.0),
                ],
            ),
        ):
            _seed_bars(self.conn, sid, rows)

        result_h2 = run_copytrade_backtest(
            self.conn,
            "00981A",
            strategy_id="L1H2",
            strategy_label="h2",
            entry_lag_days=0,
            hold_trading_days=2,
            run_id="slots-h2",
        )
        days = [
            {
                "signal_date": d.signal_date,
                "entry_date": d.entry_date,
                "exit_date": d.exit_date,
                "alpha_ntd": d.alpha_ntd,
                "pnl_ntd": d.pnl_ntd,
                "status": d.status,
            }
            for d in result_h2.signal_days
        ]
        single = simulate_capital_recycling(self.conn, days)
        one_slot = simulate_fixed_slots(self.conn, days, n_slots=1)
        two_slots = simulate_fixed_slots(self.conn, days, n_slots=2)

        self.assertEqual(one_slot["recycled_n_cycles"], single["recycled_n_cycles"])
        self.assertGreaterEqual(
            int(two_slots["recycled_n_cycles"] or 0),
            int(single["recycled_n_cycles"] or 0),
        )
        self.assertGreaterEqual(
            float(two_slots["recycled_total_alpha_ntd"] or 0),
            float(single["recycled_total_alpha_ntd"] or 0),
        )

    def test_weight_pct_allocation_differs_from_equal(self) -> None:
        from research.backtest.copytrade_backtest import ALLOCATION_EQUAL, ALLOCATION_WEIGHT_PCT, CopytradeSignal, leg_allocations_ntd

        legs = [
            CopytradeSignal("2026-06-03", "2330", "台積電", "加码", 100, 1.0, 8.0),
            CopytradeSignal("2026-06-03", "2317", "鴻海", "新进", 50, 2.0, 2.0),
        ]
        eq = leg_allocations_ntd(legs, 10_000.0, ALLOCATION_EQUAL)
        wt = leg_allocations_ntd(legs, 10_000.0, ALLOCATION_WEIGHT_PCT)
        self.assertAlmostEqual(sum(eq), 10_000.0)
        self.assertAlmostEqual(sum(wt), 10_000.0)
        self.assertNotAlmostEqual(eq[0], wt[0])
        self.assertAlmostEqual(wt[0], 10_000.0 * 8 / 10)

    def test_leg_allocations_from_multipliers(self) -> None:
        from research.backtest.copytrade_backtest import leg_allocations_from_multipliers

        allocs = leg_allocations_from_multipliers([1.5, 0.5, 1.0], 10_000.0)
        self.assertAlmostEqual(sum(allocs), 10_000.0)
        self.assertAlmostEqual(allocs[0], 10_000.0 * 1.5 / 3.0)

if __name__ == "__main__":
    unittest.main()
