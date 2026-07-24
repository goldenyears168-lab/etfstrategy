"""Unit tests · Songshan copytrade gate + timed limit once_date."""

from __future__ import annotations

from pathlib import Path

from order.songshan_copytrade_ledger import (
    already_handled,
    append_entry,
    symbol_already_bought,
)
from order.songshan_copytrade_order import evaluate_25m_nonfail
from order.timed_limit_order import run_timed_limit_job


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


def test_resolve_quantity_from_budget() -> None:
    from order.timed_limit_order import resolve_quantity_shares

    assert resolve_quantity_shares({"quantity_shares": 1000, "price": "140.5"}) == 1000
    assert resolve_quantity_shares({"budget_twd": 10000, "price": "140.5"}) == 71
    assert resolve_quantity_shares({"budget_twd": 10000, "price": "710"}) == 14


def test_timed_limit_skip_wrong_date(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "order.timed_limit_order.STATE_PATH", tmp_path / "timed_state.json"
    )
    job = {
        "id": "sell_6451_462_20260723",
        "enabled": True,
        "once_date": "2026-07-23",
        "symbol": "6451",
        "side": "sell",
        "quantity_shares": 1000,
        "price": "462",
        "timeout_sec": 600,
    }
    out = run_timed_limit_job(job, session_date="2026-07-22", dry_run=True)
    assert out["status"] == "skip_date"


def test_timed_limit_dry_run_does_not_burn(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "order.timed_limit_order.STATE_PATH", tmp_path / "timed_state.json"
    )
    job = {
        "id": "sell_6451_462_20260723",
        "enabled": True,
        "once_date": "2026-07-23",
        "symbol": "6451",
        "side": "sell",
        "quantity_shares": 1000,
        "price": "462",
        "timeout_sec": 600,
    }
    out1 = run_timed_limit_job(job, session_date="2026-07-23", dry_run=True)
    assert out1["status"] == "dry_run"
    out2 = run_timed_limit_job(job, session_date="2026-07-23", dry_run=True)
    assert out2["status"] == "dry_run"
