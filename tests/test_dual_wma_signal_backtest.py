from __future__ import annotations

import unittest

from research.backtest.dual_wma_signal_backtest import _matches_imp_early_long
from rrg_universe_intraday_panel import SMALL_EXTENSION_MAX, SPREAD_ALIGNED_MAX


class MatchesImpEarlyLongTest(unittest.TestCase):
    def _pt(
        self,
        *,
        w20q: str = "improving",
        w5q: str = "improving",
        spread_dist: float = 1.0,
        spread_rs: float = 0.5,
        spread_mom: float = 0.2,
        w20_rs: float = 99.0,
        fresh: bool = True,
    ) -> dict:
        return {
            "fresh": fresh,
            "w20": {"quadrant": w20q, "rs_ratio": w20_rs},
            "w5": {"quadrant": w5q},
            "spread_dist": spread_dist,
            "spread_rs": spread_rs,
            "spread_mom": spread_mom,
        }

    def test_aligned_default_params(self) -> None:
        self.assertTrue(
            _matches_imp_early_long(
                self._pt(spread_dist=1.0),
                small_extension_max=SMALL_EXTENSION_MAX,
                aligned_max=SPREAD_ALIGNED_MAX,
                require_both_vectors=False,
                w20_rs_min=None,
                w20_rs_max=None,
            )
        )

    def test_rejects_stale(self) -> None:
        self.assertFalse(
            _matches_imp_early_long(
                self._pt(fresh=False),
                small_extension_max=SMALL_EXTENSION_MAX,
                aligned_max=SPREAD_ALIGNED_MAX,
                require_both_vectors=False,
                w20_rs_min=None,
                w20_rs_max=None,
            )
        )

    def test_small_extension_path(self) -> None:
        self.assertTrue(
            _matches_imp_early_long(
                self._pt(spread_dist=2.5, spread_rs=1.5, spread_mom=1.5),
                small_extension_max=3.0,
                aligned_max=1.5,
                require_both_vectors=False,
                w20_rs_min=None,
                w20_rs_max=None,
            )
        )

    def test_aligned_only_when_small_ext_disabled(self) -> None:
        self.assertTrue(
            _matches_imp_early_long(
                self._pt(spread_dist=1.0),
                small_extension_max=None,
                aligned_max=1.5,
                require_both_vectors=False,
                w20_rs_min=None,
                w20_rs_max=None,
            )
        )
        self.assertFalse(
            _matches_imp_early_long(
                self._pt(spread_dist=2.5, spread_rs=1.5, spread_mom=1.5),
                small_extension_max=None,
                aligned_max=1.5,
                require_both_vectors=False,
                w20_rs_min=None,
                w20_rs_max=None,
            )
        )

    def test_require_both_vectors(self) -> None:
        pt_or_ok = self._pt(spread_rs=0.5, spread_mom=-0.1)
        self.assertTrue(
            _matches_imp_early_long(
                pt_or_ok,
                small_extension_max=SMALL_EXTENSION_MAX,
                aligned_max=SPREAD_ALIGNED_MAX,
                require_both_vectors=False,
                w20_rs_min=None,
                w20_rs_max=None,
            )
        )
        self.assertFalse(
            _matches_imp_early_long(
                pt_or_ok,
                small_extension_max=SMALL_EXTENSION_MAX,
                aligned_max=SPREAD_ALIGNED_MAX,
                require_both_vectors=True,
                w20_rs_min=None,
                w20_rs_max=None,
            )
        )

    def test_w20_rs_band(self) -> None:
        self.assertTrue(
            _matches_imp_early_long(
                self._pt(w20_rs=99.0),
                small_extension_max=SMALL_EXTENSION_MAX,
                aligned_max=SPREAD_ALIGNED_MAX,
                require_both_vectors=False,
                w20_rs_min=98.0,
                w20_rs_max=100.0,
            )
        )
        self.assertFalse(
            _matches_imp_early_long(
                self._pt(w20_rs=97.5),
                small_extension_max=SMALL_EXTENSION_MAX,
                aligned_max=SPREAD_ALIGNED_MAX,
                require_both_vectors=False,
                w20_rs_min=98.0,
                w20_rs_max=100.0,
            )
        )

    def test_tighter_aligned_max(self) -> None:
        self.assertFalse(
            _matches_imp_early_long(
                self._pt(spread_dist=1.3),
                small_extension_max=SMALL_EXTENSION_MAX,
                aligned_max=1.0,
                require_both_vectors=False,
                w20_rs_min=None,
                w20_rs_max=None,
            )
        )

    def test_entry_params_grid_skips_invalid_small_ext(self) -> None:
        from research.backtest.dual_wma_signal_backtest import imp_early_long_entry_grid

        grid = imp_early_long_entry_grid()
        small_exts = {ep.small_extension_max for ep in grid}
        self.assertNotIn(2.0, small_exts)
        self.assertGreaterEqual(len(grid), 60)
        self.assertLessEqual(len(grid), 120)


