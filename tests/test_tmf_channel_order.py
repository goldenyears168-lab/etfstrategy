"""Unit tests · TMF futopt order builder + config fail-closed (no broker)."""

from __future__ import annotations

import os
import unittest
from unittest import mock

import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from order.fubon_futopt_orders import FutOptResolvedOrder, build_futopt_order, market_type_for_hhmm
from order.tmf_channel_config import TmfChannelOrderConfig, load_tmf_channel_order_config
from order.tmf_channel_ledger import roll_day, save_ledger, trading_day_str
from order.tmf_channel_marketdata import in_tmf_trade_window
from order.tmf_channel_order import reconcile_once

_TZ = ZoneInfo("Asia/Taipei")


class FutOptBuilderTest(unittest.TestCase):
    def test_market_type_day_night(self):
        from fubon_neo.constant import FutOptMarketType

        self.assertEqual(market_type_for_hhmm("09:30"), FutOptMarketType.Future)
        self.assertEqual(market_type_for_hhmm("15:05"), FutOptMarketType.FutureNight)
        self.assertEqual(market_type_for_hhmm("02:00"), FutOptMarketType.FutureNight)

    def test_build_limit_buy(self):
        from fubon_neo.constant import BSAction, FutOptPriceType, TimeInForce

        o = build_futopt_order(
            FutOptResolvedOrder(
                symbol="TMFH6",
                buy_sell="Buy",
                lot=1,
                price=20000.0,
                price_type="limit",
                time_in_force="rod",
                order_type="auto",
                market_type="future",
                user_def="tmfch",
            )
        )
        self.assertEqual(o.buy_sell, BSAction.Buy)
        self.assertEqual(o.price_type, FutOptPriceType.Limit)
        self.assertEqual(o.time_in_force, TimeInForce.ROD)
        self.assertEqual(o.lot, 1)
        self.assertEqual(o.symbol, "TMFH6")


