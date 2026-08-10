"""Unit tests · TMF futopt order builder + config fail-closed (no broker)."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from order.fubon_futopt_orders import FutOptResolvedOrder, build_futopt_order, market_type_for_hhmm
from order.tmf_channel_config import TmfChannelOrderConfig, load_tmf_channel_order_config
from order.tmf_channel_ledger import record_actions, roll_day, save_ledger, trading_day_str
from order.tmf_channel_marketdata import in_tmf_trade_window
from order.tmf_channel_order import (
    _drop_forming_last_bar,
    check_max_hold_safety_net,
    reconcile_once,
    synthesize_lost_tracking_protect_rail,
)

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
    """weekday explicitly pinned (Monday=0..Sunday=6, date.weekday() convention)
    so these stay deterministic regardless of what day the suite actually runs."""

    _MON, _TUE, _FRI, _SAT, _SUN = 0, 1, 4, 5, 6

    def test_windows_on_a_weekday(self):
        self.assertTrue(in_tmf_trade_window("08:45", weekday=self._TUE))
        self.assertTrue(in_tmf_trade_window("13:45", weekday=self._TUE))
        self.assertFalse(in_tmf_trade_window("14:00", weekday=self._TUE))
        self.assertTrue(in_tmf_trade_window("15:00", weekday=self._TUE))
        self.assertTrue(in_tmf_trade_window("01:00", weekday=self._TUE))

    def test_no_day_session_on_saturday_or_sunday(self):
        for wd in (self._SAT, self._SUN):
            self.assertFalse(in_tmf_trade_window("09:00", weekday=wd))
            self.assertFalse(in_tmf_trade_window("13:00", weekday=wd))

    def test_no_new_night_session_opens_saturday_or_sunday_evening(self):
        for wd in (self._SAT, self._SUN):
            self.assertFalse(in_tmf_trade_window("15:00", weekday=wd))
            self.assertFalse(in_tmf_trade_window("22:00", weekday=wd))

    def test_fridays_night_session_tail_continues_into_saturday(self):
        """The live 2026-08-08 incident: Friday 15:00's night session
        legitimately runs through Saturday 00:00-05:00 — this must stay
        True, only the fake "Saturday day session" (08:45+) was the bug."""
        self.assertTrue(in_tmf_trade_window("01:00", weekday=self._SAT))
        self.assertTrue(in_tmf_trade_window("04:59", weekday=self._SAT))

    def test_no_session_tail_continues_into_sunday_or_monday(self):
        """Saturday has no night session, so Sunday's 00:00-05:00 tail is
        not a continuation of anything real; Sunday likewise has no night
        session, so Monday's tail is not real either."""
        self.assertFalse(in_tmf_trade_window("01:00", weekday=self._SUN))
        self.assertFalse(in_tmf_trade_window("01:00", weekday=self._MON))

    def test_monday_day_session_and_night_open_are_real(self):
        self.assertTrue(in_tmf_trade_window("08:45", weekday=self._MON))
        self.assertTrue(in_tmf_trade_window("15:00", weekday=self._MON))

    def test_defaults_to_real_current_weekday_when_not_given(self):
        # No explicit weekday: must resolve via datetime.now(), not crash.
        result = in_tmf_trade_window("10:00")
        self.assertIsInstance(result, bool)


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


