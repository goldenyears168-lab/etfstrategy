"""Unit tests · Carlo Zarattini Track 2 ORB helpers."""

from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.archive.cz_intraday_common import (
    aggregate_5m,
    simulate_orb_qqq_session,
)


def _synthetic_day(
    *,
    or_bullish: bool = True,
    n_minutes: int = 30,
) -> pd.DataFrame:
    rows = []
    base = 100.0
    for i in range(n_minutes):
        h, m = divmod(i, 60)
        minute = f"{9 + h:02d}:{m:02d}:00"
        if i < 5:
            o, c = base, base + (0.5 if or_bullish else -0.5)
            hi, lo = max(o, c) + 0.2, min(o, c) - 0.2
        elif i == 5:
            o, c, hi, lo = base + 0.5, base + 0.6, base + 0.8, base + 0.4
        else:
            o = base + 0.5 + (i - 5) * 0.05
            c = o + 0.03
            hi, lo = c + 0.05, o - 0.02
        rows.append({"minute": minute, "open": o, "high": hi, "low": lo, "close": c, "volume": 1000})
    return pd.DataFrame(rows)


class CzTrack2OrbTests(unittest.TestCase):
    def test_aggregate_5m_first_bar_covers_five_minutes(self) -> None:
        day = _synthetic_day()
        bars = aggregate_5m(day)
        self.assertGreaterEqual(len(bars), 2)
        self.assertEqual(str(bars.iloc[0]["minute"]), "09:00:00")
        self.assertEqual(str(bars.iloc[1]["minute"]), "09:05:00")

    def test_long_orb_range_stop_eod(self) -> None:
        day = _synthetic_day(or_bullish=True)
        rec = simulate_orb_qqq_session(
            day,
            trade_date="2024-01-02",
            atr14_val=None,
            stop_mode="range",
            exit_mode="eod",
            cost_rt=0.0,
        )
        self.assertIsNotNone(rec)
        assert rec is not None
        self.assertEqual(rec.direction, "long")
        self.assertGreater(rec.gross_pct, 0)

    def test_doji_skips(self) -> None:
        day = _synthetic_day(or_bullish=True)
        day.loc[day.index[:5], "close"] = day.loc[day.index[:5], "open"]
        rec = simulate_orb_qqq_session(
            day,
            trade_date="2024-01-02",
            atr14_val=None,
            stop_mode="range",
            exit_mode="eod",
        )
        self.assertIsNone(rec)


if __name__ == "__main__":
    unittest.main()
