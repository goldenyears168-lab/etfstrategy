"""Tests for c18acc snapshot_1300 helpers."""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from research.backtest.c18acc_snapshot_1300 import (
    SNAPSHOT_1300_MINUTE,
    build_snapshot_variant_config,
    collect_snapshot_stock_ids,
    preregistered_variants,
    snapshot_px_strict,
)
from research.backtest.rrg_mono_score_swap_c import _swap_px
from rrg_mono_daily_brief import ScanRow


class Snapshot1300Tests(unittest.TestCase):
    def test_preregistered_variants(self) -> None:
        variants = preregistered_variants(n_slots=3)
        self.assertEqual([v.variant_id for v in variants], ["S0", "S1", "S2", "S2b"])
        s2 = next(v for v in variants if v.variant_id == "S2")
        self.assertEqual(s2.timing_mode, "snapshot_1300")
        self.assertTrue(s2.accel_sell_bypass_on_spread_lost)

    def test_build_snapshot_variant_config(self) -> None:
        cfg = build_snapshot_variant_config(
            "Sx",
            timing_mode="snapshot_1300",
            label="test",
            accel_sell_bypass_on_spread_lost=False,
            n_slots=2,
        )
        self.assertEqual(cfg.timing_mode, "snapshot_1300")
        self.assertEqual(cfg.n_slots, 2)
        self.assertFalse(cfg.accel_sell_bypass_on_spread_lost)

    def test_collect_snapshot_stock_ids(self) -> None:
        rows = [
            ScanRow(
                stock_id="2330",
                stock_name="TSMC",
                fresh=True,
                mono=True,
                seg_last=1.0,
                disp=1.0,
                segs=[1.0],
                quadrants=["L"],
                rs_ratio=100.0,
                rs_momentum=100.0,
                daily_pct=0.0,
            )
        ]
        ids = collect_snapshot_stock_ids([{"stock_id": "2317"}], rows)
        self.assertEqual(ids, {"2317", "2330"})

    def test_snapshot_px_strict_no_fallback(self) -> None:
        conn = MagicMock()
        cache: dict = {}
        with patch(
            "research.backtest.c18acc_snapshot_1300.kbar_close_at_minute",
            return_value=None,
        ):
            self.assertIsNone(
                snapshot_px_strict(conn, "2330", "2024-06-03", kbar_cache=cache)
            )

    def test_swap_px_snapshot_1300(self) -> None:
        conn = MagicMock()
        close = pd.DataFrame({"2330": [100.0]}, index=["2024-06-03"])
        cfg = build_snapshot_variant_config(
            "S2",
            timing_mode="snapshot_1300",
            label="x",
            accel_sell_bypass_on_spread_lost=True,
        )
        with patch(
            "research.backtest.c18acc_snapshot_1300.snapshot_px_strict",
            return_value=101.5,
        ):
            px, minute = _swap_px(
                conn,
                close=close,
                stock_id="2330",
                trade_date="2024-06-03",
                config=cfg,
                kbar_cache={},
            )
        self.assertEqual(px, 101.5)
        self.assertEqual(minute, SNAPSHOT_1300_MINUTE)


if __name__ == "__main__":
    unittest.main()
