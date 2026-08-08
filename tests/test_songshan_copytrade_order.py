"""Unit tests · Songshan copytrade gate."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from order.songshan_copytrade_config import SongshanCopytradeOrderConfig
from order.songshan_copytrade_ledger import (
    already_handled,
    append_entry,
    load_ledger,
    symbol_already_bought,
)
from order.songshan_copytrade_order import (
    evaluate_25m_nonfail,
    process_songshan_copytrade_poll,
)


def test_evaluate_25m_nonfail_pass() -> None:
    g = evaluate_25m_nonfail(100.0, 100.5, 100.2)
    assert g["fail"] is False
    assert g["reason"] == "nonfail"


def test_evaluate_25m_nonfail_fail_break() -> None:
    g = evaluate_25m_nonfail(100.0, 101.5, 99.5)
    assert g["fail"] is True
    assert g["reason"] == "fail_break"
    assert g["popped"] is True


def test_evaluate_25m_nonfail_pop_but_hold_open_ok() -> None:
    g = evaluate_25m_nonfail(100.0, 101.5, 100.0)
    assert g["fail"] is False


def test_ledger_already_handled() -> None:
    ledger: dict = {"entries": []}
    assert already_handled(ledger, signal_date="2026-07-22", symbol="2492") is False
    append_entry(
        ledger,
        {
            "signal_date": "2026-07-22",
            "symbol": "2492",
            "status": "skipped_fail",
        },
    )
    assert already_handled(ledger, signal_date="2026-07-22", symbol="2492") is True
    assert already_handled(ledger, signal_date="2026-07-22", symbol="2330") is False


def test_ledger_submit_failed_burns_slot() -> None:
    """Broker reject must not re-submit every 5m inside 09:25–09:40."""
    ledger: dict = {"entries": []}
    append_entry(
        ledger,
        {
            "signal_date": "2026-07-22",
            "symbol": "2492",
            "status": "submit_failed",
            "broker": {"is_success": False, "message": "全額處置股"},
        },
    )
    assert already_handled(ledger, signal_date="2026-07-22", symbol="2492") is True


def test_symbol_already_bought_blocks_new_signal_day() -> None:
    """Same symbol on a later signal_date must not rebuy unless pyramid allowed."""
    ledger: dict = {"entries": []}
    assert symbol_already_bought(ledger, symbol="2492") is False
    append_entry(
        ledger,
        {
            "signal_date": "2026-07-20",
            "entry_date": "2026-07-21",
            "symbol": "2492",
            "status": "submitted",
            "quantity_shares": 500,
        },
    )
    assert symbol_already_bought(ledger, symbol="2492") is True
    assert symbol_already_bought(ledger, symbol="2330") is False
    # Fail / reject do not count as bought
    ledger2: dict = {"entries": []}
    append_entry(
        ledger2,
        {"signal_date": "2026-07-22", "symbol": "2492", "status": "submit_failed"},
    )
    append_entry(
        ledger2,
        {"signal_date": "2026-07-22", "symbol": "2330", "status": "skipped_fail"},
    )
    assert symbol_already_bought(ledger2, symbol="2492") is False
    assert symbol_already_bought(ledger2, symbol="2330") is False


def _cfg(ledger_path: Path, **overrides) -> SongshanCopytradeOrderConfig:
    base = dict(
        enabled=True, dry_run=False, auto_submit=True,
        budget_twd=100_000.0, quantity_shares=None,
        branch_id="9217", buy_floor=50_000_000, net_min=0.95,
        entry_after="09:25", entry_until="09:40",
        price_type="chase_ask", market_type="intraday_odd", user_def="ss5d",
        ledger_path=ledger_path, mega_path=Path("/nonexistent_mega.json"),
        hold_days=7, submit_notify=False, allow_pyramid=False,
        poll_max_attempts=3, poll_interval_sec=0.0,
    )
    base.update(overrides)
    return SongshanCopytradeOrderConfig(**base)


def test_broker_exception_on_submit_burns_slot_no_double_submit() -> None:
    """2026-08-08 code review 修正驗證：place_resolved_order 拋例外(不是回傳
    is_success=False)時，必須被當成submit_failed記錄進ledger並燒掉這個slot——
    不能讓整支函式崩潰導致同一個訊號在同一個進場窗口內被重複嘗試買進。"""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "ledger.json"
        cfg = _cfg(ledger_path)
        candidate = {"stock_id": "2492", "buy_5d": 6e7, "net_ratio": 0.97, "day_whale": True}

        with (
            mock.patch("order.songshan_copytrade_order.prev_trading_day",
                      return_value="2026-07-21"),
            mock.patch("order.songshan_copytrade_order._scan_candidates",
                      return_value=[candidate]),
            mock.patch("order.fubon_session.connect_fubon", return_value=object()),
            mock.patch("order.songshan_copytrade_order.live_25m_nonfail",
                      return_value={"ok": True, "fail": False, "reason": "nonfail"}),
            mock.patch("order.chase_runner.chase_ask_price", return_value=50.0),
            mock.patch("order.fubon_account.bank_remain",
                      return_value={"available_balance": 500_000}),
            mock.patch("order.songshan_copytrade_order.reconcile_before_submit",
                      return_value=None),
            mock.patch("order.fubon_orders.place_resolved_order",
                      side_effect=RuntimeError("broker connection reset")),
        ):
            out1 = process_songshan_copytrade_poll(
                session_date="2026-07-22", poll_minute="09:30",
                conn=mock.Mock(), dry_run=False, cfg=cfg,
            )
            # simulate the next 5m poll tick inside the same entry window
            out2 = process_songshan_copytrade_poll(
                session_date="2026-07-22", poll_minute="09:35",
                conn=mock.Mock(), dry_run=False, cfg=cfg,
            )

        assert out1["status"] == "submit_failed"
        assert out1["entries"][0]["broker"]["exception"] is True
        # second poll must NOT re-attempt the same (signal_date, symbol)
        assert out2["status"] == "already_handled"

        ledger = load_ledger(ledger_path)
        matching = [e for e in ledger["entries"]
                   if e.get("signal_date") == "2026-07-21" and e.get("symbol") == "2492"]
        assert len(matching) == 1  # not double-appended


def test_client_intent_id_stable_across_poll_ticks_in_entry_window() -> None:
    """2026-08-08 Task 54: cid 必須用 cfg.entry_after（固定 config 值）組，不能用
    當下真實 poll_minute——09:25-09:40 窗口內每 ~5 分鐘會重新 poll 到同一訊號，
    若 cid 隨 poll_minute 變動，reconcile_before_submit 在下一個 tick 就永遠找不到
    前一 tick 送出的訂單，broker 端冪等檢查形同虛設。這裡用 dry_run 模式驗證同一
    (signal_date, symbol) 在不同 poll_minute 下產生完全相同的 client_intent_id。"""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "ledger.json"
        cfg = _cfg(ledger_path, dry_run=True)
        candidate = {"stock_id": "2492", "buy_5d": 6e7, "net_ratio": 0.97, "day_whale": True}

        with (
            mock.patch("order.songshan_copytrade_order.prev_trading_day",
                      return_value="2026-07-21"),
            mock.patch("order.songshan_copytrade_order._scan_candidates",
                      return_value=[candidate]),
            mock.patch("order.fubon_session.connect_fubon", return_value=object()),
            mock.patch("order.songshan_copytrade_order.live_25m_nonfail",
                      return_value={"ok": True, "fail": False, "reason": "nonfail"}),
            mock.patch("order.chase_runner.chase_ask_price", return_value=50.0),
        ):
            out_early = process_songshan_copytrade_poll(
                session_date="2026-07-22", poll_minute="09:25",
                conn=mock.Mock(), dry_run=True, cfg=cfg,
            )
            out_late = process_songshan_copytrade_poll(
                session_date="2026-07-22", poll_minute="09:35",
                conn=mock.Mock(), dry_run=True, cfg=cfg,
            )

        cid_early = out_early["entries"][0]["client_intent_id"]
        cid_late = out_late["entries"][0]["client_intent_id"]
        assert cid_early == cid_late
        assert cid_early == "ss5d_20260721_0925_2492"


def test_reconcile_before_submit_skips_resubmit_when_broker_already_has_order() -> None:
    """2026-08-08 Task 54: broker 端已經有這個 client_intent_id 的訂單時（例如上一個
    poll tick 已經送出但 ledger 沒寫入就被中斷），reconcile_before_submit 必須擋下
    place_resolved_order 的重複呼叫——這是 shared OMS lifecycle 補上的 broker 端
    冪等檢查，跟 leading-dip/detach-gate 同一套手法。"""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "ledger.json"
        cfg = _cfg(ledger_path)
        candidate = {"stock_id": "2492", "buy_5d": 6e7, "net_ratio": 0.97, "day_whale": True}
        reconciled_fields = {
            "order_no": "R1001",
            "broker_status": 10,
            "lifecycle_status": "working",
            "filled_qty": 0,
            "quantity_shares": 2000,
            "avg_fill_price": None,
            "notional_twd": 0.0,
        }

        with (
            mock.patch("order.songshan_copytrade_order.prev_trading_day",
                      return_value="2026-07-21"),
            mock.patch("order.songshan_copytrade_order._scan_candidates",
                      return_value=[candidate]),
            mock.patch("order.fubon_session.connect_fubon", return_value=object()),
            mock.patch("order.songshan_copytrade_order.live_25m_nonfail",
                      return_value={"ok": True, "fail": False, "reason": "nonfail"}),
            mock.patch("order.chase_runner.chase_ask_price", return_value=50.0),
            mock.patch("order.fubon_account.bank_remain",
                      return_value={"available_balance": 500_000}),
            mock.patch("order.songshan_copytrade_order.reconcile_before_submit",
                      return_value=reconciled_fields) as fake_reconcile,
            mock.patch("order.fubon_orders.place_resolved_order") as fake_place,
        ):
            out = process_songshan_copytrade_poll(
                session_date="2026-07-22", poll_minute="09:30",
                conn=mock.Mock(), dry_run=False, cfg=cfg,
            )

        fake_place.assert_not_called()
        assert fake_reconcile.call_args.kwargs.get("client_intent_id") == "ss5d_20260721_0925_2492"
        assert out["status"] == "submitted"
        assert out["entries"][0]["broker"]["reconciled"] is True


def test_cash_gate_skips_when_below_reserved_buffer_and_retries_when_cash_frees_up() -> None:
    """2026-08-08 Task 55: songshan 原本完全不查帳戶現金，account_risk.reserved_cash_twd
    這個帳戶級緩衝只有 c18acc/leading-dip 真的有接。這裡驗證補上的 pre-flight cash gate：
    (1) 現金不足時 status=skipped_cash、不呼叫 place_resolved_order；(2) 這個 skip 不燒
    slot——不在 _HANDLED_STATUSES 裡，下一個 poll tick 現金若足夠會正常重試送單
    （跟 leading-dip 的 cash-skip 可重試設計一致，不是像 skipped_fail 那樣終局封鎖）。"""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "ledger.json"
        cfg = _cfg(ledger_path)
        candidate = {"stock_id": "2492", "buy_5d": 6e7, "net_ratio": 0.97, "day_whale": True}

        with (
            mock.patch("order.songshan_copytrade_order.prev_trading_day",
                      return_value="2026-07-21"),
            mock.patch("order.songshan_copytrade_order._scan_candidates",
                      return_value=[candidate]),
            mock.patch("order.fubon_session.connect_fubon", return_value=object()),
            mock.patch("order.songshan_copytrade_order.live_25m_nonfail",
                      return_value={"ok": True, "fail": False, "reason": "nonfail"}),
            mock.patch("order.chase_runner.chase_ask_price", return_value=50.0),
            mock.patch("order.fubon_account.bank_remain",
                      return_value={"available_balance": 40_000}),
            mock.patch("order.fubon_orders.place_resolved_order") as fake_place,
        ):
            out1 = process_songshan_copytrade_poll(
                session_date="2026-07-22", poll_minute="09:25",
                conn=mock.Mock(), dry_run=False, cfg=cfg,
            )

        fake_place.assert_not_called()
        assert out1["status"] == "skipped_cash"
        assert already_handled(load_ledger(ledger_path), signal_date="2026-07-21", symbol="2492") is False

        with (
            mock.patch("order.songshan_copytrade_order.prev_trading_day",
                      return_value="2026-07-21"),
            mock.patch("order.songshan_copytrade_order._scan_candidates",
                      return_value=[candidate]),
            mock.patch("order.fubon_session.connect_fubon", return_value=object()),
            mock.patch("order.songshan_copytrade_order.live_25m_nonfail",
                      return_value={"ok": True, "fail": False, "reason": "nonfail"}),
            mock.patch("order.chase_runner.chase_ask_price", return_value=50.0),
            mock.patch("order.fubon_account.bank_remain",
                      return_value={"available_balance": 500_000}),
            mock.patch("order.songshan_copytrade_order.reconcile_before_submit",
                      return_value=None),
            mock.patch("order.fubon_orders.place_resolved_order",
                      return_value={"is_success": True, "order_no": "R2001"}),
        ):
            out2 = process_songshan_copytrade_poll(
                session_date="2026-07-22", poll_minute="09:30",
                conn=mock.Mock(), dry_run=False, cfg=cfg,
            )

        assert out2["status"] == "submitted"
