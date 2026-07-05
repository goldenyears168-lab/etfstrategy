"""Tests for hybrid drs_5d + ret_5d research."""

from __future__ import annotations

import unittest

from research.backtest.rrg_hybrid_drs5d_ret5d_backtest import (
    _bench_tier,
    compute_hybrid_intraday_score,
    compute_hybrid_structural_score,
    compute_ret5d_score,
    run_walk_forward_ret5d,
)


def _row(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "drs_rsr_5d": 1.5,
        "drs_rsr_4d": 1.2,
        "drs_rsr_1d": 0.4,
        "heading_rsr": 0.7,
        "mono_up": True,
        "disp": 0.8,
        "sig_ret": 1.0,
        "spread_rs": 1.5,
        "spread_class": "extension",
        "w5_drs_rsr_1d": 0.5,
        "bench_today_ret": 1.2,
        "roe_latest_q": 12.0,
        "revenue_yoy_pct": 15.0,
    }
    base.update(kw)
    return base


class TestHybridRet5d(unittest.TestCase):
    def test_extension_bonus_raises_hybrid(self) -> None:
        hi = compute_hybrid_structural_score(_row(spread_rs=2.0, spread_class="extension"))
        lo = compute_hybrid_structural_score(_row(spread_rs=-1.0, spread_class="pullback"))
        self.assertGreater(hi, lo)

    def test_ret5d_score_uses_bench_not_struct(self) -> None:
        pos = compute_ret5d_score(_row(bench_today_ret=2.0, w5_drs_rsr_1d=0.8))
        neg = compute_ret5d_score(_row(bench_today_ret=-2.0, w5_drs_rsr_1d=0.8))
        self.assertGreater(pos, neg)

    def test_chase_penalty_ret5d(self) -> None:
        ok = compute_ret5d_score(_row(sig_ret=2.0))
        chase = compute_ret5d_score(_row(sig_ret=5.0))
        self.assertGreater(ok, chase)

    def test_intraday_bonus_raises_hybrid(self) -> None:
        base = _row(intraday_extension=False)
        with_id = _row(intraday_extension=True)
        self.assertGreater(
            compute_hybrid_intraday_score(with_id),
            compute_hybrid_intraday_score(base),
        )

    def test_bench_tier(self) -> None:
        self.assertEqual(_bench_tier(2.5), "b2")
        self.assertEqual(_bench_tier(1.5), "b1")
        self.assertEqual(_bench_tier(0.5), "b0")
        self.assertEqual(_bench_tier(-0.1), "neg")

    def test_walk_forward_ret5d_synthetic(self) -> None:
        import pandas as pd

        rows = []
        for yr in range(2018, 2026):
            for mo in (3, 6, 9):
                d = f"{yr}-{mo:02d}-15"
                for i in range(40):
                    rows.append(
                        {
                            "date": d,
                            "score_ret5d": float(i) / 40.0,
                            "ret_5d": float(i) * 0.01,
                        }
                    )
        df = pd.DataFrame(rows)
        wfa = run_walk_forward_ret5d(df, train_years=3, test_years=1)
        self.assertGreater(wfa["summary"].get("n_folds", 0), 0)


if __name__ == "__main__":
    unittest.main()
