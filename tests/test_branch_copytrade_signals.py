"""元大-松江 branch copytrade · signals + swap boundaries."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from copytrade.branch_signals import (
    BRANCH_BUY_ACTION,
    iter_branch_amount_buy_signals,
    iter_branch_buy_signals,
    mean_net,
)
from copytrade.signals import CopytradeSignal
from research.backtest.branch_copytrade_swap import simulate_fixed_slots_with_swap
from research.backtest.copytrade_backtest import (
    select_executed_signal_days,
    simulate_fixed_slots,
)
from stock_db import connect, upsert_stock_broker_branch_daily, upsert_stock_daily_bars

TRADER = "9801"
SOURCE = "finmind"


def _seed_bars(
    conn: sqlite3.Connection,
    stock_id: str,
    dates: list[str],
    *,
    start_px: float = 100.0,
    volume: float = 500_000.0,
) -> None:
    rows = []
    px = start_px
    for i, d in enumerate(dates):
        op = px
        cl = px * (1.01 if i % 2 == 0 else 0.99)
        rows.append(
            {
                "stock_id": stock_id,
                "trade_date": d,
                "open": op,
                "high": max(op, cl) * 1.01,
                "low": min(op, cl) * 0.99,
                "close": cl,
                "volume": volume,
                "source": SOURCE,
            }
        )
        px = cl
    upsert_stock_daily_bars(conn, rows)


def _seed_tape(
    conn: sqlite3.Connection,
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
                "securities_trader_id": TRADER,
                "securities_trader": "元大-松江",
                "stock_id": sid,
                "buy": buy,
                "sell": sell,
                "net": net,
                "source": SOURCE,
            }
        )
    upsert_stock_broker_branch_daily(conn, rows)


class BranchSignalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.conn = connect(self.db)
        self.dates = [
            "2025-01-02",
            "2025-01-03",
            "2025-01-06",
            "2025-01-07",
            "2025-01-08",
            "2025-01-09",
            "2025-01-10",
            "2025-01-13",
            "2025-01-14",
            "2025-01-15",
            "2025-01-16",
            "2025-01-17",
            "2025-01-20",
            "2025-01-21",
            "2025-01-22",
            "2025-01-23",
            "2025-01-24",
            "2025-01-27",
            "2025-01-28",
            "2025-01-29",
            "2025-01-30",
        ]
        for sid in ("2330", "2317", "2454", "2881"):
            _seed_bars(self.conn, sid, self.dates, start_px=100.0 + int(sid) % 7)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_threshold_and_top_n(self) -> None:
        # 2330/2317 above 100k; 2454 below; 2881 above but 4th by rank
        _seed_tape(
            self.conn,
            "2025-01-15",
            [
                ("2330", 500_000.0),
                ("2317", 200_000.0),
                ("2454", 50_000.0),
                ("2881", 150_000.0),
            ],
        )
        sigs = iter_branch_buy_signals(
            self.conn,
            TRADER,
            min_net_shares=100_000.0,
            top_n=2,
            window_start="2025-01-15",
            window_end="2025-01-15",
            min_avg_volume_shares=None,
        )
        self.assertEqual(len(sigs), 2)
        self.assertEqual(sigs[0].stock_id, "2330")
        self.assertEqual(sigs[1].stock_id, "2317")
        self.assertEqual(sigs[0].action, BRANCH_BUY_ACTION)
        self.assertGreaterEqual(sigs[0].share_delta, 100_000.0)

    def test_mean_net(self) -> None:
        legs = [
            CopytradeSignal("2025-01-15", "2330", "2330", BRANCH_BUY_ACTION, 100.0, None),
            CopytradeSignal("2025-01-15", "2317", "2317", BRANCH_BUY_ACTION, 300.0, None),
        ]
        self.assertEqual(mean_net(legs), 200.0)

    def test_amount_threshold_ranks_by_net_times_signal_close(self) -> None:
        _seed_tape(
            self.conn,
            "2025-01-15",
            [
                ("2330", 100_000.0),
                ("2317", 120_000.0),
                ("2454", 50_000.0),
            ],
        )
        sigs = iter_branch_amount_buy_signals(
            self.conn,
            TRADER,
            min_net_amount_ntd=5_000_000.0,
            top_n=2,
            window_start="2025-01-15",
            window_end="2025-01-15",
            min_avg_volume_shares=None,
        )
        self.assertEqual(len(sigs), 2)
        # Seeded closes differ, so ranking must follow amount rather than shares.
        close_by_stock = {
            row["stock_id"]: float(row["close"])
            for row in self.conn.execute(
                """
                SELECT stock_id, close
                FROM stock_daily_bars
                WHERE trade_date = '2025-01-15'
                """
            )
        }
        amounts = [
            float(sig.share_delta) * close_by_stock[sig.stock_id] for sig in sigs
        ]
        self.assertGreaterEqual(amounts[0], amounts[1])
        self.assertTrue(all(amount >= 5_000_000.0 for amount in amounts))


class BranchSwapBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.conn = connect(self.db)
        # Enough calendar for L1 + H5
        self.dates = [f"2025-02-{d:02d}" for d in range(3, 29)]
        for sid in ("2330", "2317", "2454"):
            _seed_bars(self.conn, sid, self.dates, start_px=50.0)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _day(
        self,
        signal_date: str,
        stock_id: str,
        net: float,
        *,
        pnl: float,
        alpha: float,
        entry: str,
        exit_d: str,
    ) -> dict:
        return {
            "signal_date": signal_date,
            "entry_date": entry,
            "exit_date": exit_d,
            "n_legs": 1,
            "deployed_ntd": 10_000.0,
            "pnl_ntd": pnl,
            "return_pct": pnl / 100.0,
            "bench_return_pct": 0.0,
            "alpha_ntd": alpha,
            "status": "complete",
            "_net": net,
            "_stock": stock_id,
        }

    def test_skip_when_full_vs_swap_enters_more(self) -> None:
        # Three overlapping signal days, 1 slot → skip keeps 1; swap can replace.
        grouped: dict[str, list[CopytradeSignal]] = {
            "2025-02-03": [
                CopytradeSignal(
                    "2025-02-03", "2330", "2330", BRANCH_BUY_ACTION, 100_000.0, None
                )
            ],
            "2025-02-05": [
                CopytradeSignal(
                    "2025-02-05", "2317", "2317", BRANCH_BUY_ACTION, 50_000.0, None
                )
            ],
            "2025-02-07": [
                CopytradeSignal(
                    "2025-02-07", "2454", "2454", BRANCH_BUY_ACTION, 300_000.0, None
                )
            ],
        }
        # Precompute skip path via synthetic day dicts with overlapping holds.
        days = [
            self._day(
                "2025-02-03",
                "2330",
                100_000,
                pnl=100,
                alpha=50,
                entry="2025-02-04",
                exit_d="2025-02-11",
            ),
            self._day(
                "2025-02-05",
                "2317",
                50_000,
                pnl=10,
                alpha=5,
                entry="2025-02-06",
                exit_d="2025-02-13",
            ),
            self._day(
                "2025-02-07",
                "2454",
                300_000,
                pnl=200,
                alpha=80,
                entry="2025-02-10",
                exit_d="2025-02-17",
            ),
        ]
        executed, meta = select_executed_signal_days(days, n_slots=1)
        self.assertEqual(len(executed), 1)
        self.assertEqual(meta["n_skipped"], 2)

        swap = simulate_fixed_slots_with_swap(
            self.conn,
            grouped,
            n_slots=1,
            entry_lag_days=0,
            hold_trading_days=5,
            capital_ntd=10_000.0,
        )
        # First entry + at least one swap on stronger later basket (300k > 100k)
        self.assertGreaterEqual(swap.n_entries, 2)
        self.assertGreaterEqual(swap.n_swaps, 1)
        skip_sim = simulate_fixed_slots(self.conn, days, n_slots=1)
        self.assertEqual(int(skip_sim["recycled_n_cycles"] or 0), 1)


if __name__ == "__main__":
    unittest.main()
