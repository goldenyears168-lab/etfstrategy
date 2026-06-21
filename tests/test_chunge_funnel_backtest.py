"""Tests for chunge_funnel_backtest and slot_backtest_summary."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from research.backtest.chunge_funnel_backtest import (
    build_chunge_candidates_calendar,
    simulate_chunge_hold7,
    simulate_chunge_pivot_stop,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.slot_backtest_summary import (
    SlotBacktestConfig,
    build_summary_payload,
    load_slot_backtest_summary,
    metrics_from_summary_payload,
    write_slot_backtest_summary,
)
from stock_db import connect, upsert_daily_bars, upsert_vcp_screen_scores_v2, upsert_stock_daily_bars


class TestSlotBacktestSummary(unittest.TestCase):
    def test_json_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "summary.json"
            cfg = SlotBacktestConfig(date_start="2026-01-01", date_end="2026-06-30")
            payload = build_summary_payload(
                track_id="vcp-pivot-gate",
                config=cfg,
                summary={"n_periods": 5, "mean_excess_pct": 1.2},
                source_module="chunge_funnel_backtest",
            )
            write_slot_backtest_summary(p, payload)
            loaded = load_slot_backtest_summary(p)
            assert loaded is not None
            metrics = metrics_from_summary_payload(loaded)
            self.assertEqual(metrics["n_periods"], 5)
            self.assertAlmostEqual(metrics["mean_excess_pct"], 1.2)


class TestChungeFunnelBacktest(unittest.TestCase):
    def test_simulate_with_screen_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            synced = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            upsert_vcp_screen_scores_v2(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "as_of_date": "2026-06-10",
                        "model_id": "vcp-funnel",
                        "stock_name": "台積電",
                        "composite_score": 72.0,
                        "rating": "VCP-adjacent",
                        "execution_state": "Pre-breakout",
                        "entry_ready": 0,
                        "pattern_type": "VCP-adjacent",
                        "pivot_price": None,
                        "distance_from_pivot_pct": None,
                        "stop_loss": None,
                        "risk_pct": None,
                        "valid_vcp": 1,
                        "metadata_json": "{}",
                    }
                ],
            )
            cands = build_chunge_candidates_calendar(
                conn, ["2026-06-10"], min_composite=45.0
            )
            self.assertEqual(len(cands["2026-06-10"]), 1)
            self.assertEqual(cands["2026-06-10"][0].stock_id, "2330")
            conn.close()

    def test_entry_ready_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            upsert_vcp_screen_scores_v2(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "as_of_date": "2026-06-10",
                        "model_id": "vcp-funnel",
                        "stock_name": "台積電",
                        "composite_score": 72.0,
                        "rating": "VCP-adjacent",
                        "execution_state": "Pre-breakout",
                        "entry_ready": 1,
                        "pattern_type": "VCP-adjacent",
                        "pivot_price": None,
                        "distance_from_pivot_pct": None,
                        "stop_loss": None,
                        "risk_pct": None,
                        "valid_vcp": 1,
                        "metadata_json": "{}",
                    },
                    {
                        "stock_id": "2317",
                        "as_of_date": "2026-06-10",
                        "model_id": "vcp-funnel",
                        "stock_name": "鴻海",
                        "composite_score": 80.0,
                        "rating": "VCP-adjacent",
                        "execution_state": "Pre-breakout",
                        "entry_ready": 0,
                        "pattern_type": "VCP-adjacent",
                        "pivot_price": None,
                        "distance_from_pivot_pct": None,
                        "stop_loss": None,
                        "risk_pct": None,
                        "valid_vcp": 1,
                        "metadata_json": "{}",
                    },
                ],
            )
            all_cands = build_chunge_candidates_calendar(
                conn, ["2026-06-10"], min_composite=45.0
            )
            ready_cands = build_chunge_candidates_calendar(
                conn,
                ["2026-06-10"],
                min_composite=45.0,
                entry_ready_only=True,
            )
            self.assertEqual(len(all_cands["2026-06-10"]), 2)
            self.assertEqual(len(ready_cands["2026-06-10"]), 1)
            self.assertEqual(ready_cands["2026-06-10"][0].stock_id, "2330")
            conn.close()

    def test_pivot_stop_breakout_and_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            synced = "2026-06-01T00:00:00+00:00"
            dates = [
                "2026-06-08",
                "2026-06-09",
                "2026-06-10",
                "2026-06-11",
                "2026-06-12",
            ]
            upsert_stock_daily_bars(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "trade_date": d,
                        "open": op,
                        "high": hi,
                        "low": lo,
                        "close": cl,
                        "volume": 1000,
                        "source": "finmind",
                        "synced_at": synced,
                    }
                    for d, op, hi, lo, cl in [
                        ("2026-06-08", 95.0, 96.0, 94.0, 95.5),
                        ("2026-06-09", 96.0, 97.0, 90.0, 96.5),
                        ("2026-06-10", 97.0, 99.0, 96.0, 98.0),
                        ("2026-06-11", 99.0, 101.0, 98.0, 100.0),
                        ("2026-06-12", 100.0, 100.5, 88.0, 89.0),
                    ]
                ],
            )
            upsert_daily_bars(
                conn,
                [
                    {
                        "code": "IX0001",
                        "date": d,
                        "open": 100.0,
                        "high": 100.0,
                        "low": 100.0,
                        "close": 100.0,
                        "volume": 0,
                        "spread": 0.0,
                        "source": "tej",
                        "synced_at": synced,
                    }
                    for d in dates
                ],
            )
            upsert_vcp_screen_scores_v2(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "as_of_date": "2026-06-10",
                        "model_id": "vcp-funnel",
                        "stock_name": "台積電",
                        "composite_score": 72.0,
                        "rating": "VCP-adjacent",
                        "execution_state": "Pre-breakout",
                        "entry_ready": 1,
                        "pattern_type": "VCP-adjacent",
                        "pivot_price": 100.0,
                        "distance_from_pivot_pct": -2.0,
                        "stop_loss": None,
                        "risk_pct": None,
                        "valid_vcp": 1,
                        "metadata_json": "{}",
                    }
                ],
            )
            close, _, _ = load_price_panels(conn)
            full_dates = close.index.astype(str).tolist()
            cands = build_chunge_candidates_calendar(
                conn,
                ["2026-06-10"],
                min_composite=45.0,
                entry_ready_only=True,
            )
            periods, summary = simulate_chunge_pivot_stop(
                conn,
                trade_dates=["2026-06-10", "2026-06-11", "2026-06-12"],
                full_dates=full_dates,
                close=close,
                candidates_by_date=cands,
                n_slots=1,
                hold_days=20,
                max_entry_wait_days=5,
                stop_lookback_days=5,
            )
            self.assertEqual(summary["n_periods"], 1)
            trade = periods[0]
            self.assertEqual(trade["entry_date"], "2026-06-11")
            self.assertEqual(trade["exit_date"], "2026-06-12")
            self.assertEqual(trade["exit_reason"], "stop")
            self.assertAlmostEqual(trade["entry_px"], 100.0)
            self.assertAlmostEqual(trade["exit_px"], 89.1, places=1)
            conn.close()


class TestTrackEvaluationSlot(unittest.TestCase):
    def test_load_slot_from_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "rrg.json"
            summary.write_text(
                json.dumps(
                    {
                        "track_id": "rrg-mono-hold7",
                        "generated_at": "2026-06-20",
                        "summary": {
                            "n_periods": 10,
                            "win_rate_vs_bench_pct": 60.0,
                            "mean_excess_pct": 0.5,
                        },
                    }
                ),
                encoding="utf-8",
            )
            from research.backtest.slot_backtest_summary import (
                load_slot_backtest_summary,
                metrics_from_summary_payload,
            )

            data = load_slot_backtest_summary(summary)
            assert data is not None
            metrics = metrics_from_summary_payload(data)
            self.assertEqual(metrics["n_periods"], 10)
            self.assertEqual(metrics["win_rate_vs_bench_pct"], 60.0)


if __name__ == "__main__":
    unittest.main()
