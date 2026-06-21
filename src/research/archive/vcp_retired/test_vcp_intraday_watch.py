"""vcp_intraday_watch：盤中 watchlist 邏輯。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from stock_db import connect, upsert_vcp_screen_scores_v2
from vcp_intraday_watch import (
    ALERT_STATE_PATH,
    WatchRow,
    build_watch_rows,
    classify_intraday,
    filter_new_alerts,
    load_merged_watchlist,
    run_intraday_watch,
)


def _v2_row(**overrides) -> dict:
    d0 = overrides.pop("as_of_date", (date.today() - timedelta(days=1)).isoformat())
    base = {
        "stock_id": "6488",
        "as_of_date": d0,
        "model_id": "vcp-tm",
        "stock_name": "環球晶",
        "composite_score": 79.0,
        "rating": "Textbook VCP",
        "execution_state": "Pre-breakout",
        "entry_ready": 1,
        "pattern_type": "Textbook VCP",
        "pivot_price": 967.0,
        "distance_from_pivot_pct": 1.79,
        "stop_loss": 900.0,
        "risk_pct": 7.0,
        "valid_vcp": 1,
        "metadata_json": "{}",
    }
    base.update(overrides)
    return base


def _test_conn() -> sqlite3.Connection:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return connect(Path(tmp.name))


class TestVcpIntradayWatch(unittest.TestCase):
    def setUp(self) -> None:
        self._alert_backup: Path | None = None
        if ALERT_STATE_PATH.is_file():
            self._alert_backup = ALERT_STATE_PATH.read_bytes()

    def tearDown(self) -> None:
        if self._alert_backup is not None:
            ALERT_STATE_PATH.write_bytes(self._alert_backup)
        elif ALERT_STATE_PATH.is_file():
            ALERT_STATE_PATH.unlink()

    def test_classify_hold_vs_fade(self) -> None:
        status, dist, pb = classify_intraday(607.0, 623.0, day_high=639.0)
        self.assertEqual(status, "BREAKOUT_FADE")
        self.assertAlmostEqual(pb or 0, 2.5, places=1)

        status, _, _ = classify_intraday(607.0, 623.0, day_high=625.0)
        self.assertEqual(status, "BREAKOUT_HOLD")

        status, _, pb = classify_intraday(940.0, 954.0, day_high=990.0)
        self.assertEqual(status, "BREAKOUT_FADE")
        self.assertGreater(pb or 0, 3.0)

        status, dist, _ = classify_intraday(967.0, 1045.0, day_high=1045.0)
        self.assertEqual(status, "BREAKOUT_EXTENDED")
        self.assertGreater(dist or 0, 8.0)

    def test_classify_near_setup(self) -> None:
        self.assertEqual(classify_intraday(100.0, 101.0, day_high=101.0)[0], "BREAKOUT_HOLD")
        self.assertEqual(classify_intraday(100.0, 98.0)[0], "NEAR")
        self.assertEqual(classify_intraday(100.0, 90.0)[0], "SETUP")

    def test_load_merged_watchlist_dedupes_by_score(self) -> None:
        conn = _test_conn()
        d0 = (date.today() - timedelta(days=1)).isoformat()
        upsert_vcp_screen_scores_v2(
            conn,
            [
                _v2_row(as_of_date=d0, model_id="vcp-tm", composite_score=79.0),
                _v2_row(
                    as_of_date=d0,
                    model_id="chunge-funnel",
                    composite_score=60.0,
                    execution_state="Breakout",
                ),
            ],
        )
        rows = load_merged_watchlist(
            conn,
            d0,
            model_ids=("vcp-tm", "chunge-funnel"),
            min_score=50.0,
            execution_states=("Pre-breakout", "Breakout"),
        )
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model_id"], "vcp-tm")

    def test_load_merged_filters_execution_state(self) -> None:
        conn = _test_conn()
        d0 = (date.today() - timedelta(days=1)).isoformat()
        upsert_vcp_screen_scores_v2(
            conn,
            [
                _v2_row(as_of_date=d0, execution_state="Extended"),
                _v2_row(
                    as_of_date=d0,
                    stock_id="2330",
                    execution_state="Breakout",
                    composite_score=70.0,
                ),
            ],
        )
        rows = load_merged_watchlist(
            conn,
            d0,
            model_ids=("vcp-tm",),
            min_score=50.0,
            execution_states=("Pre-breakout", "Breakout"),
        )
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stock_id"], "2330")

    def test_build_watch_rows_fade(self) -> None:
        watchlist = [
            {
                "stock_id": "2327",
                "stock_name": "國巨",
                "vcp_score": 74.0,
                "execution_state": "Breakout",
                "pivot_price": 940.0,
                "model_id": "chunge-funnel",
                "entry_ready": False,
            }
        ]
        ticks = [{"stock_id": "2327", "open": 970.0, "high": 990.0, "close": 954.0}]
        rows = build_watch_rows(watchlist, ticks)
        self.assertEqual(rows[0].intraday_status, "BREAKOUT_FADE")

    def test_filter_new_alerts_dedupes_same_day(self) -> None:
        row = WatchRow(
            stock_id="6488",
            stock_name="環球晶",
            vcp_score=79.0,
            execution_state="Breakout",
            pivot_price=967.0,
            model_id="vcp-tm",
            price=1045.0,
            day_open=983.0,
            day_high=1045.0,
            dist_pivot_pct=8.0,
            pullback_from_high_pct=0.0,
            intraday_status="BREAKOUT_EXTENDED",
            action_hint="",
        )
        session = "2099-01-02"
        first = filter_new_alerts([row], session_date=session, alert_on=("BREAKOUT_EXTENDED",))
        second = filter_new_alerts([row], session_date=session, alert_on=("BREAKOUT_EXTENDED",))
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)

    @patch("vcp_intraday_watch.fetch_tick_snapshots")
    @patch("vcp_intraday_watch.send_watch_alerts")
    @patch("vcp_intraday_watch.write_intraday_report")
    def test_run_intraday_watch(
        self,
        mock_write: unittest.mock.MagicMock,
        mock_notify: unittest.mock.MagicMock,
        mock_tick: unittest.mock.MagicMock,
    ) -> None:
        conn = _test_conn()
        d0 = (date.today() - timedelta(days=1)).isoformat()
        upsert_vcp_screen_scores_v2(
            conn,
            [
                _v2_row(
                    as_of_date=d0,
                    stock_id="2337",
                    stock_name="旺宏",
                    composite_score=60.0,
                    execution_state="Pre-breakout",
                    pivot_price=180.0,
                ),
            ],
        )
        mock_tick.return_value = ([{"stock_id": "2337", "close": 166.0, "high": 166.5}], None)
        mock_write.return_value = Path("/tmp/vcp_intraday.md")

        path, rows, err = run_intraday_watch(
            conn,
            session_date="2099-06-16",
            model_ids=("vcp-tm",),
            send_notifications=True,
        )
        conn.close()
        self.assertIsNotNone(path)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(err)
        mock_notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
