from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mutual_fund_disclosure_watch import (
    format_new_disclosure_body,
    maybe_send_new_disclosure_alert,
    run_disclosure_watch,
    watch_fund,
)
from stock_db import connect, upsert_mutual_fund_holdings, upsert_mutual_fund_holdings_meta
from sync_mutual_fund_holdings import ALLIANZ_TW_TECH, DISCLOSURE_MONTHLY


class MutualFundDisclosureWatchTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self._db_path = Path(tmp.name)
        self.conn = connect(self._db_path)

    def tearDown(self) -> None:
        self.conn.close()
        self._db_path.unlink(missing_ok=True)

    def _seed_monthly(self, snapshot_date: str) -> None:
        upsert_mutual_fund_holdings_meta(
            self.conn,
            {
                "fund_code": ALLIANZ_TW_TECH.fund_code,
                "snapshot_date": snapshot_date,
                "fund_name": ALLIANZ_TW_TECH.fund_name,
                "disclosure_type": DISCLOSURE_MONTHLY,
                "fund_size_billion": 100.0,
                "holding_count": 1,
                "source": "moneydj_wr04",
                "source_edit_at": snapshot_date,
            },
        )
        upsert_mutual_fund_holdings(
            self.conn,
            [
                {
                    "fund_code": ALLIANZ_TW_TECH.fund_code,
                    "snapshot_date": snapshot_date,
                    "disclosure_type": DISCLOSURE_MONTHLY,
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "rank_no": 1,
                    "shares": None,
                    "weight_pct": 7.0,
                    "amount": None,
                    "asset_type": "國內上市",
                    "source": "moneydj_wr04",
                    "source_edit_at": snapshot_date,
                }
            ],
        )

    @patch("mutual_fund_disclosure_watch.fetch_moneydj_holdings")
    def test_unchanged_when_remote_not_newer(self, mock_fetch) -> None:
        self._seed_monthly("2026-05-31")
        mock_fetch.return_value = (
            "2026-05-31",
            [{"stock_id": "2330", "stock_name": "台積電", "weight_pct": 7.0}],
            100.0,
        )

        result = run_disclosure_watch(self.conn, ALLIANZ_TW_TECH, sync_on_new=False)

        self.assertEqual(result.status, "unchanged")
        self.assertEqual(result.remote_latest, "2026-05-31")

    @patch("mutual_fund_disclosure_watch.sync_latest_moneydj")
    @patch("mutual_fund_disclosure_watch.fetch_moneydj_holdings")
    def test_new_snapshot_triggers_sync(self, mock_fetch, mock_sync) -> None:
        self._seed_monthly("2026-05-31")
        mock_fetch.return_value = (
            "2026-06-30",
            [
                {"stock_id": "6223", "stock_name": "旺矽", "weight_pct": 8.0},
                {"stock_id": "2330", "stock_name": "台積電", "weight_pct": 7.0},
            ],
            120.0,
        )
        mock_sync.return_value = 10

        result = run_disclosure_watch(self.conn, ALLIANZ_TW_TECH, sync_on_new=True)

        self.assertEqual(result.status, "new")
        self.assertEqual(result.remote_latest, "2026-06-30")
        self.assertEqual(result.holdings_written, 10)
        mock_sync.assert_called_once()

    @patch("mutual_fund_disclosure_watch.send_alert")
    def test_email_only_on_new(self, mock_send) -> None:
        from mutual_fund_disclosure_watch import DisclosureWatchResult

        unchanged = DisclosureWatchResult(
            status="unchanged",
            fund_code="ACDD04",
            fund_name=ALLIANZ_TW_TECH.fund_name,
            db_latest="2026-05-31",
            remote_latest="2026-05-31",
        )
        with patch.dict(os.environ, {"RUN_MUTUAL_FUND_DISCLOSURE_EMAIL": "1"}):
            self.assertFalse(maybe_send_new_disclosure_alert(unchanged))
            mock_send.assert_not_called()

            new = DisclosureWatchResult(
                status="new",
                fund_code="ACDD04",
                fund_name=ALLIANZ_TW_TECH.fund_name,
                db_latest="2026-05-31",
                remote_latest="2026-06-30",
                remote_source="moneydj_wr04",
                holdings_written=10,
                top_holdings=(("6223", "旺矽", 8.0),),
            )
            self.assertTrue(maybe_send_new_disclosure_alert(new))
            mock_send.assert_called_once()
            subject, body = mock_send.call_args[0]
            self.assertIn("2026-06-30", subject)
            self.assertIn("旺矽", body)

    def test_format_new_disclosure_body(self) -> None:
        from mutual_fund_disclosure_watch import DisclosureWatchResult

        body = format_new_disclosure_body(
            DisclosureWatchResult(
                status="new",
                fund_code="ACDD04",
                fund_name=ALLIANZ_TW_TECH.fund_name,
                db_latest="2026-05-31",
                remote_latest="2026-06-30",
                remote_source="moneydj_wr04",
                holdings_written=10,
                top_holdings=(("2330", "台積電", 7.05),),
            )
        )
        self.assertIn("月前十大持股已公布", body)
        self.assertIn("2330 台積電 7.05%", body)

    @patch("mutual_fund_disclosure_watch.maybe_send_new_disclosure_alert")
    @patch("mutual_fund_disclosure_watch.run_disclosure_watch")
    @patch("mutual_fund_disclosure_watch.connect")
    def test_watch_fund_respects_notify_flag(self, mock_connect, mock_run, mock_notify) -> None:
        from mutual_fund_disclosure_watch import DisclosureWatchResult

        mock_connect.return_value = self.conn
        mock_run.return_value = DisclosureWatchResult(
            status="unchanged",
            fund_code="ACDD04",
            fund_name=ALLIANZ_TW_TECH.fund_name,
        )

        watch_fund(notify=False)
        mock_notify.assert_not_called()

        watch_fund(notify=True)
        mock_notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
