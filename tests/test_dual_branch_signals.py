"""Dual-branch copytrade signals · mode/metric boundaries."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from copytrade.dual_branch_signals import (
    DualBranchTapeCache,
    iter_dual_branch_buy_signals,
)
from copytrade.signals import CopytradeSignal
from research.backtest.amount_rrg_copytrade_funnel_study import (
    FunnelSpec,
    _passes,
    max_drawdown_ntd,
    phi_binary,
)
from research.backtest.fubon_xindian_filter_study import (
    DEFAULT_READINESS_AMOUNT_NTD,
    FilterConfig,
    build_config_grouped,
    evaluate_forward_gate,
    evaluate_strategy_readiness_gates,
)
from stock_db import connect, upsert_stock_broker_branch_daily, upsert_stock_daily_bars

SOURCE = "finmind"
SJ = "9801"
XD = "9661"


def _seed_bars(
    conn: sqlite3.Connection,
    stock_id: str,
    dates: list[str],
    *,
    close: float = 100.0,
    volume: float = 500_000.0,
) -> None:
    rows = [
        {
            "stock_id": stock_id,
            "trade_date": d,
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": volume,
            "source": SOURCE,
        }
        for d in dates
    ]
    upsert_stock_daily_bars(conn, rows)


def _seed_tape(
    conn: sqlite3.Connection,
    trader_id: str,
    trader_name: str,
    trade_date: str,
    nets: list[tuple[str, float]],
) -> None:
    rows = []
    for sid, net in nets:
        buy = max(net, 0.0) + 10_000.0
        sell = buy - net
        rows.append(
            {
                "trade_date": trade_date,
                "securities_trader_id": trader_id,
                "securities_trader": trader_name,
                "stock_id": sid,
                "buy": buy,
                "sell": sell,
                "net": net,
                "source": SOURCE,
            }
        )
    upsert_stock_broker_branch_daily(conn, rows)


class DualBranchSignalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.conn = connect(self.db)
        dates = [f"2025-01-{d:02d}" for d in range(2, 22)]
        self.day = "2025-01-21"
        for sid, px in (("2330", 1000.0), ("2317", 100.0), ("1101", 50.0)):
            _seed_bars(self.conn, sid, dates, close=px)
        # SJ heavy lots on cheap 1101; XD heavy amount on expensive 2330
        _seed_tape(
            self.conn,
            SJ,
            "元大-松江",
            self.day,
            [("1101", 600_000.0), ("2317", 80_000.0)],
        )
        _seed_tape(
            self.conn,
            XD,
            "富邦-新店",
            self.day,
            [("2330", 60_000.0), ("1101", 600_000.0), ("2317", 200_000.0)],
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_lots_top1_sj(self) -> None:
        sigs = iter_dual_branch_buy_signals(
            self.conn,
            mode="sj",
            metric="lots",
            min_threshold=50_000.0,
            top_n=1,
            window_start="2025-01-01",
            window_end=self.day,
        )
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].stock_id, "1101")

    def test_amount_top1_prefers_high_price(self) -> None:
        # 2330: 60k shares * 1000 = 60M; 1101: 600k * 50 = 30M
        sigs = iter_dual_branch_buy_signals(
            self.conn,
            mode="xd",
            metric="amount",
            min_threshold=5_000_000.0,
            top_n=1,
            window_start="2025-01-01",
            window_end=self.day,
        )
        self.assertEqual(sigs[0].stock_id, "2330")

    def test_and_requires_both(self) -> None:
        sigs = iter_dual_branch_buy_signals(
            self.conn,
            mode="and",
            metric="lots",
            min_threshold=500_000.0,
            top_n=3,
            window_start="2025-01-01",
            window_end=self.day,
        )
        ids = {s.stock_id for s in sigs}
        self.assertEqual(ids, {"1101"})

    def test_or_max_score(self) -> None:
        sigs = iter_dual_branch_buy_signals(
            self.conn,
            mode="or",
            metric="lots",
            min_threshold=50_000.0,
            top_n=1,
            window_start="2025-01-01",
            window_end=self.day,
        )
        self.assertEqual(sigs[0].stock_id, "1101")

    def test_sum_and_cache(self) -> None:
        tape = DualBranchTapeCache.load(
            self.conn,
            window_start="2025-01-01",
            window_end=self.day,
            need_closes=True,
        )
        sigs = iter_dual_branch_buy_signals(
            self.conn,
            mode="sum",
            metric="lots",
            min_threshold=700_000.0,
            top_n=1,
            window_start="2025-01-01",
            window_end=self.day,
            tape=tape,
        )
        # 1101: 600k+600k=1.2M passes; 2317: 80k+200k=280k fails
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].stock_id, "1101")


class AmountRrgFunnelHelperTests(unittest.TestCase):
    def test_threshold_boundaries_and_missing_value(self) -> None:
        le = FunnelSpec("le", "f", "le", "x", "le", -2.0)
        ge = FunnelSpec("ge", "f", "ge", "x", "ge", 100.0)
        self.assertTrue(_passes(-2.0, le))
        self.assertFalse(_passes(-1.99, le))
        self.assertTrue(_passes(100.0, ge))
        self.assertFalse(_passes(None, ge))

    def test_phi_and_drawdown(self) -> None:
        self.assertEqual(
            phi_binary([True, True, False, False], [True, True, False, False]),
            1.0,
        )
        self.assertEqual(
            phi_binary([True, True, False, False], [False, False, True, True]),
            -1.0,
        )
        self.assertEqual(max_drawdown_ntd([10.0, -4.0, -8.0, 3.0]), -12.0)


class FubonXindianFilterTests(unittest.TestCase):
    @staticmethod
    def _signal(stock_id: str, net: float) -> CopytradeSignal:
        return CopytradeSignal(
            signal_date="2025-01-21",
            stock_id=stock_id,
            stock_name=stock_id,
            action="分點買超",
            share_delta=net,
            weight_delta=None,
        )

    def test_w20_gate_precedes_top_n(self) -> None:
        day = "2025-01-21"
        raw = {
            day: [
                self._signal("2330", 700_000.0),
                self._signal("2317", 650_000.0),
            ]
        }
        features = {
            (day, "2330"): {
                "signal_close": 100.0,
                "avg_volume20": 2_000_000.0,
                "rs_momentum": 99.0,
            },
            (day, "2317"): {
                "signal_close": 100.0,
                "avg_volume20": 2_000_000.0,
                "rs_momentum": 102.0,
            },
        }
        grouped, funnel = build_config_grouped(
            raw,
            features,
            FilterConfig(top_n=1, w20_momentum_min=101.0),
        )
        self.assertEqual([s.stock_id for s in grouped[day]], ["2317"])
        self.assertEqual(funnel["w20_pass_legs"], 1)

    def test_amount_metric_ranks_after_filters(self) -> None:
        day = "2025-01-21"
        raw = {
            day: [
                self._signal("2330", 100_000.0),
                self._signal("2317", 500_000.0),
            ]
        }
        features = {
            (day, "2330"): {
                "signal_close": 1_000.0,
                "avg_volume20": 2_000_000.0,
                "rs_momentum": 102.0,
            },
            (day, "2317"): {
                "signal_close": 100.0,
                "avg_volume20": 2_000_000.0,
                "rs_momentum": 102.0,
            },
        }
        grouped, _ = build_config_grouped(
            raw,
            features,
            FilterConfig(
                metric="amount",
                threshold=50_000_000.0,
                top_n=1,
            ),
        )
        self.assertEqual([s.stock_id for s in grouped[day]], ["2330"])

    def test_leading_quadrant_gate_precedes_top_n(self) -> None:
        day = "2025-01-21"
        raw = {
            day: [
                self._signal("2330", 500_000.0),
                self._signal("2317", 400_000.0),
            ]
        }
        features = {
            (day, "2330"): {
                "signal_close": 100.0,
                "avg_volume20": 2_000_000.0,
                "rs_momentum": 99.0,
                "quadrant": "weakening",
            },
            (day, "2317"): {
                "signal_close": 100.0,
                "avg_volume20": 2_000_000.0,
                "rs_momentum": 102.0,
                "quadrant": "leading",
            },
        }
        grouped, funnel = build_config_grouped(
            raw,
            features,
            FilterConfig(
                metric="amount",
                threshold=30_000_000.0,
                w20_momentum_min=None,
                rrg_quadrant="leading",
                top_n=1,
            ),
        )
        self.assertEqual([signal.stock_id for signal in grouped[day]], ["2317"])
        self.assertEqual(funnel["quadrant_pass_legs"], 1)


class FubonXindianReadinessGateTests(unittest.TestCase):
    @staticmethod
    def _arm() -> dict:
        metrics = {
            "full": {
                "n_cycles": 42,
                "median_return_pct": 5.0,
                "mean_excess_pct": 2.0,
                "max_drawdown_pct": 12.0,
            },
            "is": {
                "n_cycles": 25,
                "median_return_pct": 4.0,
                "mean_excess_pct": 1.5,
            },
            "oos": {
                "n_cycles": 17,
                "median_return_pct": 6.0,
                "mean_excess_pct": 2.5,
            },
        }
        return {"metrics": metrics}

    def test_untouched_forward_is_mandatory_for_adoption(self) -> None:
        arm = self._arm()
        folds = [
            {
                "n_cycles": 8,
                "median_return_pct": 2.0,
                "mean_excess_pct": 1.0,
            }
            for _ in range(4)
        ]
        blocked = evaluate_strategy_readiness_gates(
            cost_60=arm,
            neighborhood=[self._arm() for _ in range(9)],
            folds=folds,
            untouched_forward_available=False,
        )
        self.assertTrue(blocked["historical_passed"])
        self.assertFalse(blocked["adoption_passed"])
        self.assertEqual(blocked["decision"], "paper_forward_required")

        adopted = evaluate_strategy_readiness_gates(
            cost_60=arm,
            neighborhood=[self._arm() for _ in range(9)],
            folds=folds,
            untouched_forward_available=True,
        )
        self.assertTrue(adopted["adoption_passed"])
        self.assertEqual(adopted["decision"], "adopt_strategy")

    def test_forward_gate_fails_without_sample(self) -> None:
        empty = evaluate_forward_gate(trading_days=0, forward_metrics=None)
        self.assertFalse(empty["passed"])
        self.assertFalse(empty["checks"]["trading_days_ge_minimum"])
        self.assertFalse(empty["checks"]["completed_cycles_ge_minimum"])

    def test_forward_gate_fails_when_cycles_insufficient(self) -> None:
        blocked = evaluate_forward_gate(
            trading_days=100,
            forward_metrics={
                "n_cycles": 8,
                "median_return_pct": 3.0,
                "mean_excess_pct": 1.5,
                "win_rate_pct": 60.0,
                "max_drawdown_pct": 10.0,
            },
        )
        self.assertFalse(blocked["passed"])
        self.assertTrue(blocked["checks"]["trading_days_ge_minimum"])
        self.assertFalse(blocked["checks"]["completed_cycles_ge_minimum"])

    def test_forward_gate_passes_when_all_thresholds_met(self) -> None:
        ok = evaluate_forward_gate(
            trading_days=90,
            forward_metrics={
                "n_cycles": 12,
                "median_return_pct": 1.0,
                "mean_excess_pct": 0.5,
                "win_rate_pct": 50.0,
                "max_drawdown_pct": 20.0,
            },
        )
        self.assertTrue(ok["passed"])
        self.assertTrue(all(ok["checks"].values()))

    def test_default_readiness_amount_is_50m_not_40m(self) -> None:
        self.assertEqual(DEFAULT_READINESS_AMOUNT_NTD, 50_000_000.0)
        fifty = FilterConfig(
            metric="amount",
            threshold=DEFAULT_READINESS_AMOUNT_NTD,
            rrg_quadrant="leading",
            top_n=1,
            entry_lag=0,
            hold=8,
            n_slots=1,
        )
        forty = FilterConfig(
            metric="amount",
            threshold=40_000_000.0,
            rrg_quadrant="leading",
            top_n=1,
            entry_lag=0,
            hold=8,
            n_slots=1,
        )
        self.assertNotEqual(fifty.id, forty.id)
        self.assertIn("50M", fifty.id)
        self.assertIn("40M", forty.id)


class XdStockFuturesTimingHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        scripts = root / "scripts" / "research"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        import run_xd_50m_stock_futures_timing as mod  # noqa: WPS433

        cls.mod = mod

    def test_outright_rejects_spread_contract(self) -> None:
        self.assertTrue(self.mod._is_outright("202608"))
        self.assertFalse(self.mod._is_outright("202608/202609"))

    def test_first_tick_not_before_1725(self) -> None:
        Tick = self.mod.Tick
        from datetime import datetime

        ticks = [
            Tick(datetime(2026, 7, 17, 17, 20, 0), "202608", 100.0, 1),
            Tick(datetime(2026, 7, 17, 17, 25, 0), "202608", 101.0, 1),
            Tick(datetime(2026, 7, 17, 17, 26, 0), "202608", 102.0, 1),
        ]
        hit = self.mod.first_tick_at_or_after(
            ticks, day="2026-07-17", hhmmss="17:25:00", contract="202608"
        )
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.price, 101.0)
        self.mod.assert_entry_not_before_1725(hit.ts)

    def test_nth_trading_day_after(self) -> None:
        cal = ["2026-07-14", "2026-07-15", "2026-07-16", "2026-07-17"]
        self.assertEqual(
            self.mod.nth_trading_day_after(cal, "2026-07-14", 1), "2026-07-15"
        )
        self.assertEqual(
            self.mod.nth_trading_day_after(cal, "2026-07-14", 3), "2026-07-17"
        )

    def test_products_for_universe(self) -> None:
        night = self.mod.products_for_universe("night50")
        ids = {p["product"] for p in night}
        self.assertEqual(ids, {"CDF", "QFF", "CCF"})
        top1 = self.mod.products_for_universe("leading_top1")
        self.assertEqual({p["product"] for p in top1}, {"CDF", "QFF"})
        self.assertTrue(self.mod.NIGHT50_STOCK_IDS <= {"2330", "2303"})


if __name__ == "__main__":
    unittest.main()