class DropFormingLastBarTest(unittest.TestCase):
    """Found live 2026-08-08: Fubon's candle feed returns a live-updating bar
    for the in-progress minute, whose H/L/C/V (and PV8 regime classification)
    change across successive ~20s polls of the same minute — a live/backtest
    mismatch that made the reconciler cancel+replace the same resting order
    every poll (~28 place + ~26 cancel/hour). desired_from_simulate() must
    drop that still-forming bar before calling simulate()."""

    def _bars(self, *timestamps: str) -> list[dict]:
        return [
            dict(t=t, o=100.0, h=101.0, l=99.0, c=100.5, v=10.0) for t in timestamps
        ]

    def test_forming_bar_dropped_when_now_within_its_minute(self):
        bars = self._bars("2026-08-08T03:25:00.000+08:00")
        fixed_now = datetime(2026, 8, 8, 3, 25, 37, tzinfo=_TZ)  # 37s into the same minute
        with mock.patch("order.tmf_channel_order.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat = datetime.fromisoformat
            self.assertEqual(_drop_forming_last_bar(bars), [])

    def test_closed_bar_kept_once_its_minute_has_elapsed(self):
        bars = self._bars("2026-08-08T03:24:00.000+08:00")
        fixed_now = datetime(2026, 8, 8, 3, 25, 1, tzinfo=_TZ)  # 1s past bar close
        with mock.patch("order.tmf_channel_order.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat = datetime.fromisoformat
            self.assertEqual(len(_drop_forming_last_bar(bars)), 1)

    def test_only_last_bar_is_dropped_earlier_history_kept(self):
        bars = self._bars(
            "2026-08-08T03:23:00.000+08:00",
            "2026-08-08T03:24:00.000+08:00",
            "2026-08-08T03:25:00.000+08:00",
        )
        fixed_now = datetime(2026, 8, 8, 3, 25, 10, tzinfo=_TZ)
        with mock.patch("order.tmf_channel_order.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat = datetime.fromisoformat
            out = _drop_forming_last_bar(bars)
            self.assertEqual([b["t"] for b in out], [bars[0]["t"], bars[1]["t"]])

    def test_empty_bars_returns_empty(self):
        self.assertEqual(_drop_forming_last_bar([]), [])

    def test_missing_timestamp_fails_safe_unchanged(self):
        bars = [dict(o=100.0, h=101.0, l=99.0, c=100.5, v=10.0)]
        self.assertEqual(_drop_forming_last_bar(bars), bars)

    def test_desired_from_simulate_reports_bars_lt_20_after_truncation(self):
        from order.tmf_channel_order import desired_from_simulate

        bars = self._bars(*[f"2026-08-08T03:{i:02d}:00.000+08:00" for i in range(20)])
        fixed_now = datetime(2026, 8, 8, 3, 19, 5, tzinfo=_TZ)  # last bar still forming
        with mock.patch("order.tmf_channel_order.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat = datetime.fromisoformat
            out = desired_from_simulate(bars, day="2026-08-08", recipe={})
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "bars_lt_20")


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
        max_hold_safety_min=90.0,
        kill_consecutive_failures=5,
        recipe={},
        recipe_version="test",
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
        # Never write production data/order/tmf_channel_broadcast.json from unit tests.
        self._emit_patch = mock.patch(
            "order.tmf_channel_broadcast.emit_from_summary",
            side_effect=lambda *a, **k: {"schema": "tmf-channel-broadcast-v1", "test": True},
        )
        self._emit_patch.start()

    def tearDown(self):
        self._emit_patch.stop()
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


class ForceCliGateTest(unittest.TestCase):
    def test_cli_force_refused_without_env(self):
        import os
        from order.tmf_channel_order import main

        os.environ.pop("ORDER_TMF_CHANNEL_FORCE_OK", None)
        rc = main(["--force", "--json"])
        self.assertEqual(rc, 2)

    def test_cli_force_allowed_with_env(self):
        import os
        from order import tmf_channel_order as mod

        os.environ["ORDER_TMF_CHANNEL_FORCE_OK"] = "1"
        self.addCleanup(lambda: os.environ.pop("ORDER_TMF_CHANNEL_FORCE_OK", None))
        with mock.patch.object(mod, "reconcile_once", return_value={"ok": True, "reason": "outside_session"}):
            rc = mod.main(["--force", "--json"])
        self.assertEqual(rc, 0)


class OneLotScaleBlockTest(unittest.TestCase):
    """max_lots=1 must drop same-side want so resting scale cannot fill to n=2."""

    def test_blocks_same_side_short_keeps_protect_long(self):
        from order.tmf_channel_order import block_same_side_scale_wants

        ws, wl, why = block_same_side_scale_wants(
            44844.0, 44696.0, open_pos={"s": "S", "n": 1}, max_lots=1
        )
        self.assertIsNone(ws)
        self.assertEqual(wl, 44696.0)
        self.assertIn("side=S", why or "")

    def test_blocks_same_side_long_keeps_protect_short(self):
        from order.tmf_channel_order import block_same_side_scale_wants

        ws, wl, why = block_same_side_scale_wants(
            44515.0, 44415.0, open_pos={"s": "L", "n": 1}, max_lots=1
        )
        self.assertEqual(ws, 44515.0)
        self.assertIsNone(wl)
        self.assertIn("side=L", why or "")

    def test_flat_keeps_dual_hang(self):
        from order.tmf_channel_order import block_same_side_scale_wants

        ws, wl, why = block_same_side_scale_wants(
            100.0, 90.0, open_pos=None, max_lots=1
        )
        self.assertEqual(ws, 100.0)
        self.assertEqual(wl, 90.0)
        self.assertIsNone(why)

    def test_config_hard_locks_max_lots_to_one(self):
        env = {
            "ORDER_MASTER_ENABLED": "0",
            "ORDER_TMF_CHANNEL_MAX_LOTS": "2",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_tmf_channel_order_config()
        self.assertEqual(cfg.max_lots, 1)
        self.assertEqual(cfg.recipe.get("max_lots"), 1)


class LostTrackingProtectRailTest(unittest.TestCase):
    """2026-08-10: simulate()'s own max_hold_bars had already closed its
    internal copy of a still-real broker fill, so want_s/want_l came back
    None every poll and the dashboard's "S 掛帶 O+16~30" text was purely
    decorative -- no order was actually resting there. This rebuilds a
    protective rail off the real broker entry once tracking is lost."""

    def test_long_position_gets_sell_protect_above_entry(self):
        ws, wl, synthesized = synthesize_lost_tracking_protect_rail(
            None, None,
            broker_live={"s": "L", "n": 1, "ep": 44990.0},
            active_cell_recipe={"hang_lo": 16.0, "hang_hi": 30.0},
            fallback_recipe={},
        )
        self.assertEqual(ws, 45020.0)
        self.assertIsNone(wl)
        self.assertTrue(synthesized)

    def test_short_position_gets_buy_protect_below_entry(self):
        ws, wl, synthesized = synthesize_lost_tracking_protect_rail(
            None, None,
            broker_live={"s": "S", "n": 1, "ep": 45000.0},
            active_cell_recipe={"hang_lo": 16.0, "hang_hi": 30.0},
            fallback_recipe={},
        )
        self.assertIsNone(ws)
        self.assertEqual(wl, 44970.0)
        self.assertTrue(synthesized)

    def test_missing_cell_recipe_falls_back_to_base_recipe_hang_hi(self):
        ws, _wl, synthesized = synthesize_lost_tracking_protect_rail(
            None, None,
            broker_live={"s": "L", "n": 1, "ep": 44990.0},
            active_cell_recipe=None,
            fallback_recipe={"hang_hi": 50.0},
        )
        self.assertEqual(ws, 45040.0)
        self.assertTrue(synthesized)

    def test_real_tracked_want_is_never_overridden(self):
        ws, wl, synthesized = synthesize_lost_tracking_protect_rail(
            45020.0, None,
            broker_live={"s": "L", "n": 1, "ep": 44990.0},
            active_cell_recipe={"hang_lo": 16.0, "hang_hi": 30.0},
            fallback_recipe={},
        )
        self.assertEqual(ws, 45020.0)
        self.assertIsNone(wl)
        self.assertFalse(synthesized)

    def test_no_broker_position_leaves_wants_untouched(self):
        ws, wl, synthesized = synthesize_lost_tracking_protect_rail(
            None, None, broker_live=None, active_cell_recipe=None, fallback_recipe={},
        )
        self.assertIsNone(ws)
        self.assertIsNone(wl)
        self.assertFalse(synthesized)

    def test_reconcile_once_places_synthesized_protect_when_tracking_lost(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        ledger_path = tmp.name
        try:
            save_ledger(ledger_path, {
                "schema": "tmf-channel-ledger-v1",
                "day": trading_day_str(),
                "api_calls_day": 0,
                "day_pnl_pts": 0.0,
                "killed": False,
                "kill_reason": None,
                "last_symbol": None,
                "last_desired": None,
                "actions_tail": [],
                "broker_pos": None,
                "consecutive_order_failures": 0,
            })
            cfg = _dry_cfg(ledger_path)
            fake_desired = {
                "ok": True,
                "want_s": None,
                "want_l": None,
                "open_pos": None,
                "trades": [],
                "events": [],
                "spot": 45100.0,
                "last_t": "2026-08-10T16:40:00.000+08:00",
                "regime": "div_hh_weak_vol",
                "active_cell": {
                    "cell": "night|div_hh_weak_vol", "session": "night",
                    "pv": "div_hh_weak_vol",
                    "recipe": {"hang_lo": 18.0, "hang_hi": 32.0},
                },
                "nq_gate": "L",
                "nq_gate_error": None,
                "recipe_version": "test",
            }
            with (
                mock.patch("order.tmf_channel_order.connect_fubon", return_value=object()),
                mock.patch("order.tmf_channel_order.pick_futopt_account", return_value=object()),
                mock.patch(
                    "order.tmf_channel_order.resolve_front_symbol",
                    return_value=("TMFH6", "微型臺指期貨086", "2026-08-19"),
                ),
                mock.patch("order.tmf_channel_order.fetch_1m_bars", return_value=[{"t": "x"}] * 25),
                mock.patch("order.tmf_channel_order.desired_from_simulate", return_value=fake_desired),
                mock.patch(
                    "order.tmf_channel_order.query_tmf_broker_net",
                    return_value={"s": "L", "n": 1, "ep": 44990.0, "acct_symbol": "FITM"},
                ),
                mock.patch("order.tmf_channel_order.get_futopt_order_results", return_value=[]),
                mock.patch("order.tmf_channel_order.place_futopt_order") as mock_place,
                mock.patch(
                    "order.tmf_channel_broadcast.emit_from_summary",
                    side_effect=lambda *a, **k: {"schema": "tmf-channel-broadcast-v1", "test": True},
                ),
            ):
                out = reconcile_once(cfg, force=True)

            self.assertTrue(out.get("protect_rail_synthesized"))
            self.assertEqual(out.get("want_s"), 45022.0)
            self.assertIsNone(out.get("want_l"))
            self.assertEqual(mock_place.call_count, 1)
            _session, resolved = mock_place.call_args[0]
            self.assertEqual(resolved.buy_sell, "Sell")
            self.assertEqual(resolved.price, 45022.0)
        finally:
            Path(ledger_path).unlink(missing_ok=True)

    def test_reconcile_once_still_synthesizes_after_same_side_want_leaks_past_block(self):
        """2026-08-10 live: the deployed synthesis fix never actually fired
        because it ran BEFORE block_same_side_scale_wants -- a same-side
        want_l leaked past a fully-blocked cell (block=["L","S"]) read as
        non-None right where the synthesis check looked, so it declined to
        act; only after the max-lots same-side lock stripped that leaked
        want a few lines later did both sides actually go None. Moved after
        block_same_side_scale_wants so this exact sequence still synthesizes."""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        ledger_path = tmp.name
        try:
            save_ledger(ledger_path, {
                "schema": "tmf-channel-ledger-v1",
                "day": trading_day_str(),
                "api_calls_day": 0,
                "day_pnl_pts": 0.0,
                "killed": False,
                "kill_reason": None,
                "last_symbol": None,
                "last_desired": None,
                "actions_tail": [],
                "broker_pos": None,
                "consecutive_order_failures": 0,
            })
            cfg = _dry_cfg(ledger_path)
            fake_desired = {
                "ok": True,
                "want_s": None,
                "want_l": 45038.0,  # leaked same-side want despite block=["L","S"]
                "open_pos": None,
                "trades": [],
                "events": [],
                "spot": 45092.0,
                "last_t": "2026-08-10T17:30:00.000+08:00",
                "regime": "div_hh_weak_vol",
                "active_cell": {
                    "cell": "night|div_hh_weak_vol", "session": "night",
                    "pv": "div_hh_weak_vol",
                    "recipe": {"hang_lo": 18.0, "hang_hi": 32.0, "block": ["L", "S"]},
                },
                "nq_gate": "L",
                "nq_gate_error": None,
                "recipe_version": "test",
            }
            with (
                mock.patch("order.tmf_channel_order.connect_fubon", return_value=object()),
                mock.patch("order.tmf_channel_order.pick_futopt_account", return_value=object()),
                mock.patch(
                    "order.tmf_channel_order.resolve_front_symbol",
                    return_value=("TMFH6", "微型臺指期貨086", "2026-08-19"),
                ),
                mock.patch("order.tmf_channel_order.fetch_1m_bars", return_value=[{"t": "x"}] * 25),
                mock.patch("order.tmf_channel_order.desired_from_simulate", return_value=fake_desired),
                mock.patch(
                    "order.tmf_channel_order.query_tmf_broker_net",
                    return_value={"s": "L", "n": 1, "ep": 44990.0, "acct_symbol": "FITM"},
                ),
                mock.patch("order.tmf_channel_order.get_futopt_order_results", return_value=[]),
                mock.patch("order.tmf_channel_order.place_futopt_order") as mock_place,
                mock.patch(
                    "order.tmf_channel_broadcast.emit_from_summary",
                    side_effect=lambda *a, **k: {"schema": "tmf-channel-broadcast-v1", "test": True},
                ),
            ):
                out = reconcile_once(cfg, force=True)

            self.assertTrue(out.get("protect_rail_synthesized"))
            self.assertEqual(out.get("want_s"), 45022.0)
            self.assertIsNone(out.get("want_l"))
            _session, resolved = mock_place.call_args[0]
            self.assertEqual(resolved.buy_sell, "Sell")
            self.assertEqual(resolved.price, 45022.0)
        finally:
            Path(ledger_path).unlink(missing_ok=True)


class DayPnlFilterTest(unittest.TestCase):
    def test_filters_prior_night_out_of_calendar_day(self):
        from order.tmf_channel_order import day_pnl_from_sim_trades

        trades = [
            {"xt": "2026-08-05T22:10:00.000+08:00", "pnl": -800.0},
            {"xt": "2026-08-06T01:10:00.000+08:00", "pnl": -500.0},  # trading day Aug 5
            {"xt": "2026-08-06T10:10:00.000+08:00", "pnl": -50.0},
            {"xt": "2026-08-06T11:00:00.000+08:00", "pnl": 12.0},
        ]
        # Whole-window sum would be -1338 and false-trip kill; day filter keeps day session only.
        self.assertEqual(day_pnl_from_sim_trades(trades, "2026-08-06"), -38.0)
        self.assertEqual(day_pnl_from_sim_trades(trades, "2026-08-05"), -1300.0)

    def test_live_skips_sim_day_pnl_kill(self):
        from order.tmf_channel_order import trip_day_pnl_kill

        self.assertTrue(
            trip_day_pnl_kill(dry_run=True, day_pnl_pts=-662.0, kill_day_loss_pts=400.0)
        )
        self.assertFalse(
            trip_day_pnl_kill(dry_run=False, day_pnl_pts=-662.0, kill_day_loss_pts=400.0)
        )


class QuietCancelThrottleTest(unittest.TestCase):
    """2026-08-08: order-layer throttle for redundant cancel/place round trips
    caused by PV8 flickering near block/non-block boundaries — confirmed via
    true re-simulation on the real engine + all 4 sanctioned windows that
    smoothing the classifier itself is not worth the safety cost (a
    significant, non-single-day-artifact number of bar-events where a
    resting order would sit in a technically-blocked cell). This throttle
    instead rate-limits the ORDER LAYER's redundant cancels, never touching
    what gets blocked or when."""

    def test_first_quiet_cancel_not_suppressed_and_stamps_ledger(self):
        from order.tmf_channel_order import should_throttle_quiet_cancel

        t0 = datetime(2026, 8, 8, 3, 0, 0, tzinfo=_TZ)
        suppress, ledger = should_throttle_quiet_cancel(
            "S",
            quiet_skip_reason="quiet_flat_skip:dry|dry|2.1min",
            open_pos=None,
            ledger={},
            now=t0,
        )
        self.assertFalse(suppress)
        self.assertEqual(ledger["cancel_throttle_last"]["S"], t0.isoformat())

    def test_second_quiet_cancel_within_window_is_suppressed_and_stamp_not_refreshed(self):
        from order.tmf_channel_order import should_throttle_quiet_cancel

        t0 = datetime(2026, 8, 8, 3, 0, 0, tzinfo=_TZ)
        _, ledger = should_throttle_quiet_cancel(
            "S", quiet_skip_reason="quiet_flat_skip:dry|dry|2.1min", open_pos=None, ledger={}, now=t0
        )
        later = t0 + timedelta(seconds=20)
        suppress, ledger = should_throttle_quiet_cancel(
            "S",
            quiet_skip_reason="quiet_flat_skip:dry|dry|2.4min",
            open_pos=None,
            ledger=ledger,
            min_interval_sec=45.0,
            now=later,
        )
        self.assertTrue(suppress)
        self.assertEqual(ledger["cancel_throttle_last"]["S"], t0.isoformat())  # not bumped

    def test_cancel_after_window_elapsed_is_not_suppressed_and_restamps(self):
        from order.tmf_channel_order import should_throttle_quiet_cancel

        t0 = datetime(2026, 8, 8, 3, 0, 0, tzinfo=_TZ)
        _, ledger = should_throttle_quiet_cancel(
            "S", quiet_skip_reason="quiet_flat_skip:dry|dry|2.1min", open_pos=None, ledger={}, now=t0
        )
        later = t0 + timedelta(seconds=46)
        suppress, ledger = should_throttle_quiet_cancel(
            "S",
            quiet_skip_reason="quiet_flat_skip:dry|dry|3.0min",
            open_pos=None,
            ledger=ledger,
            min_interval_sec=45.0,
            now=later,
        )
        self.assertFalse(suppress)
        self.assertEqual(ledger["cancel_throttle_last"]["S"], later.isoformat())

    def test_block_reason_never_suppressed_even_with_a_fresh_stamp(self):
        """Pins the non-negotiable invariant: block cancels are NEVER
        suppressed by this function under any ledger state."""
        from order.tmf_channel_order import should_throttle_quiet_cancel

        t0 = datetime(2026, 8, 8, 3, 0, 0, tzinfo=_TZ)
        ledger = {"cancel_throttle_last": {"S": t0.isoformat()}}
        suppress, ledger = should_throttle_quiet_cancel(
            "S",
            quiet_skip_reason="block:S",
            open_pos=None,
            ledger=ledger,
            now=t0 + timedelta(seconds=1),
        )
        self.assertFalse(suppress)

    def test_mixed_block_and_quiet_reason_string_isolates_side(self):
        """Regression pin for the exact joined-reason string shape
        should_throttle_quiet_cancel depends on from
        apply_quiet_flat_entry_gate. If that format ever changes, this test
        should break loudly instead of this throttle silently mis-parsing."""
        from order.tmf_channel_order import should_throttle_quiet_cancel

        reason = "block:S|quiet_flat_skip:both|dry|3.2min"
        suppress_s, _ = should_throttle_quiet_cancel(
            "S", quiet_skip_reason=reason, open_pos=None, ledger={}
        )
        self.assertFalse(suppress_s)  # fail-safe: "block:S" in reason

        t0 = datetime(2026, 8, 8, 3, 0, 0, tzinfo=_TZ)
        _, ledger = should_throttle_quiet_cancel(
            "L", quiet_skip_reason=reason, open_pos=None, ledger={}, now=t0
        )
        suppress_l, _ = should_throttle_quiet_cancel(
            "L",
            quiet_skip_reason=reason,
            open_pos=None,
            ledger=ledger,
            now=t0 + timedelta(seconds=5),
        )
        self.assertTrue(suppress_l)  # normal throttle logic applies to L

    def test_open_pos_not_none_never_suppressed(self):
        from order.tmf_channel_order import should_throttle_quiet_cancel

        t0 = datetime(2026, 8, 8, 3, 0, 0, tzinfo=_TZ)
        ledger = {"cancel_throttle_last": {"S": t0.isoformat()}}
        suppress, _ = should_throttle_quiet_cancel(
            "S",
            quiet_skip_reason="quiet_flat_skip:dry|dry|2.1min",
            open_pos={"s": "S", "n": 1, "ep": 44900.0},
            ledger=ledger,
            now=t0 + timedelta(seconds=1),
        )
        self.assertFalse(suppress)

    def test_malformed_stamp_in_ledger_fails_safe(self):
        from order.tmf_channel_order import should_throttle_quiet_cancel

        t0 = datetime(2026, 8, 8, 3, 0, 0, tzinfo=_TZ)
        ledger = {"cancel_throttle_last": {"S": "not-a-timestamp"}}
        suppress, ledger = should_throttle_quiet_cancel(
            "S", quiet_skip_reason="quiet_flat_skip:dry|dry|2.1min", open_pos=None, ledger=ledger, now=t0
        )
        self.assertFalse(suppress)
        self.assertEqual(ledger["cancel_throttle_last"]["S"], t0.isoformat())

    def test_reason_without_quiet_flat_skip_substring_is_not_suppressed(self):
        from order.tmf_channel_order import should_throttle_quiet_cancel

        for reason in (None, ""):
            suppress, _ = should_throttle_quiet_cancel(
                "S", quiet_skip_reason=reason, open_pos=None, ledger={}
            )
            self.assertFalse(suppress, msg=f"reason={reason!r}")


class QuietFlatEntryGateTest(unittest.TestCase):
    """2026-08-08: two independent live incidents fixed here.

    (1) A real Buy fired while the active cell showed block=['L','S']
    (night|normal / night|div_hh_weak_vol are hard-blocked by CELL_TUNE) —
    this gate never checked cell.block at all, only skip_quiet_mode. Fixed:
    block is stripped immediately, no hysteresis (it's a permanent policy).
    (2) pv flickering across the quiet/not-quiet boundary (contract<->dry)
    made the reconciler cancel+replace the same resting order every ~20-60s
    (~28 place + ~26 cancel/hour, confirmed live). Fixed: an already-resting
    rail is only cancelled once pv has stayed in the quiet set continuously
    for quiet_hysteresis_min (ledger-tracked, default 2 min); the streak
    clock resets the moment pv leaves the quiet set.
    """

    def _desired(self, pv: str, skip_quiet_mode: str = "dry", block=None) -> dict:
        return {
            "regime": pv,
            "active_cell": {
                "pv": pv,
                "recipe": {"skip_quiet_mode": skip_quiet_mode, "block": block or []},
            },
        }

    def test_first_quiet_poll_keeps_rail_and_starts_streak_clock(self):
        from order.tmf_channel_order import apply_quiet_flat_entry_gate

        now = datetime(2026, 8, 8, 3, 0, 0, tzinfo=_TZ)
        ws, wl, why, ledger = apply_quiet_flat_entry_gate(
            44312.0, 44200.0, broker_live=None, desired=self._desired("dry"), ledger={}, now=now
        )
        self.assertEqual(ws, 44312.0)
        self.assertEqual(wl, 44200.0)
        self.assertIsNone(why)
        self.assertEqual(ledger["quiet_pv_value"], "dry")
        self.assertEqual(ledger["quiet_pv_since"], now.isoformat())

    def test_flicker_within_hysteresis_window_never_strips(self):
        from order.tmf_channel_order import apply_quiet_flat_entry_gate

        t0 = datetime(2026, 8, 8, 3, 0, 0, tzinfo=_TZ)
        ledger: dict = {}
        ws = wl = None
        for i, pv in enumerate(["dry", "contract", "dry", "contract", "dry"]):
            now = t0 + timedelta(seconds=20 * i)
            ws, wl, why, ledger = apply_quiet_flat_entry_gate(
                44312.0, 44200.0, broker_live=None, desired=self._desired(pv), ledger=ledger, now=now
            )
        # 5 polls spanning 80s, well under the 2-min default hysteresis.
        self.assertEqual(ws, 44312.0)
        self.assertEqual(wl, 44200.0)

    def test_brief_exit_from_quiet_bridges_the_streak_not_resets_it(self):
        """The live bug even after the first hysteresis fix: a market that
        keeps drifting briefly out of "dry" and back kept resetting the
        clock to zero, so the streak matured every ~2-3 min and cancelled
        + immediately replaced the identical resting price each time
        (confirmed live: 5 cycles at 44941 within 10 minutes). A single
        ~20-30s poll outside the quiet set must not reset the maturing
        streak — only a *sustained* exit should."""
        from order.tmf_channel_order import apply_quiet_flat_entry_gate

        t0 = datetime(2026, 8, 8, 3, 0, 0, tzinfo=_TZ)
        _, _, _, ledger = apply_quiet_flat_entry_gate(
            44312.0, 44200.0, broker_live=None, desired=self._desired("dry"), ledger={}, now=t0
        )
        brief_gap_at = t0 + timedelta(seconds=30)  # well under the 1-min exit debounce
        _, _, _, ledger = apply_quiet_flat_entry_gate(
            44312.0, 44200.0, broker_live=None, desired=self._desired("contract", "dry"),
            ledger=ledger, now=brief_gap_at,
        )
        self.assertEqual(ledger["quiet_pv_since"], t0.isoformat())  # unchanged, not reset
        back_to_quiet_at = t0 + timedelta(minutes=2, seconds=1)
        ws, wl, why, ledger = apply_quiet_flat_entry_gate(
            44312.0, 44200.0, broker_live=None, desired=self._desired("dry"),
            ledger=ledger, now=back_to_quiet_at,
        )
        # Streak had already been running since t0 -- matures on schedule
        # despite the brief 30s gap, not restarted from that gap.
        self.assertIsNone(ws)
        self.assertIsNone(wl)
        self.assertIn("quiet_flat_skip", why)

    def test_sustained_exit_from_quiet_does_reset_the_streak(self):
        from order.tmf_channel_order import apply_quiet_flat_entry_gate

        t0 = datetime(2026, 8, 8, 3, 0, 0, tzinfo=_TZ)
        _, _, _, ledger = apply_quiet_flat_entry_gate(
            44312.0, 44200.0, broker_live=None, desired=self._desired("dry"), ledger={}, now=t0
        )
        first_exit_at = t0 + timedelta(seconds=20)
        _, _, _, ledger = apply_quiet_flat_entry_gate(
            44312.0, 44200.0, broker_live=None, desired=self._desired("contract", "dry"),
            ledger=ledger, now=first_exit_at,
        )
        self.assertEqual(ledger["quiet_pv_since"], t0.isoformat())  # not reset yet — only 20s out
        sustained_exit_at = first_exit_at + timedelta(minutes=1, seconds=1)  # past exit debounce
        _, _, _, ledger = apply_quiet_flat_entry_gate(
            44312.0, 44200.0, broker_live=None, desired=self._desired("contract", "dry"),
            ledger=ledger, now=sustained_exit_at,
        )
        self.assertIsNone(ledger["quiet_pv_since"])
        quiet_again_at = t0 + timedelta(minutes=3)  # would be past hysteresis if clock hadn't reset
        ws, wl, why, ledger = apply_quiet_flat_entry_gate(
            44312.0, 44200.0, broker_live=None, desired=self._desired("dry"),
            ledger=ledger, now=quiet_again_at,
        )
        self.assertEqual(ws, 44312.0)
        self.assertEqual(wl, 44200.0)
        self.assertIsNone(why)

    def test_quiet_streak_past_hysteresis_strips(self):
        from order.tmf_channel_order import apply_quiet_flat_entry_gate

        t0 = datetime(2026, 8, 8, 3, 0, 0, tzinfo=_TZ)
        _, _, _, ledger = apply_quiet_flat_entry_gate(
            44312.0, 44200.0, broker_live=None, desired=self._desired("dry"), ledger={}, now=t0
        )
        later = t0 + timedelta(minutes=2, seconds=1)
        ws, wl, why, ledger = apply_quiet_flat_entry_gate(
            44312.0, 44200.0, broker_live=None, desired=self._desired("dry"), ledger=ledger, now=later
        )
        self.assertIsNone(ws)
        self.assertIsNone(wl)
        self.assertIn("quiet_flat_skip:dry|dry", why)

    def test_flat_contract_kept_when_quiet_is_dry_only(self):
        from order.tmf_channel_order import apply_quiet_flat_entry_gate

        ws, wl, why, ledger = apply_quiet_flat_entry_gate(
            44312.0,
            44200.0,
            broker_live=None,
            desired=self._desired("contract", "dry"),
            ledger={},
        )
        self.assertEqual(ws, 44312.0)
        self.assertEqual(wl, 44200.0)
        self.assertIsNone(why)

    def test_in_position_keeps_protect_rails(self):
        from order.tmf_channel_order import apply_quiet_flat_entry_gate

        ws, wl, why, ledger = apply_quiet_flat_entry_gate(
            None,
            44200.0,
            broker_live={"s": "S", "n": 1, "ep": 44350.0},
            desired=self._desired("dry"),
            ledger={},
        )
        self.assertIsNone(ws)
        self.assertEqual(wl, 44200.0)
        self.assertIsNone(why)

    def test_broker_zero_size_treated_as_flat_starts_streak(self):
        from order.tmf_channel_order import apply_quiet_flat_entry_gate

        ws, wl, why, ledger = apply_quiet_flat_entry_gate(
            100.0,
            90.0,
            broker_live={"s": "S", "n": 0},
            desired=self._desired("dry"),
            ledger={},
        )
        self.assertEqual(ws, 100.0)
        self.assertEqual(ledger["quiet_pv_value"], "dry")

    def test_hard_block_strips_both_sides_immediately_no_hysteresis(self):
        """The live bug: night|normal / night|div_hh_weak_vol have
        block=['L','S'] but a real Buy still went out — this gate never
        checked cell.block, only skip_quiet_mode."""
        from order.tmf_channel_order import apply_quiet_flat_entry_gate

        ws, wl, why, ledger = apply_quiet_flat_entry_gate(
            44925.0,
            44925.0,
            broker_live=None,
            desired=self._desired("normal", skip_quiet_mode="dry", block=["L", "S"]),
            ledger={},
        )
        self.assertIsNone(ws)
        self.assertIsNone(wl)
        self.assertIn("block:S", why)
        self.assertIn("block:L", why)

    def test_hard_block_one_side_only_keeps_the_other(self):
        from order.tmf_channel_order import apply_quiet_flat_entry_gate

        ws, wl, why, ledger = apply_quiet_flat_entry_gate(
            44925.0,
            44800.0,
            broker_live=None,
            desired=self._desired("normal", skip_quiet_mode="dry", block=["L"]),
            ledger={},
        )
        self.assertEqual(ws, 44925.0)
        self.assertIsNone(wl)
        self.assertEqual(why, "block:L")

    def test_block_and_matured_quiet_streak_combine_reasons(self):
        from order.tmf_channel_order import apply_quiet_flat_entry_gate

        t0 = datetime(2026, 8, 8, 3, 0, 0, tzinfo=_TZ)
        ledger = {"quiet_pv_since": t0.isoformat(), "quiet_pv_value": "dry"}
        later = t0 + timedelta(minutes=3)
        ws, wl, why, ledger = apply_quiet_flat_entry_gate(
            44925.0,
            44800.0,
            broker_live=None,
            desired=self._desired("dry", skip_quiet_mode="dry", block=["S"]),
            ledger=ledger,
            now=later,
        )
        self.assertIsNone(ws)
        self.assertIsNone(wl)
        self.assertIn("block:S", why)
        self.assertIn("quiet_flat_skip", why)


class MaxHoldSafetyNetTest(unittest.TestCase):
    """2026-08-07: independent, sim-state-free backstop — see check_max_hold_safety_net
    docstring. Confirmed live 2026-08-06: a position sat 8+ hours with simulate()'s
    own max_hold_bars (16-38) never firing once broker_live went authoritative."""

    def _empty_ledger(self):
        return {"position_open_ts": None, "position_open_sig": None}

    def test_flat_position_clears_tracking(self):
        ledger = {"position_open_ts": "2026-08-06T13:41:00+08:00", "position_open_sig": "L"}
        ledger, elapsed, why = check_max_hold_safety_net(
            ledger, broker_live=None, max_hold_safety_min=90.0
        )
        self.assertIsNone(ledger["position_open_ts"])
        self.assertIsNone(ledger["position_open_sig"])
        self.assertIsNone(elapsed)
        self.assertIsNone(why)

    def test_zero_size_broker_live_treated_as_flat(self):
        ledger, elapsed, why = check_max_hold_safety_net(
            self._empty_ledger(), broker_live={"s": "L", "n": 0}, max_hold_safety_min=90.0
        )
        self.assertIsNone(ledger["position_open_ts"])
        self.assertIsNone(elapsed)
        self.assertIsNone(why)

    def test_first_observation_starts_clock_no_flatten(self):
        now = datetime(2026, 8, 6, 13, 41, tzinfo=_TZ)
        ledger, elapsed, why = check_max_hold_safety_net(
            self._empty_ledger(),
            broker_live={"s": "L", "n": 1, "ep": 44223.0},
            max_hold_safety_min=90.0,
            now=now,
        )
        self.assertEqual(ledger["position_open_sig"], "L")
        self.assertEqual(ledger["position_open_ts"], now.isoformat())
        self.assertEqual(elapsed, 0.0)
        self.assertIsNone(why)

    def test_same_side_continues_clock_under_cap_no_flatten(self):
        ledger = {
            "position_open_ts": datetime(2026, 8, 6, 13, 41, tzinfo=_TZ).isoformat(),
            "position_open_sig": "L",
        }
        later = datetime(2026, 8, 6, 14, 30, tzinfo=_TZ)  # 49 min later
        ledger, elapsed, why = check_max_hold_safety_net(
            ledger, broker_live={"s": "L", "n": 1}, max_hold_safety_min=90.0, now=later
        )
        self.assertAlmostEqual(elapsed, 49.0, places=3)
        self.assertIsNone(why)

    def test_exceeds_cap_triggers_flatten(self):
        """The exact scenario confirmed live 2026-08-06: opened 13:41, still open
        hours later — must trigger past the safety cap regardless of sim state."""
        ledger = {
            "position_open_ts": datetime(2026, 8, 6, 13, 41, tzinfo=_TZ).isoformat(),
            "position_open_sig": "L",
        }
        much_later = datetime(2026, 8, 6, 21, 40, tzinfo=_TZ)  # ~8 hours later
        ledger, elapsed, why = check_max_hold_safety_net(
            ledger, broker_live={"s": "L", "n": 1}, max_hold_safety_min=90.0, now=much_later
        )
        self.assertGreater(elapsed, 470)
        self.assertIsNotNone(why)
        self.assertIn("max_hold_safety_net", why)

    def test_side_flip_resets_clock_not_immediately_stale(self):
        """A short opening right after a long closed shouldn't inherit the long's age."""
        ledger = {
            "position_open_ts": datetime(2026, 8, 6, 13, 41, tzinfo=_TZ).isoformat(),
            "position_open_sig": "L",
        }
        later = datetime(2026, 8, 6, 21, 40, tzinfo=_TZ)
        ledger, elapsed, why = check_max_hold_safety_net(
            ledger, broker_live={"s": "S", "n": 1}, max_hold_safety_min=90.0, now=later
        )
        self.assertEqual(ledger["position_open_sig"], "S")
        self.assertEqual(elapsed, 0.0)
        self.assertIsNone(why)

    def test_corrupt_timestamp_in_ledger_does_not_crash(self):
        ledger = {"position_open_ts": "not-a-timestamp", "position_open_sig": "L"}
        now = datetime(2026, 8, 6, 13, 41, tzinfo=_TZ)
        ledger, elapsed, why = check_max_hold_safety_net(
            ledger, broker_live={"s": "L", "n": 1}, max_hold_safety_min=90.0, now=now
        )
        # treated as a fresh open (can't trust the corrupt timestamp)
        self.assertEqual(elapsed, 0.0)
        self.assertIsNone(why)

    def test_query_failure_preserves_clock_instead_of_resetting_it(self):
        """2026-08-10 live: a ~4min "call id" query outage made broker_live go
        None (query raised, not a confirmed flat) and the old code wiped
        position_open_ts anyway -- silently restarting a 90min safety-net
        clock on a position that had already been open ~65min. query_failed
        must leave the tracked open_ts/sig untouched so the outage can never
        extend how long a naked position rides past the cap."""
        ledger = {
            "position_open_ts": datetime(2026, 8, 10, 15, 9, tzinfo=_TZ).isoformat(),
            "position_open_sig": "L",
        }
        during_outage = datetime(2026, 8, 10, 16, 14, tzinfo=_TZ)
        ledger, elapsed, why = check_max_hold_safety_net(
            ledger,
            broker_live=None,
            max_hold_safety_min=90.0,
            now=during_outage,
            query_failed=True,
        )
        self.assertEqual(ledger["position_open_ts"], datetime(2026, 8, 10, 15, 9, tzinfo=_TZ).isoformat())
        self.assertEqual(ledger["position_open_sig"], "L")
        self.assertIsNone(elapsed)
        self.assertIsNone(why)

        after_outage = datetime(2026, 8, 10, 17, 0, tzinfo=_TZ)  # 111min since real open
        ledger, elapsed, why = check_max_hold_safety_net(
            ledger,
            broker_live={"s": "L", "n": 1},
            max_hold_safety_min=90.0,
            now=after_outage,
            query_failed=False,
        )
        self.assertGreater(elapsed, 90)
        self.assertIsNotNone(why)


class ConsecutiveOrderFailureKillTest(unittest.TestCase):
    """record_actions()'s consecutive_order_failures streak (2026-08-10):
    found live a 財力證明額度 broker rejection made the worker retry the
    same failing SELL every poll indefinitely. This streak feeds a new
    kill_triggers check in reconcile_once() (cfg.kill_consecutive_failures,
    default 5) so the worker halts instead of retrying forever.
    """

    def _ledger(self, **overrides):
        base = {
            "schema": "tmf-channel-ledger-v1",
            "day": trading_day_str(),
            "consecutive_order_failures": 0,
            "api_calls_day": 0,
            "actions_tail": [],
        }
        base.update(overrides)
        return base

    def test_failed_action_increments_streak(self):
        ledger = self._ledger()
        act = {"kind": "place", "side": "S", "ok": False, "counts_api": True, "error": "quota"}
        ledger = record_actions(ledger, [act], api_n=0)
        self.assertEqual(ledger["consecutive_order_failures"], 1)

    def test_streak_persists_and_accumulates_across_polls(self):
        ledger = self._ledger()
        act = {"kind": "place", "side": "S", "ok": False, "counts_api": True, "error": "quota"}
        for expected in (1, 2, 3, 4, 5):
            ledger = record_actions(ledger, [act], api_n=0)
            self.assertEqual(ledger["consecutive_order_failures"], expected)

    def test_success_resets_streak(self):
        ledger = self._ledger(consecutive_order_failures=4)
        ok_act = {"kind": "place", "side": "S", "ok": True, "counts_api": True}
        ledger = record_actions(ledger, [ok_act], api_n=1)
        self.assertEqual(ledger["consecutive_order_failures"], 0)

    def test_non_api_actions_do_not_affect_streak(self):
        ledger = self._ledger(consecutive_order_failures=2)
        non_api_act = {"kind": "noop", "counts_api": False}
        ledger = record_actions(ledger, [non_api_act], api_n=0)
        self.assertEqual(ledger["consecutive_order_failures"], 2)

    def test_mixed_actions_in_one_call_use_final_outcome(self):
        ledger = self._ledger()
        actions = [
            {"kind": "cancel", "ok": False, "counts_api": True},
            {"kind": "place", "ok": True, "counts_api": True},
        ]
        ledger = record_actions(ledger, actions, api_n=1)
        self.assertEqual(ledger["consecutive_order_failures"], 0)

    def test_config_default_is_five(self):
        env = {"ORDER_TMF_CHANNEL_KILL_CONSECUTIVE_FAILURES": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_tmf_channel_order_config()
        self.assertEqual(cfg.kill_consecutive_failures, 5)

    def test_config_respects_env_override(self):
        env = {"ORDER_TMF_CHANNEL_KILL_CONSECUTIVE_FAILURES": "7"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_tmf_channel_order_config()
        self.assertEqual(cfg.kill_consecutive_failures, 7)

    def test_reconcile_once_kills_on_fifth_consecutive_place_failure(self):
        """End-to-end: seed the ledger one failure short of the threshold,
        force a poll where the only broker action is a failing place, and
        confirm reconcile_once actually flips killed=True with a reason
        naming consecutive_order_failures -- not just that record_actions()
        counts correctly in isolation."""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        ledger_path = tmp.name
        try:
            seed_ledger = {
                "schema": "tmf-channel-ledger-v1",
                "day": trading_day_str(),
                "api_calls_day": 4,
                "day_pnl_pts": 0.0,
                "killed": False,
                "kill_reason": None,
                "last_symbol": None,
                "last_desired": None,
                "actions_tail": [],
                "broker_pos": None,
                "consecutive_order_failures": 4,
            }
            save_ledger(ledger_path, seed_ledger)
            cfg = TmfChannelOrderConfig(
                strategy_id="tmf-micro-channel",
                order_enabled=True,
                auto_submit=True,
                dry_run=False,
                max_lots=1,
                place_every=1,
                rail_match_pts=2.0,
                max_api_per_poll=8,
                max_api_per_day=120,
                user_def="tmfch",
                ledger_path=ledger_path,
                product="TMF",
                kill_day_loss_pts=400.0,
                max_hold_safety_min=90.0,
                kill_consecutive_failures=5,
                recipe={},
                recipe_version="test",
            )
            fake_desired = {
                "ok": True,
                "want_s": 44958.0,
                "want_l": None,
                "open_pos": None,
                "trades": [],
                "events": [],
                "spot": 44900.0,
                "last_t": "2026-08-10T12:00:00.000+08:00",
                "regime": "normal",
                "active_cell": {"cell": "day|normal", "session": "day", "pv": "normal", "recipe": {}},
                "nq_gate": None,
                "nq_gate_error": None,
                "recipe_version": "test",
            }
            with (
                mock.patch("order.tmf_channel_order.connect_fubon", return_value=object()),
                mock.patch("order.tmf_channel_order.pick_futopt_account", return_value=object()),
                mock.patch(
                    "order.tmf_channel_order.resolve_front_symbol",
                    return_value=("TMFH6", "微型臺指期貨086", "2026-08-19"),
                ),
                mock.patch("order.tmf_channel_order.fetch_1m_bars", return_value=[{"t": "x"}] * 25),
                mock.patch("order.tmf_channel_order.desired_from_simulate", return_value=fake_desired),
                mock.patch("order.tmf_channel_order.query_tmf_broker_net", return_value=None),
                mock.patch("order.tmf_channel_order.get_futopt_order_results", return_value=[]),
                mock.patch(
                    "order.tmf_channel_order.place_futopt_order",
                    side_effect=RuntimeError("交易額度超過資(財)力證明額度(後檯)[8481329]"),
                ),
                mock.patch(
                    "order.tmf_channel_broadcast.emit_from_summary",
                    side_effect=lambda *a, **k: {"schema": "tmf-channel-broadcast-v1", "test": True},
                ),
            ):
                out = reconcile_once(cfg, force=True)

            self.assertEqual(out.get("reason"), "reconciled")
            saved = json.loads(Path(ledger_path).read_text())
            self.assertEqual(saved["consecutive_order_failures"], 5)
            self.assertTrue(saved["killed"])
            self.assertIn("consecutive_order_failures=5>=5", saved["kill_reason"])
        finally:
            Path(ledger_path).unlink(missing_ok=True)


class BrokerQueryFailureSuppressesFreshEntryTest(unittest.TestCase):
    """2026-08-10 live: query_tmf_broker_net failed with a "call id" error for
    ~4min while the sim's own open_pos also read None (async fill the bar
    engine never saw), so want_l flowed through unguarded -- 5 duplicate
    L 45131 places under night|expand_up/contract before self-correcting via
    dedupe_extra_rail once the query recovered. reconcile_once now nulls
    want_s/want_l whenever the broker query itself raises, so a query outage
    can never spray fresh entries while real state is unconfirmed."""

    def test_no_place_action_when_broker_query_raises(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        ledger_path = tmp.name
        try:
            save_ledger(ledger_path, {
                "schema": "tmf-channel-ledger-v1",
                "day": trading_day_str(),
                "api_calls_day": 0,
                "day_pnl_pts": 0.0,
                "killed": False,
                "kill_reason": None,
                "last_symbol": None,
                "last_desired": None,
                "actions_tail": [],
                "broker_pos": None,
                "consecutive_order_failures": 0,
            })
            cfg = _dry_cfg(ledger_path)
            fake_desired = {
                "ok": True,
                "want_s": None,
                "want_l": 45131.0,
                "open_pos": None,
                "trades": [],
                "events": [],
                "spot": 45100.0,
                "last_t": "2026-08-10T16:16:00.000+08:00",
                "regime": "expand_up",
                "active_cell": {"cell": "night|expand_up", "session": "night", "pv": "expand_up", "recipe": {}},
                "nq_gate": "L",
                "nq_gate_error": None,
                "recipe_version": "test",
            }
            with (
                mock.patch("order.tmf_channel_order.connect_fubon", return_value=object()),
                mock.patch("order.tmf_channel_order.pick_futopt_account", return_value=object()),
                mock.patch(
                    "order.tmf_channel_order.resolve_front_symbol",
                    return_value=("TMFH6", "微型臺指期貨086", "2026-08-19"),
                ),
                mock.patch("order.tmf_channel_order.fetch_1m_bars", return_value=[{"t": "x"}] * 25),
                mock.patch("order.tmf_channel_order.desired_from_simulate", return_value=fake_desired),
                mock.patch(
                    "order.tmf_channel_order.query_tmf_broker_net",
                    side_effect=RuntimeError("call id"),
                ),
                mock.patch("order.tmf_channel_order.get_futopt_order_results", return_value=[]),
                mock.patch("order.tmf_channel_order.place_futopt_order") as mock_place,
                mock.patch(
                    "order.tmf_channel_broadcast.emit_from_summary",
                    side_effect=lambda *a, **k: {"schema": "tmf-channel-broadcast-v1", "test": True},
                ),
            ):
                out = reconcile_once(cfg, force=True)

            self.assertEqual(out.get("broker_query_error"), "call id")
            self.assertIsNone(out.get("want_l"))
            self.assertEqual(mock_place.call_count, 0)
        finally:
            Path(ledger_path).unlink(missing_ok=True)

    def test_existing_resting_rail_survives_a_query_outage(self):
        """Found while answering "does touching the line always trade": the
        query-failure guard above nulls want_s/want_l, and the ordinary
        "want vanished -> cancel" path in the cancel-extras loop doesn't
        distinguish that from a confirmed flat -- it would have cancelled a
        real, still-legitimate resting rail (including one this poll's own
        protect-rail synthesis just built) for the whole outage. Confirms a
        resting order survives untouched instead."""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        ledger_path = tmp.name
        try:
            save_ledger(ledger_path, {
                "schema": "tmf-channel-ledger-v1",
                "day": trading_day_str(),
                "api_calls_day": 0,
                "day_pnl_pts": 0.0,
                "killed": False,
                "kill_reason": None,
                "last_symbol": None,
                "last_desired": None,
                "actions_tail": [],
                "broker_pos": None,
                "consecutive_order_failures": 0,
            })
            cfg = _dry_cfg(ledger_path)
            fake_desired = {
                "ok": True,
                "want_s": 45022.0,
                "want_l": None,
                "open_pos": {"s": "L", "n": 1, "ep": 44990.0},
                "trades": [],
                "events": [],
                "spot": 45000.0,
                "last_t": "2026-08-10T17:50:00.000+08:00",
                "regime": "normal",
                "active_cell": {"cell": "night|normal", "session": "night", "pv": "normal", "recipe": {}},
                "nq_gate": "L",
                "nq_gate_error": None,
                "recipe_version": "test",
            }
            resting_short = mock.Mock(
                symbol="FITM", status=0, buy_sell=mock.Mock(name="Sell"), price=45022.0,
            )
            resting_short.buy_sell.name = "Sell"
            with (
                mock.patch("order.tmf_channel_order.connect_fubon", return_value=object()),
                mock.patch("order.tmf_channel_order.pick_futopt_account", return_value=object()),
                mock.patch(
                    "order.tmf_channel_order.resolve_front_symbol",
                    return_value=("TMFH6", "微型臺指期貨086", "2026-08-19"),
                ),
                mock.patch("order.tmf_channel_order.fetch_1m_bars", return_value=[{"t": "x"}] * 25),
                mock.patch("order.tmf_channel_order.desired_from_simulate", return_value=fake_desired),
                mock.patch(
                    "order.tmf_channel_order.query_tmf_broker_net",
                    side_effect=RuntimeError("call id"),
                ),
                mock.patch(
                    "order.tmf_channel_order.get_futopt_order_results",
                    return_value=[resting_short],
                ),
                mock.patch("order.tmf_channel_order.cancel_futopt_order") as mock_cancel,
                mock.patch("order.tmf_channel_order.place_futopt_order") as mock_place,
                mock.patch(
                    "order.tmf_channel_broadcast.emit_from_summary",
                    side_effect=lambda *a, **k: {"schema": "tmf-channel-broadcast-v1", "test": True},
                ),
            ):
                out = reconcile_once(cfg, force=True)

            self.assertEqual(out.get("broker_query_error"), "call id")
            self.assertEqual(mock_cancel.call_count, 0)
            self.assertEqual(mock_place.call_count, 0)
            self.assertEqual(
                out.get("query_outage_cancel_skipped"), [{"side": "S", "price": 45022.0}]
            )
        finally:
            Path(ledger_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
