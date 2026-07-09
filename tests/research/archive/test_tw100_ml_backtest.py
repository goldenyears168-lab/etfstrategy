"""TW100 ML backtest skeleton tests."""

from __future__ import annotations

import sqlite3
import unittest

from research.backtest.archive.tw100_ml_backtest import (
    Tw100MlBacktestConfig,
    daily_rank_ic_series,
    forward_label,
    momentum_signal,
    run_tw100_baseline_backtest,
    split_ic_series,
)
from research_config import load_research_config
import pandas as pd


class Tw100MlBacktestTests(unittest.TestCase):
    def test_research_topic_registered(self) -> None:
        cfg = load_research_config()
        topic = cfg.get("tw100-alpha158-wfa")
        assert topic is not None
        self.assertEqual(topic.phase, "hypothesis")
        self.assertEqual(topic.split_spec, "tw100_wfa_504_100")
        self.assertIn("scripts/run_tw100_ml_backtest.py", topic.run_scripts)
        self.assertIn("scripts/run_tw100_monthly_wfa.py", topic.run_scripts)
        hids = {h["id"] for h in topic.hypotheses}
        self.assertIn("H-TW100-3", hids)

    def test_forward_label_horizon2(self) -> None:
        idx = pd.date_range("2024-01-01", periods=6, freq="B")
        panel = pd.DataFrame({"A": [100.0, 101, 102, 103, 104, 105]}, index=idx.astype(str))
        lab = forward_label(panel, horizon=2)
        # at idx[0]: entry idx[1]=101 exit idx[3]=103 -> 103/101-1
        self.assertAlmostEqual(float(lab.iloc[0, 0]), 103 / 101 - 1.0, places=6)

    def test_daily_rank_ic_min_cross_section(self) -> None:
        sig = pd.DataFrame({"a": [1.0, 2.0], "b": [2.0, 1.0]}, index=["2024-01-01", "2024-01-02"])
        lab = pd.DataFrame({"a": [0.1, -0.1], "b": [-0.1, 0.1]}, index=["2024-01-01", "2024-01-02"])
        series = daily_rank_ic_series(sig, lab, min_cross_section=2)
        self.assertEqual(len(series), 2)

    def test_split_ic_series(self) -> None:
        ic = [("2024-01-01", 0.1), ("2024-01-02", 0.2), ("2024-01-03", 0.3)]
        is_ic, oos_ic, is_rng, oos_rng = split_ic_series(ic, is_ratio=2 / 3)
        self.assertEqual(len(is_ic), 2)
        self.assertEqual(len(oos_ic), 1)
        self.assertIn("2024-01-01", is_rng)

    def test_run_baseline_on_fixture_db(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE universe_snapshot_meta (
                universe_id TEXT, snapshot_date TEXT, n_constituents INTEGER,
                source TEXT, synced_at TEXT, PRIMARY KEY (universe_id, snapshot_date)
            );
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
        conn.execute(
            "INSERT INTO universe_snapshot_meta VALUES (?,?,?,?,?)",
            ("tw100", snap, 5, "test", "now"),
        )
        idx = pd.bdate_range("2024-01-01", periods=80)
        for i, sid in enumerate(["2330", "2317", "2454", "2303", "2881"]):
            conn.execute(
                "INSERT INTO universe_constituents VALUES (?,?,?,?,?,?)",
                ("tw100", snap, sid, sid, i + 1, 1.0),
            )
            for j, td in enumerate(idx):
                px = 100.0 + j + i * 0.1
                conn.execute(
                    "INSERT INTO stock_daily_bars VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL)",
                    (sid, td.strftime("%Y-%m-%d"), px, px, px, px, 1000, "finmind", "now"),
                )
        cfg = Tw100MlBacktestConfig(
            snapshot_date=snap,
            start_date="2024-01-01",
            end_date="2024-04-30",
            min_cross_section=5,
        )
        payload = run_tw100_baseline_backtest(conn, cfg)
        self.assertEqual(payload["n_constituents"], 5)
        self.assertGreater(payload["ic"]["n_days"], 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
