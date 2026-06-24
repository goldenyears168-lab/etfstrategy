"""Tests for rrg_mono_swap_accel_daily_brief."""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from research.backtest.rrg_mono_score_swap_c import (
    CHAMPION_SCORE_SWAP_C_VARIANT_ID,
    champion_score_swap_c_config,
)
from rrg_mono_daily_brief import ScanRow, TOP_N
from rrg_mono_swap_accel_daily_brief import (
    BRIEF_KIND,
    build_payload,
    render_markdown,
)


def _sample_row(sid: str, seg: float, *, fresh: bool = True) -> ScanRow:
    return ScanRow(
        stock_id=sid,
        stock_name=f"N{sid}",
        fresh=fresh,
        mono=True,
        seg_last=seg,
        disp=1.2,
        segs=[0.5, 0.8, seg],
        quadrants=["leading"] * 4,
        rs_ratio=100.0,
        rs_momentum=101.0,
        daily_pct=1.0,
    )


class TestRrgMonoSwapAccelDailyBrief(unittest.TestCase):
    def test_render_markdown_main_flow(self) -> None:
        cfg = champion_score_swap_c_config()
        pool = [_sample_row("2330", 1.5), _sample_row("2317", 1.2)]
        payload = {
            "as_of": "2026-06-24",
            "brief_kind": BRIEF_KIND,
            "config": cfg,
            "tomorrow_pool": pool,
            "pool_fresh_n": 2,
            "slots": [
                {
                    "slot": 0,
                    "stock_id": "2454",
                    "stock_name": "聯發科",
                    "entry_date": "2026-06-20",
                    "seg_last": 1.0,
                }
            ],
            "held_accel": {"2454": -0.05},
            "challenger_accel": {"2330": 0.12, "2317": 0.08},
            "proximity": [],
            "hypothetical_swap_sell": None,
            "hypothetical_swap_buy": None,
            "breadth_zone": "strong",
            "breadth_zone_zh": "強勢",
            "session_dates": ["2026-06-20", "2026-06-23", "2026-06-24"],
        }
        md = render_markdown(payload)
        self.assertIn("C18acc", md)
        self.assertIn("Scheme A", md)
        self.assertIn("2330", md)
        self.assertIn("2454", md)
        self.assertIn("強勢", md)
        self.assertIn(str(cfg.min_hold_days), md)
        self.assertIn(str(cfg.max_hold_days), md)

    def test_render_markdown_empty_pool(self) -> None:
        cfg = champion_score_swap_c_config()
        payload = {
            "as_of": "2026-06-24",
            "brief_kind": BRIEF_KIND,
            "config": cfg,
            "tomorrow_pool": [],
            "pool_fresh_n": 0,
            "slots": [],
            "held_accel": {},
            "challenger_accel": {},
            "proximity": [],
            "hypothetical_swap_sell": None,
            "hypothetical_swap_buy": None,
            "breadth_zone": None,
            "breadth_zone_zh": None,
            "session_dates": ["2026-06-24"],
        }
        md = render_markdown(payload)
        self.assertIn("無 fresh mono 候選", md)
        self.assertIn("空槽", md)

    def test_champion_config_variant_id(self) -> None:
        self.assertEqual(
            champion_score_swap_c_config().variant_id,
            CHAMPION_SCORE_SWAP_C_VARIANT_ID,
        )

    @patch("rrg_mono_swap_accel_daily_brief.load_slot_state")
    @patch("rrg_mono_swap_accel_daily_brief._tomorrow_pool")
    @patch("rrg_mono_swap_accel_daily_brief._signal_rrg_panels")
    @patch("rrg_mono_swap_accel_daily_brief.load_price_panels")
    @patch("rrg_mono_swap_accel_daily_brief.load_benchmark_close")
    def test_build_payload_respects_top_n(
        self,
        mock_bench,
        mock_panels,
        mock_rrg,
        mock_pool,
        mock_state,
    ) -> None:
        import pandas as pd

        mock_panels.return_value = (pd.DataFrame(), None, None)
        mock_bench.return_value = pd.Series(dtype=float)
        mock_rrg.return_value = (
            pd.DataFrame(),
            pd.Series(dtype=float),
            pd.DataFrame(),
            pd.DataFrame(),
            ["2026-06-24"],
        )
        mock_state.return_value = {"slots": []}
        rows = [_sample_row(str(1000 + i), float(TOP_N - i)) for i in range(TOP_N + 3)]
        mock_pool.return_value = (rows[:TOP_N], len(rows))

        conn = sqlite3.connect(":memory:")
        try:
            payload = build_payload(conn, as_of="2026-06-24")
        finally:
            conn.close()

        self.assertEqual(len(payload["tomorrow_pool"]), TOP_N)
        self.assertEqual(payload["pool_fresh_n"], TOP_N + 3)


if __name__ == "__main__":
    unittest.main()
