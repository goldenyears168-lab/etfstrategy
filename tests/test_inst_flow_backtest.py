"""inst_flow_backtest · 法人連買訊號掃描。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from research.backtest.copytrade_backtest import group_signals_by_date
from research.backtest.inst_flow_backtest import (
    CONFLUENCE_SUFFIX,
    SIGNAL_PROFILES,
    apply_etf_confluence,
    confluence_action_suffix,
    confluence_profile_id,
    load_etf_add_index,
    scan_inst_flow_signals,
)
from stock_db import (
    connect,
    upsert_etf_holdings,
    upsert_etf_holdings_meta,
    upsert_stock_institutional_daily,
)


def _profile(pid: str):
    for p in SIGNAL_PROFILES:
        if p.profile_id == pid:
            return p
    raise KeyError(pid)


class TestInstFlowBacktest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _seed_inst(self, rows: list[dict]) -> None:
        synced = "2026-06-01T00:00:00+00:00"
        upsert_stock_institutional_daily(
            self.conn,
            [{**r, "source": "finmind", "synced_at": synced} for r in rows],
        )

    def test_foreign5_pos_requires_positive_today(self) -> None:
        self._seed_inst(
            [
                {"stock_id": "2330", "trade_date": f"2026-06-{d:02d}", "close_price": 900.0,
                 "foreign_net": 100.0, "investment_trust_net": 0.0,
                 "dealer_self_net": 0.0, "three_institution_net": 100.0}
                for d in range(1, 7)
            ]
        )
        # 最後一天改賣出 → 不應觸發
        self.conn.execute(
            "UPDATE stock_institutional_daily SET foreign_net = -50 WHERE stock_id='2330' AND trade_date='2026-06-06'"
        )
        signals = scan_inst_flow_signals(
            self.conn,
            profile=_profile("foreign5_pos"),
            stock_ids=["2330"],
            name_by_id={"2330": "台積電"},
        )
        dates = {s.signal_date for s in signals}
        self.assertNotIn("2026-06-06", dates)
        self.assertIn("2026-06-05", dates)

    def test_sync_buy2_needs_two_day_streak(self) -> None:
        rows = []
        for d in range(1, 5):
            rows.append(
                {
                    "stock_id": "2317",
                    "trade_date": f"2026-06-0{d}",
                    "close_price": 100.0,
                    "foreign_net": 10.0,
                    "investment_trust_net": 5.0,
                    "dealer_self_net": 0.0,
                    "three_institution_net": 15.0,
                }
            )
        self._seed_inst(rows)
        signals = scan_inst_flow_signals(
            self.conn,
            profile=_profile("sync_buy2"),
            stock_ids=["2317"],
            name_by_id={"2317": "鴻海"},
        )
        grouped = group_signals_by_date(signals)
        self.assertEqual(list(grouped.keys()), ["2026-06-02", "2026-06-03", "2026-06-04"])
        signals3 = scan_inst_flow_signals(
            self.conn,
            profile=_profile("sync_buy3"),
            stock_ids=["2317"],
            name_by_id={"2317": "鴻海"},
        )
        self.assertEqual(list(group_signals_by_date(signals3).keys()), ["2026-06-03", "2026-06-04"])

    def test_sync_buy3_needs_three_day_streak(self) -> None:
        rows = []
        for d in range(1, 5):
            f, t = (10.0, 5.0) if d >= 2 else (10.0, -1.0)
            rows.append(
                {
                    "stock_id": "2317",
                    "trade_date": f"2026-06-0{d}",
                    "close_price": 100.0,
                    "foreign_net": f,
                    "investment_trust_net": t,
                    "dealer_self_net": 0.0,
                    "three_institution_net": f + t,
                }
            )
        self._seed_inst(rows)
        signals = scan_inst_flow_signals(
            self.conn,
            profile=_profile("sync_buy3"),
            stock_ids=["2317"],
            name_by_id={"2317": "鴻海"},
        )
        grouped = group_signals_by_date(signals)
        self.assertEqual(list(grouped.keys()), ["2026-06-04"])

    def test_top_k_caps_daily_legs(self) -> None:
        rows = []
        stocks = [("2330", 500.0), ("2317", 400.0), ("2454", 300.0), ("2303", 200.0)]
        for d in range(1, 7):
            for sid, base in stocks:
                rows.append(
                    {
                        "stock_id": sid,
                        "trade_date": f"2026-06-{d:02d}",
                        "close_price": 100.0,
                        "foreign_net": base + d,
                        "investment_trust_net": 1.0,
                        "dealer_self_net": 0.0,
                        "three_institution_net": base + d + 1.0,
                    }
                )
        self._seed_inst(rows)
        signals = scan_inst_flow_signals(
            self.conn,
            profile=_profile("foreign5_pos"),
            stock_ids=[s for s, _ in stocks],
            name_by_id={s: s for s, _ in stocks},
            top_k=2,
        )
        grouped = group_signals_by_date(signals)
        self.assertIn("2026-06-06", grouped)
        self.assertEqual(len(grouped["2026-06-06"]), 2)
        # 2330 has highest foreign5 on last day
        ids = {s.stock_id for s in grouped["2026-06-06"]}
        self.assertIn("2330", ids)

    def test_rank_band_keeps_middle_ranks(self) -> None:
        rows = []
        stocks = [
            ("2330", 500.0),
            ("2317", 400.0),
            ("2454", 300.0),
            ("2303", 200.0),
            ("2881", 100.0),
        ]
        for d in range(1, 7):
            for sid, base in stocks:
                rows.append(
                    {
                        "stock_id": sid,
                        "trade_date": f"2026-06-{d:02d}",
                        "close_price": 100.0,
                        "foreign_net": base + d,
                        "investment_trust_net": 1.0,
                        "dealer_self_net": 0.0,
                        "three_institution_net": base + d + 1.0,
                    }
                )
        self._seed_inst(rows)
        signals = scan_inst_flow_signals(
            self.conn,
            profile=_profile("foreign5_pos"),
            stock_ids=[s for s, _ in stocks],
            name_by_id={s: s for s, _ in stocks},
            rank_from=2,
            rank_to=3,
        )
        grouped = group_signals_by_date(signals)
        self.assertIn("2026-06-06", grouped)
        ids = {s.stock_id for s in grouped["2026-06-06"]}
        self.assertEqual(ids, {"2317", "2454"})

    def test_etf_confluence_filters_to_add_day(self) -> None:
        synced = "2026-06-01T00:00:00+00:00"
        for d in range(1, 7):
            upsert_stock_institutional_daily(
                self.conn,
                [
                    {
                        "stock_id": "2330",
                        "trade_date": f"2026-06-{d:02d}",
                        "close_price": 900.0,
                        "foreign_net": 100.0,
                        "investment_trust_net": 10.0,
                        "dealer_self_net": 0.0,
                        "three_institution_net": 110.0,
                        "source": "finmind",
                        "synced_at": synced,
                    }
                ],
            )
        upsert_etf_holdings_meta(
            self.conn,
            {
                "etf_code": "00981A",
                "snapshot_date": "2026-06-05",
                "nav": 100.0,
                "holding_count": 1,
                "source": "test",
                "source_edit_at": None,
                "synced_at": synced,
            },
        )
        upsert_etf_holdings_meta(
            self.conn,
            {
                "etf_code": "00981A",
                "snapshot_date": "2026-06-06",
                "nav": 100.0,
                "holding_count": 2,
                "source": "test",
                "source_edit_at": None,
                "synced_at": synced,
            },
        )
        upsert_etf_holdings(
            self.conn,
            [
                {
                    "etf_code": "00981A",
                    "snapshot_date": "2026-06-05",
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "shares": 1000.0,
                    "weight_pct": 5.0,
                    "amount": None,
                    "source": "test",
                    "source_edit_at": None,
                    "synced_at": synced,
                },
                {
                    "etf_code": "00981A",
                    "snapshot_date": "2026-06-06",
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "shares": 1100.0,
                    "weight_pct": 5.0,
                    "amount": None,
                    "source": "test",
                    "source_edit_at": None,
                    "synced_at": synced,
                },
                {
                    "etf_code": "00981A",
                    "snapshot_date": "2026-06-06",
                    "stock_id": "2317",
                    "stock_name": "鴻海",
                    "shares": 500.0,
                    "weight_pct": 3.0,
                    "amount": None,
                    "source": "test",
                    "source_edit_at": None,
                    "synced_at": synced,
                },
            ],
        )
        index = load_etf_add_index(self.conn, ("00981A",))
        self.assertIn(("2026-06-06", "2317"), index)
        signals = scan_inst_flow_signals(
            self.conn,
            profile=_profile("sync_buy3"),
            stock_ids=["2330", "2317"],
            name_by_id={"2330": "台積電", "2317": "鴻海"},
        )
        conf = apply_etf_confluence(signals, index)
        conf_dates = {s.signal_date for s in conf}
        self.assertIn("2026-06-06", conf_dates)
        self.assertTrue(all(CONFLUENCE_SUFFIX in s.action for s in conf))

    def test_confluence_profile_id_single_etf(self) -> None:
        self.assertEqual(
            confluence_profile_id("sync_buy3", ("00981A",)),
            "sync_buy3+00981a",
        )
        self.assertEqual(
            confluence_action_suffix(("00981A",)),
            "+00981a",
        )
        self.assertEqual(
            confluence_profile_id("sync_buy3", ("00981A", "00982A")),
            "sync_buy3+etf",
        )


if __name__ == "__main__":
    unittest.main()
