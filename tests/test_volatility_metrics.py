"""stock_context 波動指標：ATR14、日振幅、realized vol。"""

from __future__ import annotations

import sqlite3
import unittest

from stock_context import compute_price_volatility_metrics


def _row(
    trade_date: str,
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE t (trade_date TEXT, open REAL, high REAL, low REAL, close REAL, volume INT)"
    )
    conn.execute(
        "INSERT INTO t VALUES (?,?,?,?,?,1000)",
        (trade_date, open_, high, low, close),
    )
    row = conn.execute("SELECT * FROM t").fetchone()
    conn.close()
    return row


def _flat_series(n: int, close: float = 100.0, swing: float = 1.0) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    for i in range(n):
        c = close + (i % 2) * swing
        rows.append(
            _row(
                f"2026-01-{i+1:02d}",
                open_=c,
                high=c + swing,
                low=c - swing,
                close=c,
            )
        )
    return rows


class TestVolatilityMetrics(unittest.TestCase):
    def test_insufficient_bars(self) -> None:
        atr, avg_r, rv = compute_price_volatility_metrics(
            _flat_series(5),
            close=100.0,
        )
        self.assertIsNone(atr)
        self.assertIsNone(avg_r)
        self.assertIsNone(rv)

    def test_computes_all_three(self) -> None:
        series = _flat_series(20, close=100.0, swing=2.0)
        atr, avg_r, rv = compute_price_volatility_metrics(series, close=102.0)
        self.assertIsNotNone(atr)
        self.assertIsNotNone(avg_r)
        self.assertIsNotNone(rv)
        self.assertGreater(atr, 0.0)
        self.assertGreater(avg_r, 0.0)
        self.assertGreater(rv, 0.0)

    def test_higher_swing_higher_atr(self) -> None:
        low = compute_price_volatility_metrics(_flat_series(20, swing=1.0), close=100.0)[0]
        high = compute_price_volatility_metrics(_flat_series(20, swing=3.0), close=100.0)[0]
        self.assertIsNotNone(low)
        self.assertIsNotNone(high)
        self.assertGreater(high, low)


if __name__ == "__main__":
    unittest.main()
