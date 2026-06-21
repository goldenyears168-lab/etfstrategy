"""vcp_nse_port：移植自 nse-vcp-screener 的 scoring 单元测试。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from stock_db import connect, upsert_daily_bars, upsert_stock_daily_bars
from vcp_nse_port.evaluate import evaluate_vcp_nse
from vcp_nse_port.pivot_proximity import calculate_pivot_proximity
from vcp_nse_port.scorer import calculate_composite_score
from vcp_nse_port.volume_pattern import calculate_volume_pattern
from research.archive.vcp_retired.vcp_screen import MIN_BARS, evaluate_vcp


def _uptrend_df(n: int = 260, *, tighten_tail: bool = True) -> pd.DataFrame:
    start = date(2024, 1, 2)
    rows: list[dict] = []
    price = 50.0
    for i in range(n):
        d = start + timedelta(days=i)
        if i >= n - 120 and tighten_tail:
            seg = (i - (n - 120)) // 30
            width = max(1.0, 8.0 - seg * 2.5)
        else:
            width = 6.0
        vol = 2_000_000 if i < n - 20 else 800_000
        price += 0.25
        rows.append(
            {
                "date": d.isoformat(),
                "Open": price,
                "High": price + width,
                "Low": price - width * 0.35,
                "Close": price,
                "Volume": vol,
            }
        )
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df


class VcpNsePortTests(unittest.TestCase):
    def test_composite_score_weights(self) -> None:
        out = calculate_composite_score(100, 80, 70, 90, 65)
        self.assertAlmostEqual(out["composite_score"], 82.2, places=1)
        self.assertEqual(out["quality"], "Excellent")

    def test_pivot_near_scores_high(self) -> None:
        prox = calculate_pivot_proximity(100.0, 102.0)
        self.assertEqual(prox["position"], "near_pivot")
        self.assertEqual(prox["score"], 90.0)

    def test_volume_dry_up(self) -> None:
        df = _uptrend_df(80)
        vol = calculate_volume_pattern(df)
        self.assertLess(vol["dry_up_ratio"], 1.0)
        self.assertGreater(vol["score"], 0)

    def test_evaluate_vcp_nse_on_uptrend(self) -> None:
        stock = _uptrend_df(260)
        bench = _uptrend_df(260)
        bench["Close"] = bench["Close"] * 0.8
        result = evaluate_vcp_nse(stock, bench)
        self.assertIn("composite_score", result)
        self.assertIn("passed", result)


class VcpScreenIntegrationTests(unittest.TestCase):
    def _seed_bars(self, stock_id: str, n: int) -> list[dict]:
        start = date(2024, 1, 2)
        rows: list[dict] = []
        price = 100.0
        for i in range(n):
            trade_date = (start + timedelta(days=i)).isoformat()
            width = 3.0 if i >= n - 30 else 5.0
            vol = 1_000_000 if i < n - 15 else 500_000
            price += 0.4
            rows.append(
                {
                    "stock_id": stock_id,
                    "trade_date": trade_date,
                    "open": price,
                    "high": price + width,
                    "low": price - width * 0.3,
                    "close": price,
                    "volume": vol,
                    "source": "finmind",
                }
            )
        return rows

    def test_evaluate_vcp_requires_min_bars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            try:
                upsert_stock_daily_bars(conn, self._seed_bars("2330", 30))
                self.assertIsNone(evaluate_vcp(conn, "2330"))
            finally:
                conn.close()

    def test_evaluate_vcp_returns_eval_with_enough_bars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            try:
                upsert_stock_daily_bars(conn, self._seed_bars("2330", MIN_BARS + 20))
                # benchmark IX0001 in daily_bars
                bench_rows = []
                for r in self._seed_bars("IX0001", MIN_BARS + 20):
                    bench_rows.append(
                        {
                            "code": "IX0001",
                            "date": r["trade_date"],
                            "open": r["open"] * 0.9,
                            "high": r["high"] * 0.9,
                            "low": r["low"] * 0.9,
                            "close": r["close"] * 0.9,
                            "volume": r["volume"],
                            "spread": 0.0,
                            "source": "tej",
                        }
                    )
                upsert_daily_bars(conn, bench_rows)
                ev = evaluate_vcp(conn, "2330", stock_name="台積電")
                self.assertIsNotNone(ev)
                assert ev is not None
                self.assertGreaterEqual(ev.vcp_score, 0.0)
                self.assertIn(
                    ev.execution_state,
                    ("Invalid", "Pre-breakout", "Breakout", "Overextended", "Extended"),
                )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
