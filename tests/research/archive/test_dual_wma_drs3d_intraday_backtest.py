from __future__ import annotations

import unittest

from research.backtest.archive.dual_wma_drs3d_intraday_backtest import (
    _eod_frame_idx,
    _matches_imp_extension_strict,
    _w20_rsr,
    compute_intraday_spread_score,
    evaluate_signal_ic,
)


class DualWmaDrs3dIntradayTest(unittest.TestCase):
    def _pt(
        self,
        *,
        w20q: str = "improving",
        w20_rs: float = 99.0,
        spread_class: str = "extension",
        spread_rs: float = 1.5,
        spread_mom: float = 1.0,
        spread_dist: float = 2.5,
        fresh: bool = True,
    ) -> dict:
        return {
            "fresh": fresh,
            "w20": {"quadrant": w20q, "rs_ratio": w20_rs},
            "spread_class": spread_class,
            "spread_rs": spread_rs,
            "spread_mom": spread_mom,
            "spread_dist": spread_dist,
        }

    def test_strict_extension_requires_spread_rs_positive(self) -> None:
        self.assertTrue(_matches_imp_extension_strict(self._pt()))
        self.assertFalse(_matches_imp_extension_strict(self._pt(spread_rs=-0.1)))
        self.assertFalse(_matches_imp_extension_strict(self._pt(spread_class="pullback")))

    def test_intraday_score_ranks_extension_higher(self) -> None:
        ext = compute_intraday_spread_score(self._pt())
        pull = compute_intraday_spread_score(
            self._pt(spread_class="pullback", spread_rs=-1, spread_mom=-1, spread_dist=3)
        )
        self.assertGreater(ext, pull)

    def test_eod_frame_idx(self) -> None:
        frames = [
            {"id": "a", "date": "2026-04-01", "minute": "09:00"},
            {"id": "b", "date": "2026-04-01", "minute": "13:30"},
            {"id": "c", "date": "2026-04-02", "minute": "09:00"},
        ]
        self.assertEqual(_eod_frame_idx(0, frames), 1)
        self.assertEqual(_eod_frame_idx(1, frames), 1)

    def test_w20_rsr(self) -> None:
        self.assertAlmostEqual(_w20_rsr({"w20": {"rs_ratio": 99.5}}), 99.5)

    def test_evaluate_ic_smoke(self) -> None:
        import pandas as pd

        df = pd.DataFrame(
            {
                "signal": ["imp_extension"] * 20,
                "intraday_score": [float(i) / 20 for i in range(20)],
                "daily_score": [float(i) / 25 for i in range(20)],
                "drs_rest_d": [float(i) * 0.1 for i in range(20)],
                "drs_3d": [float(i) * 0.05 for i in range(20)],
                "ret_3d_net": [0.1] * 20,
                "flip_leading_3d": [False] * 20,
            }
        )
        r = evaluate_signal_ic(df, signal="imp_extension")
        self.assertEqual(r.n, 20)
        self.assertIsNotNone(r.ic_intraday_vs_drs_rest)
        self.assertGreater(r.ic_intraday_vs_drs_rest or 0, 0.9)


if __name__ == "__main__":
    unittest.main()
