"""Unit tests · Leading Dip order isolation helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from order.leading_dip_cross_sleeve import (
    abc_ledger_open_symbols,
    blocked_symbols_for_leading_dip,
    c18acc_slot_symbols,
)
from order.leading_dip_ledger import LeadingDipLedger, load_ledger, save_ledger
from order.leading_dip_order import _exit_date, _pick_day_entries
from order.leading_dip_order_config import load_leading_dip_order_config


def test_c18_slot_symbols(tmp_path: Path) -> None:
    p = tmp_path / "slots.json"
    p.write_text(
        json.dumps({"slots": [{"stock_id": "2330"}, {"stock_id": "2454"}, {"stock_id": ""}]}),
        encoding="utf-8",
    )
    assert c18acc_slot_symbols(p) == {"2330", "2454"}


def test_abc_ledger_skips_sells_and_failed(tmp_path: Path) -> None:
    p = tmp_path / "abc.json"
    p.write_text(
        json.dumps(
            {
                "entries": [
                    {"symbol": "1101", "side": "buy", "lifecycle_status": "filled"},
                    {"symbol": "1102", "side": "sell", "lifecycle_status": "filled"},
                    {"symbol": "1103", "side": "buy", "lifecycle_status": "failed"},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert abc_ledger_open_symbols(p) == {"1101"}


def test_blocked_union(tmp_path: Path, monkeypatch) -> None:
    slots = tmp_path / "slots.json"
    slots.write_text(json.dumps({"slots": [{"stock_id": "A"}]}), encoding="utf-8")
    abc = tmp_path / "abc.json"
    abc.write_text(
        json.dumps({"entries": [{"symbol": "B", "side": "buy", "status": "filled"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("order.leading_dip_cross_sleeve.C18_SLOTS_PATH", slots)
    monkeypatch.setattr("order.leading_dip_cross_sleeve.ABC_LEDGER_PATH", abc)
    out = blocked_symbols_for_leading_dip(also_exclude={"C"})
    assert set(out["blocked"]) == {"A", "B", "C"}


def test_pick_skips_blocked_and_respects_room() -> None:
    day = pd.DataFrame(
        [
            {"sid": "1111", "ex0": -8.0, "rB_min": 0.0},
            {"sid": "2222", "ex0": -7.0, "rB_min": 0.0},
            {"sid": "3333", "ex0": -6.0, "rB_min": 0.0},
        ]
    )
    picks = _pick_day_entries(
        day,
        day_slot_cap_default=2,
        day_slot_cap_busy_crash=4,
        hard_day_cap=4,
        max_entries_per_day=4,
        blocked={"1111"},
        already_open=set(),
        room=2,
        already_today=0,
    )
    assert [p["sid"] for p in picks] == ["2222", "3333"]


def test_mid_pick_hard_cap_one() -> None:
    day = pd.DataFrame(
        [
            {"sid": "1111", "ex0": -8.0, "rB_min": -2.0},
            {"sid": "2222", "ex0": -7.0, "rB_min": -2.0},
            {"sid": "3333", "ex0": -6.0, "rB_min": -2.0},
        ]
    )
    picks = _pick_day_entries(
        day,
        day_slot_cap_default=1,
        day_slot_cap_busy_crash=2,
        hard_day_cap=2,
        max_entries_per_day=2,
        blocked=set(),
        already_open=set(),
        room=3,
        already_today=0,
    )
    # busy∧crash → expand 2, hard 2
    assert len(picks) == 2
    assert picks[0]["sid"] == "1111"


def test_exit_date_hold3() -> None:
    trading = ["2026-07-10", "2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16"]
    assert _exit_date("2026-07-10", 3, trading) == "2026-07-15"


def test_exit_date_extends_when_calendar_short() -> None:
    # Live same-day: kbar calendar ends at entry → weekday walk for T+3
    assert _exit_date("2026-07-16", 3, ["2026-07-15", "2026-07-16"]) == "2026-07-21"


def test_ledger_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "ld.json"
    led = LeadingDipLedger(positions=[{"symbol": "6505", "status": "open", "side": "buy"}])
    save_ledger(led, path=path)
    loaded = load_ledger(path)
    assert loaded.open_symbols() == {"6505"}


def test_failed_symbols_on_day() -> None:
    led = LeadingDipLedger(
        positions=[
            {"symbol": "6505", "side": "buy", "entry_date": "2026-07-27", "status": "failed"},
            {"symbol": "2492", "side": "buy", "entry_date": "2026-07-27", "status": "rejected"},
            {"symbol": "2330", "side": "buy", "entry_date": "2026-07-27", "status": "skipped"},
            {"symbol": "2454", "side": "buy", "entry_date": "2026-07-27", "status": "open"},
            {"symbol": "1101", "side": "buy", "entry_date": "2026-07-24", "status": "failed"},
        ]
    )
    # skipped (transient) stays retryable; open is not a failure; other days ignored.
    assert led.failed_symbols_on_day("2026-07-27") == {"6505", "2492"}


def test_submit_entry_records_failed_on_subprocess_crash(tmp_path: Path, monkeypatch) -> None:
    import dataclasses

    from order import fubon_subprocess
    from order.leading_dip_order import _submit_entry

    def _boom(_payload, **_kw):
        raise RuntimeError("fubon_subprocess_failed:connect refused")

    monkeypatch.setattr(fubon_subprocess, "host_needs_fubon_venv", lambda: True)
    monkeypatch.setattr(fubon_subprocess, "run_fubon_order_worker", _boom)

    conf = dataclasses.replace(
        load_leading_dip_order_config(),
        order_enabled=True,
        auto_submit=True,
        dry_run=False,
    )
    ledger_path = tmp_path / "ld.json"
    ledger = LeadingDipLedger()
    ent = _submit_entry(
        row={"sid": "6505", "minute": "09:10"},
        conf=conf,
        session_date="2026-07-27",
        poll_minute="09:39",
        trading_dates=["2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30"],
        ledger=ledger,
        ledger_path=ledger_path,
        dry=False,
        strategy_id="leading-dip",
        user_def=conf.user_def,
        budget_twd=20000,
        sleeve="main",
    )
    assert ent["status"] == "error"
    # A failed row is now persisted so the poll won't re-fire this symbol all day.
    reloaded = load_ledger(ledger_path)
    assert reloaded.failed_symbols_on_day("2026-07-27") == {"6505"}
    assert reloaded.entries_on_day("2026-07-27") == []  # failed does not count as an entry


def test_live_submit_path_imports_resolve() -> None:
    # _submit_entry imports these lazily inside the live path, so a missing symbol
    # (the old `fetch_available_balance`, which never existed) only crashed at real
    # submit time -> worker exit 1 -> no ledger -> re-fired every poll. Import them
    # here so the drift is caught by CI, not by a live order attempt.
    from order.abc_v3_f1_lifecycle import (
        outer_status_from_lifecycle,
        poll_order_lifecycle,
        reconcile_before_submit,
    )
    from order.chase_runner import chase_ask_price
    from order.fubon_account import bank_remain
    from order.fubon_orders import place_resolved_order
    from order.fubon_session import connect_fubon

    for fn in (
        bank_remain,
        place_resolved_order,
        connect_fubon,
        chase_ask_price,
        outer_status_from_lifecycle,
        poll_order_lifecycle,
        reconcile_before_submit,
    ):
        assert callable(fn)


def test_config_budget_and_mid_defaults(monkeypatch) -> None:
    monkeypatch.delenv("ORDER_LEADING_DIP_BUDGET_TWD", raising=False)
    monkeypatch.delenv("ORDER_LEADING_DIP_MID_ENABLED", raising=False)
    monkeypatch.delenv("ORDER_LEADING_DIP_MID_BUDGET_TWD", raising=False)
    cfg = load_leading_dip_order_config()
    assert cfg.budget_twd == 20000
    assert cfg.mid_enabled is True
    assert cfg.mid_budget_twd == 20000
    assert cfg.mid_minute_lo == "10:00"
    assert cfg.mid_total_open_cap == 3