class ExitFrameIdxTest(unittest.TestCase):
    def _frames(self, n: int) -> list[dict[str, str]]:
        return [{"id": f"f{i}", "date": f"2026-04-{1 + i // 10:02d}", "minute": "09:00"} for i in range(n)]

    def _pt(self, w20q: str, w5q: str = "improving", spread_dist: float = 3.0) -> dict:
        return {
            "w20": {"quadrant": w20q},
            "w5": {"quadrant": w5q},
            "spread_dist": spread_dist,
            "spread_class": "extension",
        }

    def test_hybrid_cap5d_w20_weak_exits_early(self) -> None:
        from research.backtest.dual_wma_signal_backtest import _exit_frame_idx, _hold_nd_frame_idx

        frames = self._frames(20)
        points: list[dict | None] = [None] * 20
        points[3] = self._pt("weakening")
        cap = _hold_nd_frame_idx(0, frames, 5)
        idx = _exit_frame_idx("hybrid_cap5d_w20_weak", 0, frames, points, max_forward=20)
        self.assertEqual(idx, 3)
        self.assertLess(idx, cap)

    def test_exit_w5_leading(self) -> None:
        from research.backtest.dual_wma_signal_backtest import _exit_frame_idx

        frames = self._frames(10)
        points: list[dict | None] = [None] * 10
        points[2] = self._pt("improving", w5q="leading")
        idx = _exit_frame_idx("exit_w5_leading", 0, frames, points, max_forward=10)
        self.assertEqual(idx, 2)

    def test_champion_entry_params(self) -> None:
        from research.backtest.dual_wma_signal_backtest import imp_early_long_champion_entry

        ep = imp_early_long_champion_entry()
        self.assertEqual(ep.small_extension_max, 2.5)
        self.assertEqual(ep.aligned_max, 1.0)
        self.assertTrue(ep.require_both_vectors)
        self.assertEqual(ep.w20_rs_min, 96.0)


