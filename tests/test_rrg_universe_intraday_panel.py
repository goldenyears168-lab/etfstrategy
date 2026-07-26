from __future__ import annotations

import unittest

from rrg_universe_intraday_panel import (
    _densify_intraday_points,
    build_intraday_frames,
    classify_wma_spread,
)


class DensifyIntradayPointsTest(unittest.TestCase):
    def test_forward_fills_after_first_kbar(self) -> None:
        frames = build_intraday_frames(["2026-04-01"])
        sparse = [
            {
                "frame_id": "2026-04-01T09:00",
                "date": "2026-04-01",
                "minute": "09:00",
                "rs_ratio": 100.0,
                "rs_momentum": 101.0,
                "quadrant": "leading",
            },
            {
                "frame_id": "2026-04-01T10:00",
                "date": "2026-04-01",
                "minute": "10:00",
                "rs_ratio": 102.0,
                "rs_momentum": 103.0,
                "quadrant": "leading",
            },
        ]
        dense = _densify_intraday_points(sparse, frames)
        self.assertEqual(len(dense), len(frames))
        self.assertEqual(dense[0]["frame_id"], "2026-04-01T09:00")
        self.assertEqual(dense[1]["minute"], "09:30")
        self.assertTrue(dense[1]["carried"])
        self.assertEqual(dense[1]["rs_ratio"], 100.0)
        self.assertEqual(dense[2]["minute"], "10:00")
        self.assertFalse(dense[2].get("carried"))
        self.assertEqual(dense[2]["rs_ratio"], 102.0)
        self.assertEqual(dense[3]["minute"], "10:30")
        self.assertTrue(dense[3]["carried"])
        self.assertEqual(dense[3]["rs_ratio"], 102.0)


class ClassifyWmaSpreadTest(unittest.TestCase):
    def test_pullback_extension_aligned(self) -> None:
        self.assertEqual(classify_wma_spread(98.0, 98.0, 101.0, 101.0)[0], "pullback")
        self.assertEqual(classify_wma_spread(101.0, 101.0, 98.0, 98.0)[0], "extension")
        self.assertEqual(classify_wma_spread(100.0, 100.5, 100.2, 100.0)[0], "aligned")


class ClassifyDualWmaTradeSignalTest(unittest.TestCase):
    def _pt(
        self,
        *,
        w20q: str,
        w5q: str,
        spread_class: str,
        spread_dist: float = 2.5,
        spread_rs: float = 0.0,
        spread_mom: float = 0.0,
        fresh: bool = True,
    ) -> dict:
        return {
            "fresh": fresh,
            "w20": {"quadrant": w20q},
            "w5": {"quadrant": w5q},
            "spread_class": spread_class,
            "spread_dist": spread_dist,
            "spread_rs": spread_rs,
            "spread_mom": spread_mom,
        }

    def test_four_signals(self) -> None:
        from rrg_universe_intraday_panel import classify_dual_wma_trade_signal

        self.assertEqual(
            classify_dual_wma_trade_signal(
                self._pt(
                    w20q="improving",
                    w5q="leading",
                    spread_class="extension",
                    spread_rs=2,
                    spread_mom=2,
                )
            ),
            "imp_extension",
        )
        self.assertEqual(
            classify_dual_wma_trade_signal(
                self._pt(
                    w20q="improving",
                    w5q="improving",
                    spread_class="aligned",
                    spread_rs=0.5,
                    spread_mom=0.2,
                )
            ),
            "imp_early_long",
        )
        self.assertEqual(
            classify_dual_wma_trade_signal(
                self._pt(
                    w20q="leading",
                    w5q="improving",
                    spread_class="pullback",
                    spread_rs=-2,
                    spread_mom=-2,
                )
            ),
            "lead_pullback_buy",
        )
        self.assertEqual(
            classify_dual_wma_trade_signal(
                self._pt(
                    w20q="leading",
                    w5q="lagging",
                    spread_class="pullback",
                    spread_dist=4.0,
                    spread_rs=-3,
                    spread_mom=-3,
                )
            ),
            "lead_deep_pullback_wait",
        )
        self.assertIsNone(
            classify_dual_wma_trade_signal(
                self._pt(w20q="lagging", w5q="leading", spread_class="extension")
            )
        )


