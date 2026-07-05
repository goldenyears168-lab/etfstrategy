"""TW100 walk-forward skeleton tests."""

from __future__ import annotations

import sqlite3
import unittest

import pandas as pd

from research.backtest.tw100_walk_forward import (
    Tw100WalkForwardConfig,
    build_walk_forward_folds,
    run_tw100_walk_forward_backtest,
)
from research_config import load_research_splits


class Tw100WalkForwardTests(unittest.TestCase):
    def test_split_spec_parses_train_days(self) -> None:
        spec = load_research_splits().get("tw100_wfa_504_100")
        assert spec is not None
        self.assertEqual(spec.train_days, 504)
        self.assertEqual(spec.test_days, 100)
        self.assertEqual(spec.embargo_days, 7)

    def test_build_folds_count(self) -> None:
        dates = [f"2020-01-{d:02d}" for d in range(1, 29)]  # dummy 28 days
        # extend to 700 sequential trading days
        idx = pd.bdate_range("2018-01-01", periods=700)
        dates = [d.strftime("%Y-%m-%d") for d in idx]
        folds = build_walk_forward_folds(
            dates, train_days=504, test_days=100, embargo_days=7, roll_days=100
        )
        self.assertGreaterEqual(len(folds), 1)
        self.assertEqual(folds[0].fold_id, 0)
        self.assertLess(folds[0].train_end, folds[0].test_start)

    def test_run_wfa_fixture_db(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE universe_constituents (
                universe_id TEXT, snapshot_date TEXT, stock_id TEXT,
                stock_name TEXT, rank_no INTEGER, market_value REAL,
                PRIMARY KEY (universe_id, snapshot_date, stock_id)
            );
            CREATE TABLE stock_daily_bars (
                stock_id TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
                close REAL NOT NULL, volume INTEGER, source TEXT NOT NULL,
                synced_at TEXT NOT NULL, adj_close REAL, amount REAL,
                PRIMARY KEY (stock_id, trade_date, source)
            );
            """
        )
        snap = "2026-06-26"
        sids = ["2330", "2317", "2454", "2303", "2881"]
        for i, sid in enumerate(sids):
            conn.execute(
                "INSERT INTO universe_constituents VALUES (?,?,?,?,?,?)",
                ("tw100", snap, sid, sid, i + 1, 1.0),
            )
        idx = pd.bdate_range("2020-01-01", periods=700)
        for i, sid in enumerate(sids):
            for j, td in enumerate(idx):
                px = 100.0 + j + i * 0.05
                conn.execute(
                    "INSERT INTO stock_daily_bars VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL)",
                    (sid, td.strftime("%Y-%m-%d"), px, px, px, px, 1000, "finmind", "now"),
                )
        cfg = Tw100WalkForwardConfig(
            snapshot_date=snap,
            start_date="2020-01-01",
            end_date=idx[-1].strftime("%Y-%m-%d"),
            max_folds=2,
        )
        payload = run_tw100_walk_forward_backtest(conn, cfg)
        self.assertGreaterEqual(payload["n_folds"], 1)
        self.assertIn("aggregate_test_ic", payload)
        conn.close()


if __name__ == "__main__":
    unittest.main()
