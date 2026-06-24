"""Tests for RRG mono hold7 intraday A/B/C helpers."""

from __future__ import annotations

import unittest

from research.backtest.rrg_mono_intraday_ab import (
    C18ACC_ENTRY_FILL_SWEEP,
    CVariantConfig,
    _apply_intraday_entries,
    _expert_fill_mode,
    close_shortlist,
    intraday_price_scale,
    rank_shortlist_scale,
    scaled_seg_last,
    vcp_close_shortlist,
)
from rrg_mono_daily_brief import ScanRow


def _row(stock_id: str, seg_last: float) -> ScanRow:
    return ScanRow(
        stock_id=stock_id,
        stock_name=stock_id,
        fresh=True,
        mono=True,
        seg_last=seg_last,
        disp=1.2,
        segs=[0.0, 0.5, seg_last],
        quadrants=["lagging", "improving", "leading"],
        rs_ratio=105.0,
        rs_momentum=102.0,
        daily_pct=1.0,
    )


class TestRrgMonoIntradayAb(unittest.TestCase):
    def test_close_shortlist_caps_at_ten(self) -> None:
        rows = [_row(str(i), float(i)) for i in range(15)]
        short = close_shortlist(rows)
        self.assertEqual(len(short), 10)
        self.assertEqual(short[0].stock_id, "14")

    def test_scaled_seg_last(self) -> None:
        self.assertAlmostEqual(scaled_seg_last(_row("1", 2.0), 1.5), 3.0)

    def test_intraday_price_scale_clamps(self) -> None:
        self.assertEqual(intraday_price_scale(100.0, 300.0), 2.5)
        self.assertEqual(intraday_price_scale(100.0, None), 1.0)

    def test_vcp_close_shortlist_sorts_by_composite(self) -> None:
        rows = [
            ScanRow(
                stock_id="2330",
                stock_name="台積電",
                fresh=False,
                mono=False,
                seg_last=1.0,
                disp=1.2,
                segs=[],
                quadrants=[],
                rs_ratio=100.0,
                rs_momentum=100.0,
                daily_pct=None,
                composite_score=60.0,
            ),
            ScanRow(
                stock_id="2454",
                stock_name="聯發科",
                fresh=False,
                mono=False,
                seg_last=2.0,
                disp=1.2,
                segs=[],
                quadrants=[],
                rs_ratio=100.0,
                rs_momentum=100.0,
                daily_pct=None,
                composite_score=70.0,
            ),
        ]
        short = vcp_close_shortlist(rows)
        self.assertEqual(short[0].stock_id, "2454")

    def test_rank_shortlist_prefers_intraday_momentum(self) -> None:
        import sqlite3

        import pandas as pd

        rows = [_row("A", 2.5), _row("B", 2.0)]
        close = pd.DataFrame({"A": [100.0], "B": [100.0]}, index=["2026-06-01"])
        conn = sqlite3.connect(":memory:")
        kbar_cache = {
            ("A", "2026-06-01"): (("09:30:00", 100.0), ("10:00:00", 100.0)),
            ("B", "2026-06-01"): (("09:30:00", 100.0), ("10:00:00", 150.0)),
        }
        ranked = rank_shortlist_scale(
            rows,
            conn=conn,
            close=close,
            trade_date="2026-06-01",
            minute="10:00",
            kbar_cache=kbar_cache,
        )
        conn.close()
        self.assertEqual([r.stock_id for r in ranked], ["B", "A"])

    def test_expert_fill_mode_mapping(self) -> None:
        self.assertIsNone(_expert_fill_mode(CVariantConfig(entry_fill_mode="poll_px")))
        self.assertEqual(
            _expert_fill_mode(CVariantConfig(entry_fill_mode="vwap_reclaim")),
            "vwap_reclaim",
        )

    def test_c18acc_entry_sweep_has_five_variants(self) -> None:
        self.assertEqual(len(C18ACC_ENTRY_FILL_SWEEP), 5)
        ids = {c.variant_id for c in C18ACC_ENTRY_FILL_SWEEP}
        self.assertIn("C0", ids)
        self.assertIn("C18acc-vwap", ids)

    def test_apply_intraday_expert_fill_waits_for_trigger(self) -> None:
        import sqlite3
        from unittest.mock import patch

        import pandas as pd

        from stock_db.kbar import KbarBar

        rows = [_row("A", 2.5)]
        close = pd.DataFrame({"A": [100.0]}, index=["2026-06-01"])
        bench = pd.Series([100.0], index=["2026-06-01"])
        conn = sqlite3.connect(":memory:")
        kbar_cache = {
            ("A", "2026-06-01"): (
                ("09:30:00", 99.0),
                ("09:35:00", 99.5),
                ("10:00:00", 100.5),
            ),
        }
        ohlcv = {
            ("A", "2026-06-01"): (
                KbarBar("09:30:00", 100, 100.2, 99.5, 99.6, 1000),
                KbarBar("09:35:00", 99.6, 100.2, 99.4, 100.1, 1000),
            ),
        }
        cfg = CVariantConfig(
            variant_id="test-vwap",
            confirm_bars=1,
            no_swap_before="09:30",
            entry_fill_mode="vwap_reclaim",
        )
        state: dict = {"slots": []}

        def _fake_append(**kwargs: object) -> dict:
            return {
                "stock_id": "A",
                "entry_px": kwargs["entry_px"],
                "entry_minute": kwargs.get("entry_minute"),
                "slot": 0,
            }

        with patch(
            "research.backtest.rrg_mono_intraday_ab._append_position",
            side_effect=lambda **kw: _fake_append(**kw),
        ):
            added = _apply_intraday_entries(
                conn,
                state,
                signal_date="2026-06-01",
                entry_date="2026-06-01",
                shortlist=rows,
                close=close,
                bench=bench,
                full_dates=["2026-06-01"],
                config=cfg,
                kbar_cache=kbar_cache,
                kbar_stats={"hits": 0, "checks": 0},
                ohlcv_cache=ohlcv,
            )
        conn.close()
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["entry_minute"], "09:35:00")
        self.assertAlmostEqual(added[0]["entry_px"], 100.1)


if __name__ == "__main__":
    unittest.main()