class TestSummarizeLeadPullbackShowcase(unittest.TestCase):
    def test_counts_spreads_and_top_cases(self) -> None:
        from rrg_universe_intraday_panel import summarize_lead_pullback_showcase

        frames = [
            {"id": "2026-04-01T09:00", "date": "2026-04-01", "minute": "09:00"},
            {"id": "2026-04-01T09:30", "date": "2026-04-01", "minute": "09:30"},
            {"id": "2026-04-01T10:00", "date": "2026-04-01", "minute": "10:00"},
        ]
        trajectories = [
            {
                "stock_id": "2330",
                "stock_name": "台積電",
                "points": [
                    {
                        "frame_id": "2026-04-01T09:00",
                        "trade_signal": "lead_pullback_buy",
                        "fresh": True,
                        "spread_dist": 2.0,
                        "w20": {"quadrant": "leading"},
                        "w5": {"quadrant": "improving"},
                    },
                    {
                        "frame_id": "2026-04-01T09:30",
                        "trade_signal": "lead_pullback_buy",
                        "fresh": True,
                        "spread_dist": 4.5,
                        "w20": {"quadrant": "leading"},
                        "w5": {"quadrant": "improving"},
                    },
                    {
                        "frame_id": "2026-04-01T10:00",
                        "trade_signal": "imp_extension",
                        "fresh": True,
                        "spread_dist": 5.0,
                    },
                ],
            },
            {
                "stock_id": "2317",
                "stock_name": "鴻海",
                "points": [
                    {
                        "frame_id": "2026-04-01T09:30",
                        "trade_signal": "lead_pullback_buy",
                        "fresh": True,
                        "spread_dist": 3.0,
                        "w20": {"quadrant": "leading"},
                        "w5": {"quadrant": "improving"},
                    },
                    {
                        "frame_id": "2026-04-01T10:00",
                        "trade_signal": "lead_pullback_buy",
                        "fresh": False,
                        "spread_dist": 9.0,
                    },
                ],
            },
        ]
        stats = summarize_lead_pullback_showcase(trajectories, frames)
        self.assertEqual(stats["n_events"], 3)
        self.assertEqual(stats["n_stocks"], 2)
        self.assertEqual(stats["n_signal_frames"], 2)
        self.assertEqual(stats["signal_frame_indices"], [0, 1])
        self.assertEqual(stats["spread_median"], 3.0)
        self.assertEqual(stats["spread_p75"], 4.5)
        self.assertEqual(len(stats["top_cases"]), 3)
        self.assertEqual(stats["top_cases"][0]["stock_id"], "2330")
        self.assertEqual(stats["top_cases"][0]["spread_dist"], 4.5)
        self.assertEqual(stats["top_cases"][0]["frame_idx"], 1)


class TestLeadPullbackObservationPool(unittest.TestCase):
    def test_pool_filters_default_lead_pullback(self) -> None:
        from unittest.mock import MagicMock, patch

        from rrg_universe_intraday_panel import build_dual_wma_lead_pullback_observation_pool

        conn = MagicMock()
        with patch(
            "rrg_universe_intraday_panel.load_etf_constituent_watchlist",
            return_value=[{"stock_id": "2330", "stock_name": "台積電", "etf_hold_count": 1}],
        ):
            with patch(
                "rrg_universe_intraday_panel.load_price_panels",
                return_value=(MagicMock(), None, None),
            ) as load_px:
                class _IndexList(list):
                    """list + `.tolist()` — mimics `DataFrame.index.astype(str)`."""

                    def tolist(self) -> list:
                        return list(self)

                close = MagicMock()
                close.columns = ["2330"]
                close.index.astype.return_value = _IndexList(["2026-07-03"])
                # Column-subsetting (`close[universe_cols]`) on a real DataFrame
                # keeps the same row index; mimic that here so the mock doesn't
                # lose its configured `.index` after the subset.
                close.__getitem__.return_value = close
                load_px.return_value = (close, None, None)
                with patch(
                    "rrg_universe_intraday_panel.load_benchmark_close",
                    return_value=MagicMock(reindex=MagicMock(return_value=MagicMock())),
                ):
                    with patch(
                        "rrg_universe_intraday_panel._slice_panels_for_date",
                        return_value=(MagicMock(empty=False), MagicMock(), ["2026-07-03"]),
                    ):
                        with patch(
                            "rrg_universe_intraday_panel._patch_intraday_close",
                            return_value={"2330"},
                        ):
                            with patch(
                                "rrg_universe_intraday_panel.compute_rrg_panel",
                                side_effect=[
                                    (MagicMock(), MagicMock(), None),
                                    (MagicMock(), MagicMock(), None),
                                    (MagicMock(), MagicMock(), None),
                                ],
                            ) as compute:
                                rs5 = MagicMock()
                                rs5.index = ["2026-07-03"]
                                rs5.columns = ["2330"]
                                rs5.at = MagicMock(return_value=101.0)
                                mom5 = MagicMock()
                                mom5.at = MagicMock(return_value=102.0)
                                rs20 = MagicMock()
                                rs20.index = ["2026-07-03"]
                                rs20.columns = ["2330"]
                                rs20.at = MagicMock(return_value=103.0)
                                mom20 = MagicMock()
                                mom20.at = MagicMock(return_value=104.0)
                                # W3 micro (rs3/mom3) — used only for optional
                                # w3_mom enrichment, unconfigured mock is fine.
                                rs3 = MagicMock()
                                mom3 = MagicMock()
                                compute.side_effect = [
                                    (rs5, mom5, None),
                                    (rs20, mom20, None),
                                    (rs3, mom3, None),
                                ]
                                with patch(
                                    "rrg_universe_intraday_panel.classify_dual_wma_trade_signal",
                                    return_value="lead_pullback_buy",
                                ):
                                    with patch(
                                        "rrg_universe_intraday_panel._dual_point",
                                        return_value={
                                            "w20": {"quadrant": "leading"},
                                            "w5": {"quadrant": "improving"},
                                            "spread_class": "pullback",
                                            "spread_dist": 3.2,
                                        },
                                    ):
                                        rows, as_of = build_dual_wma_lead_pullback_observation_pool(
                                            conn,
                                            session="2026-07-03",
                                            poll_minute="10:30",
                                        )
        self.assertEqual(as_of, "2026-07-03")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].stock_id, "2330")
        self.assertEqual(rows[0].seg_last, 3.2)


if __name__ == "__main__":
    unittest.main()
