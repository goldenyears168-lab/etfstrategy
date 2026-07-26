"""Unit tests · Songshan copytrade gate."""

from __future__ import annotations

from order.songshan_copytrade_ledger import (
    already_handled,
    append_entry,
    symbol_already_bought,
)
from order.songshan_copytrade_order import evaluate_25m_nonfail


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
