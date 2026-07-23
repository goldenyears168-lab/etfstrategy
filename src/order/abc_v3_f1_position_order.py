"""ABC v3+F1 position orders · champion TP sell + pyramid add buy auto-submit."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict
from datetime import datetime
from typing import Any

from order.abc_v3_f1_config import load_abc_v3_f1_order_config
from order.abc_v3_f1_lifecycle import (
    LIFECYCLE_FILLED,
    LIFECYCLE_PARTIAL,
    build_client_intent_id,
    entry_blocks_retry,
    entry_counts_toward_cash,
    outer_status_from_lifecycle,
    poll_order_lifecycle,
    reconcile_before_submit,
    refresh_open_ledger_entries,
)
from order.abc_v3_f1_monitor_config import (
    load_champion_tp_monitor_config,
    load_pyramid_add_monitor_config,
)
from order.abc_v3_f1_position_monitor import (
    PositionMonitorHit,
    scan_abc_v3_f1_positions,
)
from order.abc_v3_f1_reentry import AbcV3F1Ledger, load_ledger, load_tw_trading_dates, save_ledger
from order.chase import shares_for_budget
from order.intent import ResolvedOrder


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _exit_client_intent_id(*, parent_cid: str, session_date: str, poll_minute: str) -> str:
    stamp = session_date.replace("-", "")
    safe_poll = poll_minute.replace(":", "")
    return f"{parent_cid}_exit_{stamp}_{safe_poll}"


def _pyramid_client_intent_id(*, parent_cid: str, session_date: str, poll_minute: str) -> str:
    stamp = session_date.replace("-", "")
    safe_poll = poll_minute.replace(":", "")
    return f"{parent_cid}_pyramid_{stamp}_{safe_poll}"


def exit_done_for_parent(ledger: AbcV3F1Ledger, parent_cid: str) -> bool:
    for entry in ledger.entries:
        if str(entry.get("exit_parent_cid") or "") != parent_cid:
            continue
        if str(entry.get("order_kind") or "") not in (
            "champion_tp_sell",
            "hold_cap_sell",
            "exit_sell",
        ):
            continue
        if entry_blocks_retry(entry):
            return True
    return False


def position_monitor_enabled() -> bool:
    tp = load_champion_tp_monitor_config()
    py = load_pyramid_add_monitor_config()
    return tp.enabled or py.enabled


def position_auto_submit_enabled() -> bool:
    if not position_monitor_enabled():
        return False
    return _env_flag("ABC_V3_F1_POSITION_AUTO_SUBMIT", "0")


def _append_ledger(ledger: AbcV3F1Ledger, entry: dict[str, Any]) -> None:
    ledger.entries.append(entry)
    save_ledger(ledger)


def _submit_sell_hit(
    session: Any,
    *,
    hit: PositionMonitorHit,
    session_date: str,
    poll_minute: str,
    ledger: AbcV3F1Ledger,
    cfg: Any,
    dry_run: bool,
    holdings: dict[str, int],
) -> dict[str, Any]:
    from order.chase_runner import chase_bid_price
    from order.fubon_orders import place_resolved_order

    pos = hit.position
    parent_cid = pos.client_intent_id
    if exit_done_for_parent(ledger, parent_cid):
        return {
            "kind": hit.kind,
            "symbol": pos.symbol,
            "status": "skipped",
            "reason": f"exit_already_done:{parent_cid}",
        }

    exit_cid = _exit_client_intent_id(
        parent_cid=parent_cid, session_date=session_date, poll_minute=poll_minute
    )
    if ledger.find_by_client_intent_id(exit_cid) and entry_blocks_retry(
        ledger.find_by_client_intent_id(exit_cid) or {}
    ):
        return {
            "kind": hit.kind,
            "symbol": pos.symbol,
            "status": "skipped",
            "reason": f"idempotent:{exit_cid}",
        }

    qty = min(pos.quantity_shares, int(holdings.get(pos.symbol) or 0))
    if qty <= 0:
        return {
            "kind": hit.kind,
            "symbol": pos.symbol,
            "status": "skipped",
            "reason": "no_holdings",
            "ledger_qty": pos.quantity_shares,
            "holdings_qty": holdings.get(pos.symbol, 0),
        }

    bid = float(chase_bid_price(session, pos.symbol))
    resolved = ResolvedOrder(
        symbol=pos.symbol,
        side="sell",
        quantity_shares=qty,
        price=f"{bid:.2f}",
        price_type="limit",
        market_type=cfg.market_type,  # type: ignore[arg-type]
        time_in_force=cfg.time_in_force,  # type: ignore[arg-type]
        order_type="stock",
        user_def=exit_cid,
        note=f"champion_tp · parent={parent_cid} · {hit.reason}",
        source="delta",
    )
    preview: dict[str, Any] = {
        "kind": hit.kind,
        "symbol": pos.symbol,
        "status": "dry_run" if dry_run else "submitted",
        "client_intent_id": exit_cid,
        "exit_parent_cid": parent_cid,
        "bid_price": bid,
        "quantity_shares": qty,
        "reason": hit.reason,
        "resolved": asdict(resolved),
    }
    if dry_run:
        return preview

    reconciled = reconcile_before_submit(
        session,
        client_intent_id=exit_cid,
        fallback_ask=bid,
        fallback_qty=qty,
    )
    if reconciled is not None:
        base = {
            "symbol": pos.symbol,
            "session_date": session_date,
            "poll_minute": poll_minute,
            "client_intent_id": exit_cid,
            "exit_parent_cid": parent_cid,
            "order_kind": hit.kind,
            "strategy_id": pos.strategy_id,
            "submitted_at": datetime.now().isoformat(timespec="seconds"),
            "reason": hit.reason,
        }
        _append_ledger(ledger, {**base, **reconciled})
        preview.update(reconciled)
        preview["status"] = "reconciled"
        return preview

    submit_result = place_resolved_order(session, resolved)
    preview["submit_result"] = submit_result
    if not submit_result.get("is_success"):
        preview["status"] = "failed"
        preview["reason"] = str(submit_result.get("message") or "place_order_failed")
        return preview

    lifecycle = poll_order_lifecycle(
        session,
        client_intent_id=exit_cid,
        order_no=str(submit_result.get("order_no") or "") or None,
        fallback_ask=bid,
        fallback_qty=qty,
        max_attempts=cfg.poll_max_attempts,
        interval_sec=cfg.poll_interval_sec,
    )
    outer = outer_status_from_lifecycle(str(lifecycle.get("lifecycle_status") or ""))
    base = {
        "symbol": pos.symbol,
        "session_date": session_date,
        "poll_minute": poll_minute,
        "client_intent_id": exit_cid,
        "exit_parent_cid": parent_cid,
        "order_kind": hit.kind,
        "strategy_id": pos.strategy_id,
        "submitted_at": datetime.now().isoformat(timespec="seconds"),
        "reason": hit.reason,
        "status": outer,
    }
    _append_ledger(ledger, {**base, **lifecycle})
    preview.update(lifecycle)
    preview["status"] = outer
    return preview


def _submit_pyramid_hit(
    session: Any,
    *,
    hit: PositionMonitorHit,
    session_date: str,
    poll_minute: str,
    ledger: AbcV3F1Ledger,
    cfg: Any,
    pyramid_cfg: Any,
    dry_run: bool,
    available_cash: int | None,
) -> dict[str, Any]:
    from order.account_cap_gate import can_afford_buy
    from order.chase_runner import chase_ask_price
    from order.fubon_orders import cancel_open_buys_for_symbols, place_resolved_order

    pos = hit.position
    parent_cid = pos.client_intent_id
    pyramid_cid = _pyramid_client_intent_id(
        parent_cid=parent_cid, session_date=session_date, poll_minute=poll_minute
    )
    existing = ledger.find_by_client_intent_id(pyramid_cid)
    if existing is not None and entry_blocks_retry(existing):
        return {
            "kind": hit.kind,
            "symbol": pos.symbol,
            "status": "skipped",
            "reason": f"idempotent:{pyramid_cid}",
        }

    ask = float(chase_ask_price(session, pos.symbol))
    budget = int(round(cfg.budget_twd * pyramid_cfg.budget_fraction))
    qty = shares_for_budget(budget, ask)
    notional = qty * ask
    if available_cash is not None:
        ok, cap_reason = can_afford_buy(notional_twd=notional, available_balance=available_cash)
        if not ok:
            return {
                "kind": hit.kind,
                "symbol": pos.symbol,
                "status": "skipped",
                "reason": cap_reason,
                "ask_price": ask,
                "budget_twd": budget,
            }

    resolved = ResolvedOrder(
        symbol=pos.symbol,
        side="buy",
        quantity_shares=qty,
        price=f"{ask:.2f}",
        price_type="limit",
        market_type=cfg.market_type,  # type: ignore[arg-type]
        time_in_force=cfg.time_in_force,  # type: ignore[arg-type]
        order_type="stock",
        user_def=pyramid_cid,
        note=f"pyramid_add · parent={parent_cid} · {hit.reason}",
        source="delta",
    )
    preview: dict[str, Any] = {
        "kind": hit.kind,
        "symbol": pos.symbol,
        "status": "dry_run" if dry_run else "submitted",
        "client_intent_id": pyramid_cid,
        "pyramid_parent_cid": parent_cid,
        "ask_price": ask,
        "quantity_shares": qty,
        "budget_twd": budget,
        "notional_twd": round(notional, 2),
        "reason": hit.reason,
        "resolved": asdict(resolved),
    }
    if dry_run:
        return preview

    reconciled = reconcile_before_submit(
        session,
        client_intent_id=pyramid_cid,
        fallback_ask=ask,
        fallback_qty=qty,
    )
    if reconciled is not None:
        base = {
            "symbol": pos.symbol,
            "session_date": session_date,
            "poll_minute": poll_minute,
            "client_intent_id": pyramid_cid,
            "pyramid_parent_cid": parent_cid,
            "order_kind": "pyramid_add_buy",
            "strategy_id": pos.strategy_id,
            "budget_twd": budget,
            "submitted_at": datetime.now().isoformat(timespec="seconds"),
            "reason": hit.reason,
        }
        _append_ledger(ledger, {**base, **reconciled})
        preview.update(reconciled)
        preview["status"] = "reconciled"
        return preview

    if cfg.cancel_open_buys:
        preview["cancelled_open_buys"] = cancel_open_buys_for_symbols(session, {pos.symbol})

    submit_result = place_resolved_order(session, resolved)
    preview["submit_result"] = submit_result
    if not submit_result.get("is_success"):
        preview["status"] = "failed"
        preview["reason"] = str(submit_result.get("message") or "place_order_failed")
        return preview

    lifecycle = poll_order_lifecycle(
        session,
        client_intent_id=pyramid_cid,
        order_no=str(submit_result.get("order_no") or "") or None,
        fallback_ask=ask,
        fallback_qty=qty,
        max_attempts=cfg.poll_max_attempts,
        interval_sec=cfg.poll_interval_sec,
    )
    outer = outer_status_from_lifecycle(str(lifecycle.get("lifecycle_status") or ""))
    base = {
        "symbol": pos.symbol,
        "session_date": session_date,
        "poll_minute": poll_minute,
        "client_intent_id": pyramid_cid,
        "pyramid_parent_cid": parent_cid,
        "order_kind": "pyramid_add_buy",
        "strategy_id": pos.strategy_id,
        "budget_twd": budget,
        "submitted_at": datetime.now().isoformat(timespec="seconds"),
        "reason": hit.reason,
        "status": outer,
    }
    _append_ledger(ledger, {**base, **lifecycle})
    preview.update(lifecycle)
    preview["status"] = outer
    return preview


def process_abc_v3_f1_position_orders(
    *,
    session_date: str,
    poll_minute: str,
    conn: sqlite3.Connection | None = None,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    """Scan open ABC positions · optional champion TP sell / pyramid buy submit.

    Order sleeve removed 2026-07-16. Requires ABC_V3_F1_ORDER_FORCE_LEGACY=1 for
    library/tests only — not a live ops path.
    """
    import os

    force_legacy = os.environ.get("ABC_V3_F1_ORDER_FORCE_LEGACY", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if not force_legacy:
        return {
            "session_date": session_date,
            "poll_minute": poll_minute,
            "status": "retired",
            "reason": "abc_order_removed:ABC removed from Order layer 2026-07-16",
            "results": [],
        }

    champion_cfg = load_champion_tp_monitor_config()
    pyramid_cfg = load_pyramid_add_monitor_config()
    order_cfg = load_abc_v3_f1_order_config()

    if not champion_cfg.enabled and not pyramid_cfg.enabled:
        return {
            "session_date": session_date,
            "poll_minute": poll_minute,
            "skipped_reason": "monitor_disabled",
            "results": [],
        }

    if dry_run is None:
        dry_run = not position_auto_submit_enabled()

    if not dry_run:
        from order.live_submit_guard import live_submit_block_reason

        blocked = live_submit_block_reason(session_date=session_date)
        if blocked:
            return {
                "session_date": session_date,
                "poll_minute": poll_minute,
                "status": "error",
                "reason": blocked,
                "results": [],
            }

    if not dry_run and position_auto_submit_enabled():
        from order.fubon_subprocess import host_needs_fubon_venv, run_fubon_order_worker

        if host_needs_fubon_venv():
            try:
                out = run_fubon_order_worker(
                    {
                        "kind": "abc_v3_f1_position",
                        "session_date": session_date,
                        "poll_minute": poll_minute,
                        "dry_run": False,
                    }
                )
            except (FileNotFoundError, RuntimeError, OSError, ValueError) as exc:
                return {
                    "session_date": session_date,
                    "poll_minute": poll_minute,
                    "status": "error",
                    "reason": f"fubon_subprocess_failed:{exc}",
                    "results": [],
                }
            return out if isinstance(out, dict) else {
                "session_date": session_date,
                "poll_minute": poll_minute,
                "status": "error",
                "reason": "fubon_subprocess_bad_result",
                "results": [],
            }

    own_conn = False
    if conn is None:
        from stock_db import DEFAULT_DB_PATH, connect

        conn = connect(DEFAULT_DB_PATH)
        own_conn = True

    ledger = load_ledger()
    try:
        trading_dates = load_tw_trading_dates(conn)
        scan = scan_abc_v3_f1_positions(
            conn,
            session_date=session_date,
            poll_minute=poll_minute,
            trading_dates=trading_dates,
            ledger=ledger,
            champion_cfg=champion_cfg,
            pyramid_cfg=pyramid_cfg,
        )
        scan.hits = [
            h
            for h in scan.hits
            if h.kind not in ("champion_tp_sell", "hold_cap_sell")
            or not exit_done_for_parent(ledger, h.position.client_intent_id)
        ]
    finally:
        if own_conn and conn is not None:
            conn.close()

    results: list[dict[str, Any]] = []
    if not scan.hits:
        return {
            "session_date": session_date,
            "poll_minute": poll_minute,
            "open_n": len(scan.open_positions),
            "hits_n": 0,
            "skipped_reason": scan.skipped_reason,
            "dry_run": dry_run,
            "results": results,
        }

    if dry_run:
        for hit in scan.hits:
            if hit.kind in ("champion_tp_sell", "hold_cap_sell"):
                results.append(
                    {
                        "kind": hit.kind,
                        "symbol": hit.position.symbol,
                        "status": "dry_run",
                        "reason": hit.reason,
                        "client_intent_id": hit.position.client_intent_id,
                    }
                )
            else:
                results.append(
                    {
                        "kind": hit.kind,
                        "symbol": hit.position.symbol,
                        "status": "dry_run",
                        "reason": hit.reason,
                        "pyramid_parent_cid": hit.position.client_intent_id,
                    }
                )
        return {
            "session_date": session_date,
            "poll_minute": poll_minute,
            "open_n": len(scan.open_positions),
            "hits_n": len(scan.hits),
            "dry_run": True,
            "results": results,
        }

    session = None
    available_cash: int | None = None
    holdings: dict[str, int] = {}
    try:
        from order.fubon_account import bank_remain
        from order.fubon_orders import holdings_shares_by_symbol
        from order.fubon_session import connect_fubon

        session = connect_fubon()
        cash = bank_remain(session)
        available_cash = int(cash.get("available_balance") or cash.get("balance") or 0)
        holdings = holdings_shares_by_symbol(session)
    except (FileNotFoundError, RuntimeError, OSError, ValueError) as exc:
        return {
            "session_date": session_date,
            "poll_minute": poll_minute,
            "open_n": len(scan.open_positions),
            "hits_n": len(scan.hits),
            "status": "error",
            "reason": f"fubon_connect_failed:{exc}",
            "results": results,
        }

    assert session is not None
    if refresh_open_ledger_entries(session, ledger.entries, max_attempts=1, interval_sec=0.0):
        save_ledger(ledger)
    sells = [h for h in scan.hits if h.kind in ("champion_tp_sell", "hold_cap_sell")]
    buys = [h for h in scan.hits if h.kind == "pyramid_add_buy"]

    for hit in sells:
        row = _submit_sell_hit(
            session,
            hit=hit,
            session_date=session_date,
            poll_minute=poll_minute,
            ledger=ledger,
            cfg=order_cfg,
            dry_run=False,
            holdings=holdings,
        )
        results.append(row)
        if entry_counts_toward_cash(row):
            sym = hit.position.symbol
            sold = int(row.get("filled_qty") or row.get("quantity_shares") or 0)
            holdings[sym] = max(0, int(holdings.get(sym) or 0) - sold)

    for hit in buys:
        row = _submit_pyramid_hit(
            session,
            hit=hit,
            session_date=session_date,
            poll_minute=poll_minute,
            ledger=ledger,
            cfg=order_cfg,
            pyramid_cfg=pyramid_cfg,
            dry_run=False,
            available_cash=available_cash,
        )
        results.append(row)
        if available_cash is not None and entry_counts_toward_cash(row):
            available_cash = max(
                0,
                int(available_cash - float(row.get("notional_twd") or order_cfg.budget_twd)),
            )

    return {
        "session_date": session_date,
        "poll_minute": poll_minute,
        "open_n": len(scan.open_positions),
        "hits_n": len(scan.hits),
        "dry_run": False,
        "results": results,
    }
