"""Unit tests · dayflip-futures-short v1 order sleeve state machine.

2026-08-07：idle→entered 路徑已由真實(dry-run)冒煙測試驗證過，但 entered→covered
與 entered→force_closed 這兩條路徑在任何測試中都從未真的被走過（因為離峰測試時
永遠拿不到跳空達標的候選）。這裡用 mock 讓 pick_signal/broker 呼叫回傳固定值，
專門補測這兩條未驗證過的分支，避免靠等到真的有訊號那天才第一次驗證。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from order.dayflip_short_order import (
    LedgerState,
    _dayflip_yaml_block,
    _today,
    in_dayflip_short_trade_window,
    reconcile_once,
    save_ledger,
)


def _entered_ledger(path: Path, **overrides) -> None:
    state = LedgerState(
        date=_today(),
        status="entered",
        stock_id="2344",
        futures_symbol="CLFH6",
        entry_order_no="E1001",
        entry_price=100.0,
        cover_order_no="C2002",
        cover_target_price=98.0,
        fgap_pct=6.5,
        margin_ntd=44000.0,
        margin_source="broker_api",
    )
    for k, v in overrides.items():
        setattr(state, k, v)
    with mock.patch("order.dayflip_short_order._LEDGER_PATH", path):
        save_ledger(state)


class DayflipShortEnteredTransitionsTest(unittest.TestCase):
    def setUp(self):
        self.ledger_path = Path("/tmp/test_dayflip_short_ledger.json")
        self.ledger_path.unlink(missing_ok=True)

    def tearDown(self):
        self.ledger_path.unlink(missing_ok=True)

    def test_entered_past_force_close_time_closes_position(self):
        """entered + hm>=13:40 + 回補單尚未成交 → 送出 Buy/Close/market 平倉單並轉 force_closed。"""
        _entered_ledger(self.ledger_path)
        unfilled = mock.Mock(order_no="C2002", status="PendingSubmit", filled_qty=0)
        with (
            mock.patch("order.dayflip_short_order._LEDGER_PATH", self.ledger_path),
            mock.patch("order.fubon_session.connect_fubon", return_value=object()),
            mock.patch("order.dayflip_short_order.pick_futopt_account", return_value=object()),
            mock.patch(
                "order.dayflip_short_order.get_futopt_order_results",
                return_value=[unfilled],
            ),
            mock.patch(
                "order.dayflip_short_order.place_futopt_order",
                return_value={"ok": True, "data": {"order_no": "F3003"}},
            ) as mock_place,
        ):
            out = reconcile_once(dry_run=True, override_hm="13:41")

        self.assertEqual(out["action"], "force_closed")
        self.assertEqual(mock_place.call_count, 1)
        _session, resolved = mock_place.call_args[0]
        self.assertEqual(resolved.buy_sell, "Buy")  # cover a short by buying
        self.assertEqual(resolved.order_type, "Close")
        self.assertEqual(resolved.price_type, "market")
        self.assertEqual(resolved.symbol, "CLFH6")
        self.assertEqual(mock_place.call_args.kwargs.get("dry_run"), True)

        saved = json.loads(self.ledger_path.read_text())
        self.assertEqual(saved["status"], "force_closed")
        self.assertEqual(saved["last_action"], "force_closed_at_1340")

    def test_entered_past_force_close_time_with_already_filled_cover_becomes_covered_not_double_closed(self):
        """race規則：即使 hm>=13:40，回補單若已成交必須先轉 covered，絕不能再送
        第二張強制平倉單去打一個已經平倉的部位。"""
        _entered_ledger(self.ledger_path)
        filled = mock.Mock(order_no="C2002", status="Filled", filled_qty=1)
        with (
            mock.patch("order.dayflip_short_order._LEDGER_PATH", self.ledger_path),
            mock.patch("order.fubon_session.connect_fubon", return_value=object()),
            mock.patch("order.dayflip_short_order.pick_futopt_account", return_value=object()),
            mock.patch(
                "order.dayflip_short_order.get_futopt_order_results",
                return_value=[filled],
            ),
            mock.patch("order.dayflip_short_order.place_futopt_order") as mock_place,
        ):
            out = reconcile_once(dry_run=True, override_hm="13:41")

        self.assertEqual(out["action"], "covered")
        self.assertEqual(mock_place.call_count, 0)  # must NOT fire a second close order
        saved = json.loads(self.ledger_path.read_text())
        self.assertEqual(saved["status"], "covered")

    def test_entered_before_force_close_with_filled_cover_becomes_covered(self):
        """entered + 委託回報顯示回補單已成交 → 轉 covered，不會誤觸強制平倉。"""
        _entered_ledger(self.ledger_path)
        fake_result = mock.Mock()
        fake_result.order_no = "C2002"
        fake_result.status = "Filled"
        fake_result.filled_qty = 1
        with (
            mock.patch("order.dayflip_short_order._LEDGER_PATH", self.ledger_path),
            mock.patch("order.fubon_session.connect_fubon", return_value=object()),
            mock.patch("order.dayflip_short_order.pick_futopt_account", return_value=object()),
            mock.patch(
                "order.dayflip_short_order.get_futopt_order_results",
                return_value=[fake_result],
            ),
            mock.patch("order.dayflip_short_order.place_futopt_order") as mock_place,
        ):
            out = reconcile_once(dry_run=True, override_hm="10:00")

        self.assertEqual(out["action"], "covered")
        self.assertEqual(mock_place.call_count, 0)  # covered path never places new orders
        saved = json.loads(self.ledger_path.read_text())
        self.assertEqual(saved["status"], "covered")
        self.assertEqual(saved["last_action"], "cover_filled")

    def test_entered_before_force_close_unfilled_cover_stays_entered(self):
        """回補單還沒成交時必須維持 entered、繼續等待，不能提早轉態。"""
        _entered_ledger(self.ledger_path)
        fake_result = mock.Mock()
        fake_result.order_no = "C2002"
        fake_result.status = "PendingSubmit"
        fake_result.filled_qty = 0
        with (
            mock.patch("order.dayflip_short_order._LEDGER_PATH", self.ledger_path),
            mock.patch("order.fubon_session.connect_fubon", return_value=object()),
            mock.patch("order.dayflip_short_order.pick_futopt_account", return_value=object()),
            mock.patch(
                "order.dayflip_short_order.get_futopt_order_results",
                return_value=[fake_result],
            ),
            mock.patch("order.dayflip_short_order.place_futopt_order") as mock_place,
        ):
            out = reconcile_once(dry_run=True, override_hm="10:00")

        self.assertEqual(out["action"], "waiting_cover")
        self.assertEqual(mock_place.call_count, 0)
        saved = json.loads(self.ledger_path.read_text())
        self.assertEqual(saved["status"], "entered")  # unchanged

    def test_entered_missing_cover_order_no_retries_and_succeeds(self):
        """entered + cover_order_no 是 None(先前補下失敗留下的狀態) → 每次poll補試
        一次；這次成功就把 cover_order_no 補上，繼續留在 entered。"""
        _entered_ledger(self.ledger_path, cover_order_no=None, cover_target_price=None)
        unfilled = mock.Mock(order_no="C9009", status="PendingSubmit", filled_qty=0)
        with (
            mock.patch("order.dayflip_short_order._LEDGER_PATH", self.ledger_path),
            mock.patch("order.fubon_session.connect_fubon", return_value=object()),
            mock.patch("order.dayflip_short_order.pick_futopt_account", return_value=object()),
            mock.patch(
                "order.dayflip_short_order.get_futopt_order_results",
                return_value=[unfilled],
            ),
            mock.patch(
                "order.dayflip_short_order.place_futopt_order",
                return_value={"ok": True, "data": {"order_no": "C9009"}},
            ) as mock_place,
        ):
            out = reconcile_once(dry_run=True, override_hm="10:00")

        self.assertEqual(mock_place.call_count, 1)  # only the cover retry, no force-close
        resolved = mock_place.call_args[0][1]
        self.assertEqual(resolved.buy_sell, "Buy")
        self.assertEqual(resolved.order_type, "Close")
        self.assertEqual(resolved.price_type, "limit")
        saved = json.loads(self.ledger_path.read_text())
        self.assertEqual(saved["cover_order_no"], "C9009")
        self.assertEqual(saved["status"], "entered")
        self.assertEqual(out["action"], "waiting_cover")

    def test_entered_missing_cover_order_no_retry_fails_stays_entered_no_crash(self):
        """cover 補下重試也失敗時：不 raise、不誤標任何 terminal 狀態，留在 entered
        等下一次 poll 再試（安全網是 13:40 強制平倉，不依賴 cover_order_no 存在）。"""
        _entered_ledger(self.ledger_path, cover_order_no=None, cover_target_price=None)
        with (
            mock.patch("order.dayflip_short_order._LEDGER_PATH", self.ledger_path),
            mock.patch("order.fubon_session.connect_fubon", return_value=object()),
            mock.patch("order.dayflip_short_order.pick_futopt_account", return_value=object()),
            mock.patch(
                "order.dayflip_short_order.get_futopt_order_results",
                return_value=[],
            ),
            mock.patch(
                "order.dayflip_short_order.place_futopt_order",
                side_effect=RuntimeError("broker rejected cover order"),
            ) as mock_place,
        ):
            out = reconcile_once(dry_run=True, override_hm="10:00")

        self.assertEqual(mock_place.call_count, 1)
        saved = json.loads(self.ledger_path.read_text())
        self.assertIsNone(saved["cover_order_no"])
        self.assertEqual(saved["status"], "entered")
        self.assertEqual(saved["last_action"], "cover_retry_failed")
        self.assertEqual(out["action"], "waiting_cover")

    def test_entered_force_close_order_exception_stays_entered_not_falsely_closed(self):
        """CRITICAL 修正驗證：13:40強制平倉單本身送出失敗(broker拋例外)時，
        絕不能誤標成 force_closed——必須留在 entered，讓下一次 poll(或
        dayflip-short-watch 13:50檢查點)還能看到真實的裸空部位狀態。"""
        _entered_ledger(self.ledger_path)
        unfilled = mock.Mock(order_no="C2002", status="PendingSubmit", filled_qty=0)
        with (
            mock.patch("order.dayflip_short_order._LEDGER_PATH", self.ledger_path),
            mock.patch("order.fubon_session.connect_fubon", return_value=object()),
            mock.patch("order.dayflip_short_order.pick_futopt_account", return_value=object()),
            mock.patch(
                "order.dayflip_short_order.get_futopt_order_results",
                return_value=[unfilled],
            ),
            mock.patch(
                "order.dayflip_short_order.place_futopt_order",
                side_effect=RuntimeError("broker down at force-close"),
            ),
        ):
            out = reconcile_once(dry_run=True, override_hm="13:41")

        self.assertEqual(out["action"], "force_close_exception")
        saved = json.loads(self.ledger_path.read_text())
        self.assertEqual(saved["status"], "entered")  # NOT force_closed
        self.assertIn("force_close_exception", saved["last_action"])

    def test_entered_poll_query_failure_does_not_crash_or_falsely_advance(self):
        """委託回報查詢失敗時要回報 poll_query_failed，且絕不能誤判成 covered。"""
        _entered_ledger(self.ledger_path)
        with (
            mock.patch("order.dayflip_short_order._LEDGER_PATH", self.ledger_path),
            mock.patch("order.fubon_session.connect_fubon", return_value=object()),
            mock.patch("order.dayflip_short_order.pick_futopt_account", return_value=object()),
            mock.patch(
                "order.dayflip_short_order.get_futopt_order_results",
                side_effect=RuntimeError("broker session dropped"),
            ),
        ):
            out = reconcile_once(dry_run=True, override_hm="10:00")

        self.assertEqual(out["action"], "poll_query_failed")
        saved = json.loads(self.ledger_path.read_text())
        self.assertEqual(saved["status"], "entered")  # not silently advanced


class DayflipShortIdleToEnteredTest(unittest.TestCase):
    def setUp(self):
        self.ledger_path = Path("/tmp/test_dayflip_short_ledger_idle.json")
        self.ledger_path.unlink(missing_ok=True)

    def tearDown(self):
        self.ledger_path.unlink(missing_ok=True)

    def test_idle_with_qualifying_pick_places_entry_and_cover_then_enters(self):
        """idle→entered 全路徑（entry Sell market + cover Buy limit -2%）走一次。"""
        fake_candidate = mock.Mock(stock_id="2344")
        picked = {
            "candidate": fake_candidate,
            "live_symbol": "CLFH6",
            "live_name": "華邦電期",
            "open_px": 100.0,
            "t0_close": 93.0,
            "fgap": 0.0753,
            "margin": 27000.0,
            "margin_source": "broker_api",
        }
        with (
            mock.patch("order.dayflip_short_order._LEDGER_PATH", self.ledger_path),
            mock.patch("order.dayflip_short_order._prior_trading_day", return_value="2026-08-06"),
            mock.patch("order.dayflip_short_order.build_candidates", return_value=[fake_candidate]),
            mock.patch("order.fubon_session.connect_fubon", return_value=object()),
            mock.patch("order.dayflip_short_order.pick_futopt_account", return_value=object()),
            mock.patch("order.dayflip_short_order.pick_signal", return_value=picked),
            mock.patch(
                "order.dayflip_short_order.place_futopt_order",
                side_effect=[
                    {"ok": True, "data": {"order_no": "E1001"}},
                    {"ok": True, "data": {"order_no": "C2002"}},
                ],
            ) as mock_place,
        ):
            out = reconcile_once(dry_run=True, override_hm="08:45")

        self.assertEqual(out["action"], "entered")
        self.assertEqual(mock_place.call_count, 2)
        entry_call, cover_call = mock_place.call_args_list
        self.assertEqual(entry_call[0][1].buy_sell, "Sell")
        self.assertEqual(entry_call[0][1].order_type, "New")
        self.assertEqual(cover_call[0][1].buy_sell, "Buy")
        self.assertEqual(cover_call[0][1].order_type, "Close")
        self.assertAlmostEqual(cover_call[0][1].price, 98.0)  # 100 * (1-0.02)

        saved = json.loads(self.ledger_path.read_text())
        self.assertEqual(saved["status"], "entered")
        self.assertEqual(saved["entry_order_no"], "E1001")
        self.assertEqual(saved["cover_order_no"], "C2002")
        self.assertEqual(saved["margin_source"], "broker_api")

    def test_idle_entry_order_exception_stays_idle_no_crash(self):
        """CRITICAL 修正驗證：進場單本身送出就拋例外(尚未寫入任何持倉欄位)——
        必須維持 idle 讓下一次 poll 重新嘗試，不能 raise 讓整個 process 崩潰。"""
        fake_candidate = mock.Mock(stock_id="2344")
        picked = {
            "candidate": fake_candidate, "live_symbol": "CLFH6", "live_name": "華邦電期",
            "open_px": 100.0, "t0_close": 93.0, "fgap": 0.0753,
            "margin": 27000.0, "margin_source": "broker_api",
        }
        with (
            mock.patch("order.dayflip_short_order._LEDGER_PATH", self.ledger_path),
            mock.patch("order.dayflip_short_order._prior_trading_day", return_value="2026-08-06"),
            mock.patch("order.dayflip_short_order.build_candidates", return_value=[fake_candidate]),
            mock.patch("order.fubon_session.connect_fubon", return_value=object()),
            mock.patch("order.dayflip_short_order.pick_futopt_account", return_value=object()),
            mock.patch("order.dayflip_short_order.pick_signal", return_value=picked),
            mock.patch(
                "order.dayflip_short_order.place_futopt_order",
                side_effect=RuntimeError("network timeout placing entry"),
            ) as mock_place,
        ):
            out = reconcile_once(dry_run=True, override_hm="08:45")

        self.assertEqual(out["action"], "entry_exception")
        self.assertEqual(mock_place.call_count, 1)
        saved = json.loads(self.ledger_path.read_text())
        self.assertEqual(saved["status"], "idle")  # never advanced past idle
        self.assertIsNone(saved["stock_id"])  # no position fields written

    def test_idle_entry_succeeds_cover_exception_becomes_entered_not_lost(self):
        """CRITICAL 修正驗證：進場單成功但回補單送出拋例外——絕不能讓這筆真實
        部位在 ledger 上消失蹤跡（原本的 bug：整個 process 崩潰、ledger 停在
        idle，下一次 poll 會誤判成沒有部位而再送一張進場單）。必須立刻轉
        entered、cover_order_no 留 None 等下次 poll 重試。"""
        fake_candidate = mock.Mock(stock_id="2344")
        picked = {
            "candidate": fake_candidate, "live_symbol": "CLFH6", "live_name": "華邦電期",
            "open_px": 100.0, "t0_close": 93.0, "fgap": 0.0753,
            "margin": 27000.0, "margin_source": "broker_api",
        }
        with (
            mock.patch("order.dayflip_short_order._LEDGER_PATH", self.ledger_path),
            mock.patch("order.dayflip_short_order._prior_trading_day", return_value="2026-08-06"),
            mock.patch("order.dayflip_short_order.build_candidates", return_value=[fake_candidate]),
            mock.patch("order.fubon_session.connect_fubon", return_value=object()),
            mock.patch("order.dayflip_short_order.pick_futopt_account", return_value=object()),
            mock.patch("order.dayflip_short_order.pick_signal", return_value=picked),
            mock.patch(
                "order.dayflip_short_order.place_futopt_order",
                side_effect=[
                    {"ok": True, "data": {"order_no": "E1001"}},  # entry succeeds
                    RuntimeError("broker rejected cover order"),   # cover raises
                ],
            ) as mock_place,
        ):
            out = reconcile_once(dry_run=True, override_hm="08:45")

        self.assertEqual(out["action"], "entered_cover_pending")
        self.assertEqual(mock_place.call_count, 2)
        saved = json.loads(self.ledger_path.read_text())
        self.assertEqual(saved["status"], "entered")  # NOT idle, NOT lost
        self.assertEqual(saved["entry_order_no"], "E1001")
        self.assertIsNone(saved["cover_order_no"])
        self.assertEqual(saved["stock_id"], "2344")

    def test_idle_with_no_qualifying_pick_becomes_no_signal(self):
        fake_candidate = mock.Mock(stock_id="2344")
        with (
            mock.patch("order.dayflip_short_order._LEDGER_PATH", self.ledger_path),
            mock.patch("order.dayflip_short_order._prior_trading_day", return_value="2026-08-06"),
            mock.patch("order.dayflip_short_order.build_candidates", return_value=[fake_candidate]),
            mock.patch("order.fubon_session.connect_fubon", return_value=object()),
            mock.patch("order.dayflip_short_order.pick_signal", return_value=None),
            mock.patch("order.dayflip_short_order.place_futopt_order") as mock_place,
        ):
            out = reconcile_once(dry_run=True, override_hm="08:45")

        self.assertEqual(out["action"], "no_signal_gap_not_met")
        self.assertEqual(mock_place.call_count, 0)
        saved = json.loads(self.ledger_path.read_text())
        self.assertEqual(saved["status"], "no_signal")


class DayflipYamlSsotTest(unittest.TestCase):
    """2026-08-08修正驗證：MARGIN_CAP_NTD等常數真的是從config/order.yaml讀出來的，
    不是碰巧跟寫死的預設值一樣——改yaml內容要真的影響讀出來的值，證明SSOT接上了。"""

    def test_yaml_block_reads_real_config_order_yaml(self) -> None:
        block = _dayflip_yaml_block()
        # 這幾個 key 是 config/order.yaml 的 dayflip-futures-short 區塊本來就有的
        self.assertIn("margin_cap_twd", block)
        self.assertIn("cover_target_pct", block)
        self.assertIn("force_close_at", block)
        self.assertEqual(block["force_close_at"], "13:40")

    def test_block_computation_reflects_arbitrary_yaml_content(self) -> None:
        """不reload整個module（避免弄壞其他測試對這個module物件身分的假設）——直接
        呼叫_dayflip_yaml_block()驗證它是「讀load_order_config()回傳值」的函式，
        換一組假設定它就換一組結果，證明是真的接上去而非巧合printf出同一組數字。"""
        fake_cfg = {"strategies": {"dayflip-futures-short": {"margin_cap_twd": 999999}}}
        with mock.patch("order.dayflip_short_order.load_order_config", return_value=fake_cfg):
            block = _dayflip_yaml_block()
        self.assertEqual(block["margin_cap_twd"], 999999)

        # 拿掉整個 strategies block 也不會拋例外，回傳空dict（呼叫端fallback邏輯接手）
        with mock.patch("order.dayflip_short_order.load_order_config", return_value={}):
            empty_block = _dayflip_yaml_block()
        self.assertEqual(empty_block, {})


class DayflipTradeWindowWeekdaySafeTest(unittest.TestCase):
    """2026-08-08 Task 56：跟 tmf_channel.in_tmf_trade_window 同一天發現的同一類bug——
    只查 HH:MM 不查星期幾，KeepAlive worker 常駐後週六/週日會誤判成開盤日、對著沒開盤
    的市場 login-thrash。這裡直接釘住週末必須回 False，不論 HH:MM 落在窗內與否。"""

    def test_weekday_in_window_hours_true(self) -> None:
        for wd in range(5):  # Mon-Fri
            with self.subTest(weekday=wd):
                self.assertTrue(in_dayflip_short_trade_window("09:00", weekday=wd))

    def test_saturday_and_sunday_always_false_even_in_window_hours(self) -> None:
        for wd in (5, 6):  # Sat, Sun
            with self.subTest(weekday=wd):
                self.assertFalse(in_dayflip_short_trade_window("09:00", weekday=wd))

    def test_weekday_outside_window_hours_false(self) -> None:
        self.assertFalse(in_dayflip_short_trade_window("08:43", weekday=0))
        self.assertFalse(in_dayflip_short_trade_window("13:42", weekday=0))

    def test_boundary_inclusive(self) -> None:
        self.assertTrue(in_dayflip_short_trade_window("08:44", weekday=0))
        self.assertTrue(in_dayflip_short_trade_window("13:41", weekday=0))


class DayflipSessionPoolWiringTest(unittest.TestCase):
    """2026-08-08 Task 56：reconcile_once(use_session_pool=True) 必須走
    dayflip_short_session_pool.get_fubon_session()，不是每次都 connect_fubon() 全新
    登入——這是 KeepAlive worker 要解決的核心問題，沒測到就等於白改。"""

    def setUp(self):
        self.ledger_path = Path("/tmp/test_dayflip_short_pool_ledger.json")
        self.ledger_path.unlink(missing_ok=True)
        self.addCleanup(lambda: self.ledger_path.unlink(missing_ok=True))

    def test_entered_state_use_session_pool_calls_get_fubon_session_not_connect_fubon(self) -> None:
        _entered_ledger(self.ledger_path)
        unfilled = mock.Mock(order_no="C2002", status="PendingSubmit", filled_qty=0)
        pooled_session = object()
        with (
            mock.patch("order.dayflip_short_order._LEDGER_PATH", self.ledger_path),
            mock.patch(
                "order.dayflip_short_session_pool.get_fubon_session",
                return_value=pooled_session,
            ) as fake_pool,
            mock.patch("order.fubon_session.connect_fubon") as fake_connect,
            mock.patch("order.dayflip_short_order.pick_futopt_account", return_value=object()),
            mock.patch(
                "order.dayflip_short_order.get_futopt_order_results",
                return_value=[unfilled],
            ),
        ):
            out = reconcile_once(dry_run=True, override_hm="10:00", use_session_pool=True)

        self.assertEqual(out["action"], "waiting_cover")
        fake_pool.assert_called_once_with()
        fake_connect.assert_not_called()

    def test_explicit_session_param_takes_priority_over_session_pool(self) -> None:
        _entered_ledger(self.ledger_path)
        unfilled = mock.Mock(order_no="C2002", status="PendingSubmit", filled_qty=0)
        injected_session = object()
        with (
            mock.patch("order.dayflip_short_order._LEDGER_PATH", self.ledger_path),
            mock.patch("order.dayflip_short_session_pool.get_fubon_session") as fake_pool,
            mock.patch("order.fubon_session.connect_fubon") as fake_connect,
            mock.patch(
                "order.dayflip_short_order.pick_futopt_account",
                return_value=object(),
            ) as fake_pick_acct,
            mock.patch(
                "order.dayflip_short_order.get_futopt_order_results",
                return_value=[unfilled],
            ),
        ):
            out = reconcile_once(
                dry_run=True, override_hm="10:00", use_session_pool=True, session=injected_session,
            )

        self.assertEqual(out["action"], "waiting_cover")
        fake_pool.assert_not_called()
        fake_connect.assert_not_called()
        self.assertIs(fake_pick_acct.call_args[0][0], injected_session)

    def test_default_still_cold_starts_connect_fubon_when_pool_not_requested(self) -> None:
        """回歸測試：不傳 use_session_pool 時（原本 scripts/order/run_dayflip_short_poll.py
        的 StartInterval 冷啟動路徑）行為完全不變，仍然每次呼叫都 connect_fubon()。"""
        _entered_ledger(self.ledger_path)
        unfilled = mock.Mock(order_no="C2002", status="PendingSubmit", filled_qty=0)
        with (
            mock.patch("order.dayflip_short_order._LEDGER_PATH", self.ledger_path),
            mock.patch("order.dayflip_short_session_pool.get_fubon_session") as fake_pool,
            mock.patch("order.fubon_session.connect_fubon", return_value=object()) as fake_connect,
            mock.patch("order.dayflip_short_order.pick_futopt_account", return_value=object()),
            mock.patch(
                "order.dayflip_short_order.get_futopt_order_results",
                return_value=[unfilled],
            ),
        ):
            out = reconcile_once(dry_run=True, override_hm="10:00")

        self.assertEqual(out["action"], "waiting_cover")
        fake_connect.assert_called_once_with()
        fake_pool.assert_not_called()


if __name__ == "__main__":
    unittest.main()
