"""Tests for RRG mono score swap mode C."""

from __future__ import annotations

import unittest

from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    _last_step_trend,
    _pick_swap_pair,
)
from rrg_mono_daily_brief import ScanRow


def _row(sid: str, seg: float) -> ScanRow:
    return ScanRow(
        stock_id=sid,
        stock_name=sid,
        fresh=True,
        mono=True,
        seg_last=seg,
        disp=1.2,
        segs=[],
        quadrants=[],
        rs_ratio=100.0,
        rs_momentum=100.0,
        daily_pct=None,
    )


class TestScoreSwapC(unittest.TestCase):
    def test_pick_worst_held_and_challenger(self) -> None:
        slots = [
            {"stock_id": "2330", "seg_last": 3.0, "entry_date": "2026-01-01"},
            {"stock_id": "2454", "seg_last": 1.5, "entry_date": "2026-01-01"},
        ]
        pool = [_row("3008", 2.5), _row("2317", 1.0)]
        cfg = ScoreSwapCConfig()
        sell, buy = _pick_swap_pair(slots, pool, held_ids={"2330", "2454"}, config=cfg)
        self.assertIsNotNone(sell)
        self.assertIsNotNone(buy)
        assert sell is not None and buy is not None
        self.assertEqual(sell["stock_id"], "2454")
        self.assertEqual(buy.stock_id, "3008")

    def test_no_swap_when_challenger_too_weak(self) -> None:
        slots = [{"stock_id": "2330", "seg_last": 3.0, "entry_date": "2026-01-01"}]
        pool = [_row("3008", 2.0)]
        sell, buy = _pick_swap_pair(slots, pool, held_ids={"2330"}, config=ScoreSwapCConfig())
        self.assertIsNone(sell)
        self.assertIsNone(buy)

    def test_mom1_sort_key(self) -> None:
        slots = [
            {"stock_id": "2330", "rs_momentum": 102.0, "seg_last": 3.0, "entry_date": "2026-01-01"},
            {"stock_id": "2454", "rs_momentum": 98.0, "seg_last": 2.0, "entry_date": "2026-01-01"},
        ]
        pool = [
            ScanRow(
                stock_id="3008",
                stock_name="3008",
                fresh=True,
                mono=True,
                seg_last=1.0,
                disp=1.0,
                segs=[],
                quadrants=[],
                rs_ratio=100.0,
                rs_momentum=100.5,
                daily_pct=None,
            )
        ]
        cfg = ScoreSwapCConfig(sort_key="rs_momentum", score_margin=0.5)
        sell, buy = _pick_swap_pair(slots, pool, held_ids={"2330", "2454"}, config=cfg)
        self.assertIsNotNone(sell)
        assert sell is not None
        self.assertEqual(sell["stock_id"], "2454")

    def test_mom2_seg_step_delta_decel_gate(self) -> None:
        slots = [
            {"stock_id": "2330", "seg_last": 3.0, "entry_date": "2026-01-01"},
            {"stock_id": "2454", "seg_last": 2.0, "entry_date": "2026-01-01"},
        ]
        pool = [
            ScanRow(
                stock_id="3008",
                stock_name="3008",
                fresh=True,
                mono=True,
                seg_last=2.0,
                disp=1.0,
                segs=[1.0, 1.0, 1.5],
                quadrants=[],
                rs_ratio=100.0,
                rs_momentum=100.0,
                daily_pct=None,
            )
        ]
        cfg = ScoreSwapCConfig(sort_key="seg_step_delta", score_margin=0.05, decel_gate=True)
        held_today = {"2330": 0.1, "2454": -0.3}
        sell, buy = _pick_swap_pair(
            slots,
            pool,
            held_ids={"2330", "2454"},
            config=cfg,
            held_today=held_today,
        )
        self.assertIsNotNone(sell)
        assert sell is not None
        self.assertEqual(sell["stock_id"], "2454")
        self.assertIsNotNone(buy)

    def test_decel_down_left_gate(self) -> None:
        slots = [
            {"stock_id": "2330", "seg_last": 3.0, "entry_date": "2026-01-01"},
            {"stock_id": "2454", "seg_last": 1.5, "entry_date": "2026-01-01"},
        ]
        pool = [_row("3008", 2.5)]
        cfg = ScoreSwapCConfig(
            sort_key="seg_last",
            score_margin=0.1,
            decel_gate=True,
            structural_gate="down_left",
        )
        held_today = {"2330": 0.2, "2454": -0.3}
        held_trend = {"2330": "up_right", "2454": "down_left"}
        sell, buy = _pick_swap_pair(
            slots,
            pool,
            held_ids={"2330", "2454"},
            config=cfg,
            held_today=held_today,
            held_trend=held_trend,
        )
        self.assertIsNotNone(sell)
        assert sell is not None
        self.assertEqual(sell["stock_id"], "2454")
        self.assertEqual(buy.stock_id if buy else None, "3008")

    def test_last_step_trend_down_left(self) -> None:
        import pandas as pd

        full_dates = ["2026-01-01", "2026-01-02"]
        rs_ratio = pd.DataFrame({"2330": [101.0, 100.0]}, index=full_dates)
        rs_mom = pd.DataFrame({"2330": [101.0, 99.0]}, index=full_dates)
        self.assertEqual(
            _last_step_trend(rs_ratio, rs_mom, full_dates, "2026-01-02", "2330"),
            "down_left",
        )

    def test_avg_acceleration_trend_down_left(self) -> None:
        import pandas as pd

        full_dates = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-06"]
        # velocities increasingly down-left -> avg accel down-left
        rs_ratio = pd.DataFrame({"2330": [100.0, 99.5, 99.0, 98.5]}, index=full_dates)
        rs_mom = pd.DataFrame({"2330": [100.0, 99.8, 99.5, 99.0]}, index=full_dates)
        from research.backtest.rrg_mono_score_swap_c import _avg_acceleration_trend

        self.assertEqual(
            _avg_acceleration_trend(rs_ratio, rs_mom, full_dates, "2026-01-06", "2330"),
            "down_left",
        )

    def test_entry_window_avg_accel(self) -> None:
        import pandas as pd

        from research.backtest.rrg_mono_score_swap_c import _entry_window_avg_accel_trend

        full_dates = [
            "2025-12-27",
            "2025-12-30",
            "2025-12-31",
            "2026-01-01",
            "2026-01-02",
            "2026-01-03",
            "2026-01-06",
            "2026-01-07",
            "2026-01-08",
        ]
        rs_ratio = pd.DataFrame(
            {"2330": [100.0, 99.9, 99.8, 99.7, 99.5, 99.2, 99.0, 98.7, 98.5]},
            index=full_dates,
        )
        rs_mom = pd.DataFrame(
            {"2330": [100.0, 99.95, 99.9, 99.8, 99.7, 99.4, 99.2, 99.0, 98.8]},
            index=full_dates,
        )
        trend = _entry_window_avg_accel_trend(
            rs_ratio,
            rs_mom,
            full_dates,
            entry_date="2026-01-03",
            as_of="2026-01-07",
            stock_id="2330",
        )
        self.assertEqual(trend, "down_left")
        self.assertIsNone(
            _entry_window_avg_accel_trend(
                rs_ratio,
                rs_mom,
                full_dates,
                entry_date="2026-01-03",
                as_of="2026-01-06",
                stock_id="2330",
                min_hold_days=2,
            )
        )

    def test_split_entry_avg_accel_pre_post(self) -> None:
        import pandas as pd

        from research.backtest.rrg_mono_score_swap_c import (
            _split_accel_gate_trend,
            _split_entry_avg_accel_trends,
        )

        full_dates = [
            "2025-12-27",
            "2025-12-30",
            "2025-12-31",
            "2026-01-01",
            "2026-01-02",
            "2026-01-03",
            "2026-01-06",
            "2026-01-07",
            "2026-01-08",
            "2026-01-09",
        ]
        rs_ratio = pd.DataFrame(
            {"2330": [100.0, 99.9, 99.8, 99.7, 99.5, 99.2, 99.0, 98.7, 98.5, 98.3]},
            index=full_dates,
        )
        rs_mom = pd.DataFrame(
            {"2330": [100.0, 99.95, 99.9, 99.8, 99.7, 99.4, 99.2, 99.0, 98.8, 98.6]},
            index=full_dates,
        )
        pre, post = _split_entry_avg_accel_trends(
            rs_ratio,
            rs_mom,
            full_dates,
            entry_date="2026-01-03",
            as_of="2026-01-08",
            stock_id="2330",
            pre_days=3,
            post_days=3,
            min_hold_days=3,
        )
        self.assertIsNotNone(pre)
        self.assertIsNotNone(post)
        self.assertEqual(_split_accel_gate_trend(pre, post, mode="post_down_left"), "down_left")

    def test_disp_accel_confirm_gate(self) -> None:
        from research.backtest.rrg_mono_score_swap_c import _held_structural_trend

        cfg = ScoreSwapCConfig(structural_gate="disp_accel_confirm")
        trend = _held_structural_trend(
            structural_gate="disp_accel_confirm",
            config=cfg,
            feat={"trend": "down_left"},
            rs_ratio=__import__("pandas").DataFrame(),
            rs_mom=__import__("pandas").DataFrame(),
            full_dates=[],
            as_of="",
            stock_id="2330",
            entry_date="2026-01-01",
        )
        # empty panels -> accel path returns "" without crash
        self.assertEqual(trend, "")

    def test_step_down_left_gate(self) -> None:
        slots = [
            {"stock_id": "2330", "seg_last": 3.0, "entry_date": "2026-01-01"},
            {"stock_id": "2454", "seg_last": 1.5, "entry_date": "2026-01-01"},
        ]
        cfg = ScoreSwapCConfig(
            sort_key="seg_last",
            score_margin=0.08,
            structural_gate="step_down_left",
        )
        held_trend = {"2330": "up_right", "2454": "down_left"}
        sell, buy = _pick_swap_pair(
            slots,
            [_row("3008", 2.5)],
            held_ids={"2330", "2454"},
            config=cfg,
            held_trend=held_trend,
        )
        self.assertIsNotNone(sell)
        assert sell is not None
        self.assertEqual(sell["stock_id"], "2454")

    def test_mom2_no_swap_without_decel(self) -> None:
        slots = [{"stock_id": "2330", "seg_last": 3.0, "entry_date": "2026-01-01"}]
        pool = [
            ScanRow(
                stock_id="3008",
                stock_name="3008",
                fresh=True,
                mono=True,
                seg_last=2.0,
                disp=1.0,
                segs=[1.0, 1.1, 1.2],
                quadrants=[],
                rs_ratio=100.0,
                rs_momentum=100.0,
                daily_pct=None,
            )
        ]
        cfg = ScoreSwapCConfig(sort_key="seg_step_delta", score_margin=0.05, decel_gate=True)
        sell, buy = _pick_swap_pair(
            slots,
            pool,
            held_ids={"2330"},
            config=cfg,
            held_today={"2330": 0.1},
        )
        self.assertIsNone(sell)
        self.assertIsNone(buy)

    def test_buy_sort_key_avg_accel_and_vdot_gate(self) -> None:
        slots = [
            {"stock_id": "2330", "seg_last": 1.0, "entry_date": "2026-01-01"},
        ]
        pool = [_row("3008", 2.0), _row("2317", 2.5)]
        cfg = ScoreSwapCConfig(
            sort_key="avg_accel_decel",
            score_margin=0.0,
            accel_sell_negative_only=False,
            challenger_gate="v_dot_positive",
            buy_sort_key="avg_accel_decel",
        )
        sell, buy = _pick_swap_pair(
            slots,
            pool,
            held_ids={"2330"},
            config=cfg,
            held_today={"2330": -0.5},
            challenger_va_dot={"3008": -0.1, "2317": 0.2},
            challenger_avg_accel={"3008": 0.3, "2317": 0.1},
        )
        self.assertIsNotNone(sell)
        self.assertIsNotNone(buy)
        assert buy is not None
        self.assertEqual(buy.stock_id, "2317")

    def test_vdot_gate_blocks_non_positive(self) -> None:
        slots = [{"stock_id": "2330", "seg_last": 1.0, "entry_date": "2026-01-01"}]
        pool = [_row("3008", 2.0)]
        cfg = ScoreSwapCConfig(
            sort_key="avg_accel_decel",
            score_margin=0.0,
            challenger_gate="v_dot_positive",
        )
        sell, buy = _pick_swap_pair(
            slots,
            pool,
            held_ids={"2330"},
            config=cfg,
            held_today={"2330": -0.5},
            challenger_va_dot={"3008": -0.2},
        )
        self.assertIsNone(sell)
        self.assertIsNone(buy)