class TmfConfigFailClosedTest(unittest.TestCase):
    def test_defaults_dry_and_not_live(self):
        env = {
            "ORDER_MASTER_ENABLED": "0",
            "ORDER_TMF_CHANNEL_ENABLED": "0",
            "ORDER_TMF_CHANNEL_AUTO_SUBMIT": "0",
            "ORDER_TMF_CHANNEL_DRY_RUN": "1",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_tmf_channel_order_config()
        self.assertTrue(cfg.dry_run)
        self.assertFalse(cfg.auto_submit)
        self.assertFalse(cfg.order_enabled)

    def test_master_off_forces_dry_even_if_flags_on(self):
        env = {
            "ORDER_MASTER_ENABLED": "0",
            "ORDER_TMF_CHANNEL_ENABLED": "1",
            "ORDER_TMF_CHANNEL_AUTO_SUBMIT": "1",
            "ORDER_TMF_CHANNEL_DRY_RUN": "0",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_tmf_channel_order_config()
        self.assertTrue(cfg.dry_run)
        self.assertFalse(cfg.auto_submit)


class SessionWindowTest(unittest.TestCase):
    def test_windows(self):
        self.assertTrue(in_tmf_trade_window("08:45"))
        self.assertTrue(in_tmf_trade_window("13:45"))
        self.assertFalse(in_tmf_trade_window("14:00"))
        self.assertTrue(in_tmf_trade_window("15:00"))
        self.assertTrue(in_tmf_trade_window("01:00"))


class TradingDayBoundaryTest(unittest.TestCase):
    """Ledger day-key must roll at the session boundary (~05:00), not
    calendar midnight — else a tripped day-loss kill silently re-arms at
    00:00 while the same overnight session (15:00->05:00) is still live."""

    def test_early_morning_belongs_to_previous_evening(self):
        now = datetime(2026, 8, 6, 2, 30, tzinfo=_TZ)  # 02:30 continuation of Aug-5 night
        self.assertEqual(trading_day_str(now), "2026-08-05")

    def test_after_five_am_is_new_trading_day(self):
        now = datetime(2026, 8, 6, 6, 0, tzinfo=_TZ)
        self.assertEqual(trading_day_str(now), "2026-08-06")

    def test_evening_session_matches_calendar_date(self):
        now = datetime(2026, 8, 5, 22, 0, tzinfo=_TZ)
        self.assertEqual(trading_day_str(now), "2026-08-05")

    def test_kill_flag_survives_midnight_rollover(self):
        ledger = {
            "schema": "tmf-channel-ledger-v1",
            "day": "2026-08-05",
            "api_calls_day": 50,
            "day_pnl_pts": -450.0,
            "killed": True,
            "kill_reason": "day_pnl_pts=-450.0<=-400.0",
            "broker_pos": {"s": "S", "n": 1, "ep": 44000.0},
        }
        fixed_now = datetime(2026, 8, 6, 1, 0, tzinfo=_TZ)  # 01:00 — still Aug-5's overnight session
        with mock.patch("order.tmf_channel_ledger.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            still_alive = roll_day(dict(ledger))
            self.assertTrue(still_alive["killed"])
            self.assertEqual(still_alive["day"], "2026-08-05")

        past_session_end = datetime(2026, 8, 6, 6, 0, tzinfo=_TZ)  # 06:00 — new trading day
        with mock.patch("order.tmf_channel_ledger.datetime") as mock_dt:
            mock_dt.now.return_value = past_session_end
            rolled = roll_day(dict(ledger))
            self.assertFalse(rolled["killed"])
            self.assertEqual(rolled["day"], "2026-08-06")


def _dry_cfg(ledger_path: str) -> TmfChannelOrderConfig:
    return TmfChannelOrderConfig(
        strategy_id="tmf-micro-channel",
        order_enabled=False,
        auto_submit=False,
        dry_run=True,
        max_lots=1,
        place_every=5,
        rail_match_pts=2.0,
        max_api_per_poll=8,
        max_api_per_day=120,
        user_def="tmfch",
        ledger_path=ledger_path,
        product="TMF",
        kill_day_loss_pts=400.0,
        recipe={},
    )


class KillSwitchFlattenStopgapTest(unittest.TestCase):
    """TMF has no broker-side stop orders — every exit only fires because
    reconcile_once runs. Once killed, the old behavior froze entirely
    (skipped querying/flattening), leaving any open position naked until the
    next trading day. This locks in the 2026-08-05 stopgap: one flatten
    attempt per poll while killed, guarded by the existing dry_run fail-closed
    chain so it can never submit a real order on its own."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.ledger_path = self.tmp.name
        killed_ledger = {
            "schema": "tmf-channel-ledger-v1",
            "day": trading_day_str(),
            "api_calls_day": 10,
            "day_pnl_pts": -450.0,
            "killed": True,
            "kill_reason": "day_pnl_pts=-450.0<=-400.0",
            "last_symbol": None,
            "last_desired": None,
            "actions_tail": [],
            "broker_pos": None,
        }
        save_ledger(self.ledger_path, killed_ledger)

    def tearDown(self):
        Path(self.ledger_path).unlink(missing_ok=True)

    def test_flattens_naked_position_while_killed(self):
        cfg = _dry_cfg(self.ledger_path)
        fake_broker_pos = {"s": "L", "n": 1, "ep": 22000.0, "acct_symbol": "TMFH6"}
        with (
            mock.patch("order.tmf_channel_order.connect_fubon", return_value=object()),
            mock.patch("order.tmf_channel_order.pick_futopt_account", return_value=object()),
            mock.patch(
                "order.tmf_channel_order.resolve_front_symbol",
                return_value=("TMFH6", "微型臺指期貨086", "2026-08-19"),
            ),
            mock.patch(
                "order.tmf_channel_order.query_tmf_broker_net", return_value=fake_broker_pos
            ),
            mock.patch("order.tmf_channel_order.place_futopt_order") as mock_place,
        ):
            out = reconcile_once(cfg, force=True)

        self.assertTrue(out["reason"].startswith("killed:"))
        self.assertEqual(mock_place.call_count, 1)
        _session, resolved = mock_place.call_args[0]
        self.assertEqual(resolved.buy_sell, "Sell")  # close a long by selling
        self.assertEqual(resolved.lot, 1)
        self.assertEqual(resolved.price_type, "market")
        self.assertEqual(mock_place.call_args.kwargs.get("dry_run"), True)
        self.assertEqual(out["kill_flatten_action"]["why"], "kill_switch_flatten")
        self.assertTrue(out["kill_flatten_action"]["ok"])

    def test_no_flatten_action_when_already_flat(self):
        cfg = _dry_cfg(self.ledger_path)
        with (
            mock.patch("order.tmf_channel_order.connect_fubon", return_value=object()),
            mock.patch("order.tmf_channel_order.pick_futopt_account", return_value=object()),
            mock.patch(
                "order.tmf_channel_order.resolve_front_symbol",
                return_value=("TMFH6", "微型臺指期貨086", "2026-08-19"),
            ),
            mock.patch("order.tmf_channel_order.query_tmf_broker_net", return_value=None),
            mock.patch("order.tmf_channel_order.place_futopt_order") as mock_place,
        ):
            out = reconcile_once(cfg, force=True)

        self.assertTrue(out["reason"].startswith("killed:"))
        self.assertEqual(mock_place.call_count, 0)
        self.assertNotIn("kill_flatten_action", out)

    def test_flatten_query_error_does_not_crash_poll(self):
        cfg = _dry_cfg(self.ledger_path)
        with mock.patch(
            "order.tmf_channel_order.connect_fubon", side_effect=RuntimeError("no network")
        ):
            out = reconcile_once(cfg, force=True)

        self.assertTrue(out["reason"].startswith("killed:"))
        self.assertIn("no network", out.get("kill_flatten_error", ""))


if __name__ == "__main__":
    unittest.main()
