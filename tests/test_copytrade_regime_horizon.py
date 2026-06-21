"""copytrade_regime_horizon · PIT trend posture stratification."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from stock_db import connect


def _seed_ix(conn: sqlite3.Connection, dates_closes: list[tuple[str, float]]) -> None:
    for d, c in dates_closes:
        conn.execute(
            """
            INSERT INTO daily_bars (
                code, date, source, open, high, low, close, volume, synced_at
            )
            VALUES ('IX0001', ?, 'tej', ?, ?, ?, ?, 1000, '2025-01-01T00:00:00Z')
            """,
            (d, c, c * 1.01, c * 0.99, c),
        )
    conn.commit()


class TestCopytradeRegimeHorizon(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.conn = connect(Path(tmp.name))
        # 200+ 根 K 供 trend template
        rows = []
        for i in range(220):
            d = f"2025-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}"
            if int(d[5:7]) > 12:
                break
            c = 100.0 + i * 0.5
            rows.append((d, c))
        _seed_ix(self.conn, rows)
        self.as_of = rows[-1][0]

    def test_classify_regime_pit_returns_label(self) -> None:
        from research.backtest.copytrade_regime_horizon import classify_regime_pit

        lab = classify_regime_pit(self.conn, self.as_of)
        self.assertIsNotNone(lab)
        assert lab is not None
        self.assertIn(
            lab.trend_posture,
            ("broadening", "concentration", "transitional", "contraction"),
        )
        self.assertIn(lab.exposure_decision, ("allowed", "restrictive", "cash-priority"))

    def test_load_ix_bars_as_of_truncates_future(self) -> None:
        from research.backtest.copytrade_regime_horizon import load_ix_bars_as_of

        bars = load_ix_bars_as_of(self.conn, "2025-03-01")
        self.assertTrue(all(str(r["trade_date"]) <= "2025-03-01" for r in bars))


if __name__ == "__main__":
    unittest.main()
