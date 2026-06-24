"""Tests for RRG mono hold7 intraday exit helpers."""

from __future__ import annotations

import unittest

from research.backtest.rrg_mono_intraday_exit import (
    ExitVariantConfig,
    _daily_condition,
    _hold_dates,
    _resolve_signal_day,
)


class TestRrgMonoIntradayExit(unittest.TestCase):
    def test_ll_streak_condition(self) -> None:
        feat = {"trend": "down_left", "end_q": "weakening", "mono_up": False, "seg_last": 1.0}
        cfg = ExitVariantConfig(signal_mode="ll_streak")
        self.assertTrue(_daily_condition(feat, config=cfg, entry_seg_last=2.0))

    def test_mono_break(self) -> None:
        feat = {"trend": "up_right", "mono_up": False, "seg_last": 2.0}
        cfg = ExitVariantConfig(signal_mode="mono_break")
        self.assertTrue(_daily_condition(feat, config=cfg, entry_seg_last=2.0))

    def test_accel_d4_fires_when_not_accelerating(self) -> None:
        full_dates = [
            "2026-01-01",
            "2026-01-02",
            "2026-01-03",
            "2026-01-06",
            "2026-01-07",
            "2026-01-08",
        ]
        rs_ratio = None
        rs_mom = None

        def fake_feat(rs_r, rs_m, dates, trade_date, sid):
            if trade_date == "2026-01-07":
                return {"trend": "down_left", "mono_up": False, "seg_last": 1.0, "end_q": "weakening"}
            return {"trend": "up_right", "mono_up": True, "seg_last": 2.0, "end_q": "leading"}

        import research.backtest.rrg_mono_intraday_exit as mod

        orig = mod._daily_feat
        mod._daily_feat = lambda *a, **k: fake_feat(*a, **k)
        try:
            cfg = ExitVariantConfig(
                signal_mode="accel_d4",
                accel_hold_day=4,
                min_hold_days=4,
                max_hold_days=7,
            )
            exit_d, reason = _resolve_signal_day(
                rs_ratio=rs_ratio,
                rs_mom=rs_mom,
                full_dates=full_dates,
                entry_date="2026-01-01",
                stock_id="2330",
                entry_seg_last=2.0,
                config=cfg,
            )
            self.assertEqual(exit_d, "2026-01-07")
            self.assertEqual(reason, "accel_d4")
        finally:
            mod._daily_feat = orig

    def test_hold_dates_offset(self) -> None:
        dates = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-06"]
        held = _hold_dates(dates, "2026-01-01", 3)
        self.assertEqual(held, ["2026-01-02", "2026-01-03", "2026-01-06"])


if __name__ == "__main__":
    unittest.main()