class W20LeadingExitTest(unittest.TestCase):
    def _frames(self, n: int) -> list[dict[str, str]]:
        return [
            {
                "id": f"f{i}",
                "date": f"2026-04-{1 + i:02d}",
                "minute": "13:30",
            }
            for i in range(n)
        ]

    def _pt(self, w20q: str, w5q: str = "improving", spread_class: str = "aligned") -> dict:
        return {
            "w20": {"quadrant": w20q, "rs_ratio": 100.5},
            "w5": {"quadrant": w5q},
            "spread_class": spread_class,
            "spread_dist": 1.0,
        }

    def test_w5_gate_delays_exit(self) -> None:
        from research.backtest.dual_wma_signal_backtest import (
            W20LeadingExitParams,
            _exit_frame_idx_w20_leading,
        )

        frames = self._frames(8)
        points: list[dict | None] = [None] * 8
        points[2] = self._pt("leading", "improving")
        points[4] = self._pt("leading", "leading")
        xp = W20LeadingExitParams(w5_gate="leading", cap_days=5)
        idx = _exit_frame_idx_w20_leading(0, frames, points, xp, max_forward=8)
        self.assertEqual(idx, 4)

    def test_trail_w5_extension_after_w20(self) -> None:
        from research.backtest.dual_wma_signal_backtest import (
            W20LeadingExitParams,
            _exit_frame_idx_w20_leading,
        )

        frames = self._frames(8)
        points: list[dict | None] = [None] * 8
        points[2] = self._pt("leading", "improving")
        points[5] = self._pt("leading", "leading", spread_class="extension")
        xp = W20LeadingExitParams(trail_after_w20="w5_extension", cap_days=5)
        idx = _exit_frame_idx_w20_leading(0, frames, points, xp, max_forward=8)
        self.assertEqual(idx, 5)

    def test_confirm_polls_requires_streak(self) -> None:
        from research.backtest.dual_wma_signal_backtest import (
            W20LeadingExitParams,
            _exit_frame_idx_w20_leading,
        )

        frames = self._frames(8)
        points: list[dict | None] = [None] * 8
        points[2] = self._pt("leading")
        points[3] = self._pt("improving")
        points[4] = self._pt("leading")
        points[5] = self._pt("leading")
        xp = W20LeadingExitParams(confirm_polls=2, cap_days=5)
        idx = _exit_frame_idx_w20_leading(0, frames, points, xp, max_forward=8)
        self.assertEqual(idx, 5)

    def test_exit_grid_size(self) -> None:
        from research.backtest.dual_wma_signal_backtest import w20_leading_exit_grid

        grid = w20_leading_exit_grid()
        self.assertGreaterEqual(len(grid), 40)
        self.assertLessEqual(len(grid), 120)


class RegimeFilterTest(unittest.TestCase):
    def test_passes_breadth_and_tape(self) -> None:
        from research.backtest.dual_wma_signal_backtest import (
            RegimeFilterParams,
            _passes_regime_filter,
        )

        ctx = {
            "zone_by_date": {"2026-04-01": "neutral"},
            "prior_by_date": {"2026-04-02": "2026-04-01"},
            "flow_tape_by_date": {"2026-04-02": "多頭"},
        }
        rp = RegimeFilterParams(
            breadth_zones=("neutral", "strong"),
            flow_tape_regimes=("多頭", "震盪"),
        )
        self.assertTrue(
            _passes_regime_filter("2026-04-02", rp, ctx, chase_pct=0.5)
        )
        self.assertFalse(
            _passes_regime_filter(
                "2026-04-02",
                RegimeFilterParams(breadth_zones=("overbought",)),
                ctx,
                chase_pct=0.5,
            )
        )

    def test_chase_filter(self) -> None:
        from research.backtest.dual_wma_signal_backtest import (
            RegimeFilterParams,
            _passes_regime_filter,
        )

        ctx = {
            "zone_by_date": {},
            "prior_by_date": {"2026-04-02": "2026-04-01"},
            "flow_tape_by_date": {"2026-04-02": "震盪"},
        }
        rp = RegimeFilterParams(max_entry_chase_pct=1.0)
        self.assertTrue(_passes_regime_filter("2026-04-02", rp, ctx, chase_pct=0.8))
        self.assertFalse(_passes_regime_filter("2026-04-02", rp, ctx, chase_pct=1.5))

    def test_regime_grid_has_baseline(self) -> None:
        from research.backtest.dual_wma_signal_backtest import regime_filter_grid

        grid = regime_filter_grid()
        self.assertGreaterEqual(len(grid), 10)
        self.assertTrue(
            any(
                not g.breadth_zones and not g.flow_tape_regimes and g.max_entry_chase_pct is None
                for g in grid
            )
        )


if __name__ == "__main__":
    unittest.main()