class TestCandidatePool(unittest.TestCase):
    def test_mono_up_pool(self) -> None:
        from research.backtest.rrg_mono_score_swap_c import _candidate_pool

        row_f = _row("2330", 1.0)
        row_mu = _row("2454", 0.9)
        cfg = ScoreSwapCConfig(candidate_pool="mono_up")
        pool = _candidate_pool(
            "2024-01-04",
            fresh_mono=[row_f],
            mono_by_date={"2024-01-04": [_row("3008", 0.8)]},
            mono_up_by_date={"2024-01-04": [row_mu]},
            mono_up_fresh_by_date={"2024-01-04": [_row("2303", 0.7)]},
            config=cfg,
        )
        self.assertEqual(pool[0].stock_id, "2454")

    def test_mono_up_fresh_pool(self) -> None:
        from research.backtest.rrg_mono_score_swap_c import _candidate_pool

        row_muf = _row("2303", 0.7)
        cfg = ScoreSwapCConfig(candidate_pool="mono_up_fresh")
        pool = _candidate_pool(
            "2024-01-04",
            fresh_mono=[_row("2330", 1.0)],
            mono_by_date={},
            mono_up_by_date={"2024-01-04": [_row("2454", 0.9)]},
            mono_up_fresh_by_date={"2024-01-04": [row_muf]},
            config=cfg,
        )
        self.assertEqual(pool[0].stock_id, "2303")

    def test_split_entry_swap_pool(self) -> None:
        from research.backtest.rrg_mono_score_swap_c import _candidate_pool

        fresh = [_row("2330", 1.0)]
        mono = [_row("2454", 0.9), _row("3008", 0.8)]
        cfg = ScoreSwapCConfig(
            candidate_pool="fresh",
            entry_pool="fresh",
            swap_pool="mono_tier2",
        )
        entry = _candidate_pool(
            "2024-01-04",
            fresh_mono=fresh,
            mono_by_date={"2024-01-04": mono},
            mono_up_by_date={},
            mono_up_fresh_by_date={},
            config=cfg,
            pool_type="fresh",
        )
        swap = _candidate_pool(
            "2024-01-04",
            fresh_mono=fresh,
            mono_by_date={"2024-01-04": mono},
            mono_up_by_date={},
            mono_up_fresh_by_date={},
            config=cfg,
            pool_type="mono_tier2",
        )
        self.assertEqual([r.stock_id for r in entry], ["2330"])
        self.assertEqual([r.stock_id for r in swap], ["2454", "3008"])

    def test_fresh_union_accel_pool(self) -> None:
        import pandas as pd

        from research.backtest.rrg_mono_score_swap_c import _fresh_union_accel_pool

        full_dates = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-06"]
        rs_ratio = pd.DataFrame(
            {
                "2330": [100.0, 100.5, 101.0, 101.5],
                "2454": [100.0, 99.5, 99.0, 98.5],
            },
            index=full_dates,
        )
        rs_mom = pd.DataFrame(
            {
                "2330": [100.0, 100.3, 100.6, 100.9],
                "2454": [100.0, 99.8, 99.6, 99.4],
            },
            index=full_dates,
        )
        fresh = [_row("2330", 1.0)]
        mono = [_row("2454", 0.9)]
        pool = _fresh_union_accel_pool(
            fresh, mono, rs_ratio, rs_mom, full_dates, "2026-01-06", lb=3
        )
        self.assertEqual([r.stock_id for r in pool], ["2330"])

    def test_candidate_shortlist_passthrough(self) -> None:
        from research.backtest.rrg_mono_score_swap_c import (
            _candidate_shortlist,
            candidate_shortlist_is_passthrough,
        )

        pool = [_row("2330", 1.0), _row("2454", 0.5)]
        cfg = ScoreSwapCConfig()
        self.assertTrue(candidate_shortlist_is_passthrough(cfg))
        shortlist = _candidate_shortlist(
            pool,
            cfg,
            challenger_trend={},
            challenger_va_dot={},
            challenger_avg_accel={},
        )
        self.assertEqual(len(shortlist), 2)
        self.assertEqual(shortlist[0].stock_id, "2330")

    def test_candidate_shortlist_accel_rank(self) -> None:
        from research.backtest.rrg_mono_score_swap_c import _candidate_shortlist

        pool = [_row("2330", 1.0), _row("2454", 0.5)]
        cfg = ScoreSwapCConfig(
            candidate_rank_key="avg_accel_decel",
            candidate_require_positive_accel=True,
            candidate_top_n=1,
        )
        shortlist = _candidate_shortlist(
            pool,
            cfg,
            challenger_trend={},
            challenger_va_dot={},
            challenger_avg_accel={"2330": 0.2, "2454": 0.8},
        )
        self.assertEqual(len(shortlist), 1)
        self.assertEqual(shortlist[0].stock_id, "2454")

    def test_breadth_zone_ok(self) -> None:
        from research.backtest.rrg_mono_score_swap_c import _breadth_zone_ok

        self.assertTrue(_breadth_zone_ok("neutral", None))
        self.assertTrue(_breadth_zone_ok("strong", ["strong", "overbought"]))
        self.assertFalse(_breadth_zone_ok("neutral", ["strong", "overbought"]))

    def test_resolve_breadth_pool_types(self) -> None:
        from research.backtest.rrg_mono_score_swap_c import _resolve_breadth_pool_types

        zones = {"2026-01-01": "neutral", "2026-01-02": "strong"}
        base = ScoreSwapCConfig(candidate_pool="fresh")

        entry, swap = _resolve_breadth_pool_types("2026-01-01", zones, base)
        self.assertEqual((entry, swap), ("fresh", "fresh"))

        mono_cfg = ScoreSwapCConfig(candidate_pool="fresh", breadth_pool_mode="mono_in_hot_zones")
        entry, swap = _resolve_breadth_pool_types("2026-01-01", zones, mono_cfg)
        self.assertEqual((entry, swap), ("fresh", "fresh"))
        entry, swap = _resolve_breadth_pool_types("2026-01-02", zones, mono_cfg)
        self.assertEqual((entry, swap), ("mono_tier2", "mono_tier2"))

        swap_mono = ScoreSwapCConfig(
            candidate_pool="fresh",
            breadth_pool_mode="swap_mono_in_hot_zones",
        )
        entry, swap = _resolve_breadth_pool_types("2026-01-02", zones, swap_mono)
        self.assertEqual((entry, swap), ("fresh", "mono_tier2"))

        union_hot = ScoreSwapCConfig(
            candidate_pool="fresh",
            breadth_pool_mode="swap_union_accel_in_hot_zones",
        )
        entry, swap = _resolve_breadth_pool_types("2026-01-02", zones, union_hot)
        self.assertEqual((entry, swap), ("fresh", "fresh_union_accel"))

    def test_breadth_zone_on_date(self) -> None:
        from research.backtest.rrg_mono_score_swap_c import breadth_zone_on_date

        zones = {"2026-01-01": "neutral", "2026-01-02": "strong"}
        self.assertEqual(breadth_zone_on_date(zones, "2026-01-02"), "strong")
        self.assertEqual(breadth_zone_on_date(zones, "2026-01-99"), "unknown")

    def test_swap_gate_zone_date(self) -> None:
        from research.backtest.rrg_mono_score_swap_c import (
            _swap_allowed_for_leg,
            _swap_gate_zone_date,
        )

        cfg_swap = ScoreSwapCConfig(
            breadth_swap_zones=["strong", "overbought"],
            breadth_swap_zone_date="swap_day",
        )
        cfg_entry = ScoreSwapCConfig(
            breadth_swap_zones=["strong", "overbought"],
            breadth_swap_zone_date="entry_day",
        )
        zones = {"2026-01-01": "neutral", "2026-01-02": "strong"}
        sell = {"signal_date": "2026-01-01", "entry_date": "2026-01-01"}
        self.assertEqual(_swap_gate_zone_date("2026-01-02", sell, cfg_swap), "2026-01-02")
        self.assertEqual(_swap_gate_zone_date("2026-01-02", sell, cfg_entry), "2026-01-01")
        self.assertTrue(_swap_allowed_for_leg("2026-01-02", sell, zones, cfg_swap))
        self.assertFalse(_swap_allowed_for_leg("2026-01-02", sell, zones, cfg_entry))

    def test_mono_hot_swap_day_routing(self) -> None:
        from research.backtest.rrg_mono_score_swap_c import _resolve_breadth_pool_types

        zones = {"2026-01-02": "strong"}
        entry_day = ScoreSwapCConfig(
            candidate_pool="fresh",
            breadth_pool_mode="mono_in_hot_zones",
            breadth_challenger_pool_mode="entry_day",
        )
        swap_day = ScoreSwapCConfig(
            candidate_pool="fresh",
            breadth_pool_mode="mono_in_hot_zones",
            breadth_challenger_pool_mode="swap_day",
        )
        self.assertEqual(_resolve_breadth_pool_types("2026-01-02", zones, entry_day), ("mono_tier2", "mono_tier2"))
        self.assertEqual(_resolve_breadth_pool_types("2026-01-02", zones, swap_day), ("fresh", "mono_tier2"))


class TestEntryFallbackPool(unittest.TestCase):
    """H3: entry_fallback_pool 欄位存在且序列化正確。"""

    def test_field_default_none(self) -> None:
        cfg = ScoreSwapCConfig()
        self.assertIsNone(cfg.entry_fallback_pool)

    def test_field_set(self) -> None:
        cfg = ScoreSwapCConfig(entry_fallback_pool="mono_tier2")
        self.assertEqual(cfg.entry_fallback_pool, "mono_tier2")

    def test_to_dict_includes_field(self) -> None:
        cfg = ScoreSwapCConfig(entry_fallback_pool="mono_tier2")
        d = cfg.to_dict()
        self.assertIn("entry_fallback_pool", d)
        self.assertEqual(d["entry_fallback_pool"], "mono_tier2")

    def test_to_dict_none_when_not_set(self) -> None:
        cfg = ScoreSwapCConfig()
        self.assertIsNone(cfg.to_dict()["entry_fallback_pool"])


if __name__ == "__main__":
    unittest.main()
