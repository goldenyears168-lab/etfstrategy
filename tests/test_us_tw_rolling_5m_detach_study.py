"""Minimal tests for rolling 5m detach simulation helpers."""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from research.backtest.us_tw_rolling_5m_detach_study import (
    StrategySpec,
    _LOCAL_5M_MIN_DAYS,
    _local_5m_panel_fresh_enough,
    _panel_session_day_bar_count,
    fetch_yahoo_5m_panel,
    score_strategy,
    simulate_day,
)

_TZ_TW = ZoneInfo("Asia/Taipei")
_TZ_ET = ZoneInfo("America/New_York")


class TestDetachSim(unittest.TestCase):
    def _poll(self) -> pd.DataFrame:
        n = 8
        df = pd.DataFrame(
            {
                "poll": [f"09:{10+i*5:02d}" for i in range(n)],
                "tw_from_open": [0.0, -0.3, -0.7, -1.2, -1.5, -1.1, -0.8, -0.5],
                "nq_from_open": [0.0, -0.1, -0.1, -0.2, -0.2, -0.1, 0.0, 0.1],
                "cum_spread": [0.0, -0.2, -0.6, -1.0, -1.3, -1.0, -0.8, -0.6],
                "tw_win": [-0.1, -0.3, -0.4, -0.5, -0.3, 0.2, 0.3, 0.2],
                "spread_win": [-0.1, -0.3, -0.5, -0.6, -0.4, 0.3, 0.35, 0.1],
                "corr": [0.8, 0.5, 0.1, -0.2, 0.2, 0.4, 0.5, 0.6],
            }
        )
        df.attrs.update(
            {
                "date": "2026-07-14",
                "close_from_open": -2.0,
                "trough_from_open": -2.5,
                "peak_to_trough": 3.5,
            }
        )
        return df

    def test_detach_and_save(self) -> None:
        poll = self._poll()
        spec = StrategySpec("cum-1.0_w5", "test", cum_thr=-1.0, window_min=5)
        out = simulate_day(poll, spec)
        self.assertTrue(out["triggered"])
        self.assertEqual(out["detach_poll"], "09:25")
        # exit at -1.2 vs close -2.0 → saved loss
        self.assertGreater(out["saved_vs_close_ntd"], 0)

    def test_score(self) -> None:
        rows = [
            {
                "triggered": True,
                "peak_to_trough_pct": 3.5,
                "saved_vs_close_ntd": 5000,
                "saved_vs_trough_ntd": 8000,
                "false_alarm_opp_cost_ntd": 1000,
                "false_alarm_opp_cost_pct": 0.1,
                "saved_vs_close_pct": 0.5,
                "detach_poll": "09:15",
            },
            {
                "triggered": False,
                "peak_to_trough_pct": 1.0,
                "saved_vs_close_ntd": 0,
                "saved_vs_trough_ntd": 0,
                "false_alarm_opp_cost_ntd": 0,
                "false_alarm_opp_cost_pct": 0,
                "saved_vs_close_pct": 0,
                "detach_poll": None,
            },
        ]
        sm = score_strategy(rows)
        self.assertEqual(sm["net_edge_ntd"], 4000.0)


class TestLocal5mFreshness(unittest.TestCase):
    def _tw_panel(self, days: list[str], bars_per_day: int = 3) -> pd.DataFrame:
        idx: list[pd.Timestamp] = []
        for d in days:
            for i in range(bars_per_day):
                minute = 9 * 60 + i * 5
                idx.append(
                    pd.Timestamp(
                        f"{d} {minute // 60:02d}:{minute % 60:02d}:00",
                        tz=_TZ_TW,
                    )
                )
        return pd.DataFrame(
            {
                "open": [1.0] * len(idx),
                "high": [1.0] * len(idx),
                "low": [1.0] * len(idx),
                "close": [1.0] * len(idx),
                "volume": [0.0] * len(idx),
            },
            index=pd.DatetimeIndex(idx),
        )

    def _calendar_days(self, start: str, n: int) -> list[str]:
        start_d = date.fromisoformat(start)
        return [(start_d + pd.Timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]

    def test_session_day_count_uses_tw_wall_for_et_nq(self) -> None:
        # TW 2026-07-16 09:00 == ET 2026-07-15 21:00
        idx = pd.DatetimeIndex(
            [
                pd.Timestamp("2026-07-15 21:00:00", tz=_TZ_ET),
                pd.Timestamp("2026-07-15 21:05:00", tz=_TZ_ET),
                pd.Timestamp("2026-07-15 21:10:00", tz=_TZ_ET),
            ]
        )
        df = pd.DataFrame({"close": [1.0, 1.1, 1.2]}, index=idx)
        self.assertEqual(_panel_session_day_bar_count(df, date(2026, 7, 16)), 3)
        self.assertEqual(_panel_session_day_bar_count(df, date(2026, 7, 15)), 0)

    def test_stale_long_history_not_fresh_enough(self) -> None:
        # 40+ days ending yesterday — must NOT prefer local for today
        local = self._tw_panel(self._calendar_days("2026-06-01", _LOCAL_5M_MIN_DAYS + 5))
        self.assertFalse(_local_5m_panel_fresh_enough(local, date(2026, 7, 16)))

    def test_session_covered_is_fresh_enough(self) -> None:
        days = self._calendar_days("2026-06-01", _LOCAL_5M_MIN_DAYS)
        days.append("2026-07-16")
        local = self._tw_panel(days)
        self.assertTrue(_local_5m_panel_fresh_enough(local, date(2026, 7, 16)))

    def test_fetch_falls_back_to_yahoo_when_local_stale(self) -> None:
        stale = self._tw_panel(self._calendar_days("2026-06-01", _LOCAL_5M_MIN_DAYS + 2))
        live = self._tw_panel(["2026-07-16"], bars_per_day=2)

        with (
            patch(
                "research.backtest.us_tw_rolling_5m_detach_study.load_local_5m_panel",
                return_value=stale,
            ),
            patch("yfinance.download", return_value=live),
            patch(
                "research.backtest.us_tw_divergence_sell_gate_study._flatten_yf",
                return_value=live,
            ),
        ):
            out = fetch_yahoo_5m_panel("^TWII", date(2026, 7, 16))
        self.assertEqual(out.attrs.get("panel_source"), "yahoo_live:^TWII")
        self.assertGreater(len(out), 0)

    def test_fetch_prefers_local_when_session_covered(self) -> None:
        days = self._calendar_days("2026-06-01", _LOCAL_5M_MIN_DAYS)
        days.append("2026-07-16")
        local = self._tw_panel(days)
        local.attrs["panel_source"] = "local:IX0001"

        with (
            patch(
                "research.backtest.us_tw_rolling_5m_detach_study.load_local_5m_panel",
                return_value=local,
            ),
            patch("yfinance.download") as dl,
        ):
            out = fetch_yahoo_5m_panel("^TWII", date(2026, 7, 16))
        dl.assert_not_called()
        self.assertEqual(out.attrs.get("panel_source"), "local:IX0001")


if __name__ == "__main__":
    unittest.main()
