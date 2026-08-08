"""C18acc screen actions → Fubon auto-submit · sell-first · swap buy retry."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any, Literal

from order.account_cap_gate import can_afford_buy, load_account_risk_config, sort_actions_sell_first
from order.oms_lifecycle import (
    build_client_intent_id,
    entry_blocks_retry,
    entry_counts_toward_cash,
    outer_status_from_lifecycle,
    poll_order_lifecycle,
    reconcile_before_submit,
    refresh_open_ledger_entries,
)
from order.c18acc_order_config import C18accOrderConfig, load_c18acc_order_config
from order.c18acc_order_ledger import C18accOrderLedger, load_c18acc_order_ledger, save_c18acc_order_ledger
from order.chase import shares_for_budget
from order.intent import ResolvedOrder

ActionKind = Literal["entry", "swap", "max_hold_exit", "extension_exit", "pyramid_add"]


def _action_client_intent_id(
    *,
    cfg: C18accOrderConfig,
    session_date: str,
    poll_minute: str,
    symbol: str,
    kind: str,
    side: str,
    counterparty_id: str | None = None,
    pyramid_parent_cid: str | None = None,
) -> str:
    base = build_client_intent_id(
        strategy_id=cfg.user_def,
        session_date=session_date,
        poll_minute=poll_minute,
        symbol=symbol,
    )
    suffix = f"_{kind}_{side}"
    if counterparty_id:
        suffix += f"_to_{counterparty_id}"
    if pyramid_parent_cid:
        suffix += f"_pyr_{pyramid_parent_cid[-24:]}"
    return f"{base}{suffix}"


def _serialize_action(act: Any) -> dict[str, Any]:
    if hasattr(act, "kind"):
        return asdict(act)
    return dict(act)


def _deserialize_action(raw: dict[str, Any]) -> Any:
    from rrg_mono_swap_accel_screen import ScreenAction

    return ScreenAction(
        kind=raw["kind"],  # type: ignore[arg-type]
        stock_id=str(raw["stock_id"]),
        stock_name=str(raw.get("stock_name") or ""),
        side=raw["side"],  # type: ignore[arg-type]
        price=float(raw.get("price") or 0),
        quantity_shares=int(raw.get("quantity_shares") or 0),
        note=str(raw.get("note") or ""),
        counterparty_id=raw.get("counterparty_id"),
        counterparty_name=raw.get("counterparty_name"),
        pyramid_parent_cid=raw.get("pyramid_parent_cid"),
        slot=raw.get("slot"),
    )


def _resolved_from_action(
    act: Any,
    *,
    cfg: C18accOrderConfig,
    session_date: str,
    poll_minute: str,
    price: float,
    qty: int,
) -> ResolvedOrder:
    cid = _action_client_intent_id(
        cfg=cfg,
        session_date=session_date,
        poll_minute=poll_minute,
        symbol=act.stock_id,
        kind=act.kind,
        side=act.side,
        counterparty_id=act.counterparty_id,
        pyramid_parent_cid=act.pyramid_parent_cid,
    )
    price_type = "chase_bid" if act.side == "sell" else cfg.price_type
    return ResolvedOrder(
        symbol=act.stock_id,
        side=act.side,  # type: ignore[arg-type]
        quantity_shares=qty,
        price=f"{price:.2f}",
        price_type=price_type,  # type: ignore[arg-type]
        market_type=cfg.market_type,  # type: ignore[arg-type]
        time_in_force=cfg.time_in_force,  # type: ignore[arg-type]
        order_type="stock",
        user_def=cid,
        note=act.note,
        source="delta",
    )


def _append_entry(ledger: C18accOrderLedger, entry: dict[str, Any]) -> None:
    ledger.entries.append(entry)
    save_c18acc_order_ledger(ledger)


def _queue_pending_buy(
    ledger: C18accOrderLedger,
    *,
    action: dict[str, Any],
    session_date: str,
    poll_minute: str,
    reason: str,
    retries_left: int,
) -> None:
    ledger.pending_buys.append(
        {
            "action": action,
            "session_date": session_date,
            "poll_minute": poll_minute,
            "reason": reason,
            "retries_left": retries_left,
            "queued_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    save_c18acc_order_ledger(ledger)


def _submit_one(
    session: Any,
    act: Any,
    *,
    cfg: C18accOrderConfig,
    session_date: str,
    poll_minute: str,
    ledger: C18accOrderLedger,
    dry_run: bool,
    available_cash: int | None,
    holdings: dict[str, int],
    risk_cfg: Any,
) -> dict[str, Any]:
    from order.chase_runner import chase_ask_price, chase_bid_price
    from order.fubon_orders import apply_chase_prices, place_resolved_order

    symbol = str(act.stock_id).strip()
    cid = _action_client_intent_id(
        cfg=cfg,
        session_date=session_date,
        poll_minute=poll_minute,
        symbol=symbol,
        kind=act.kind,
        side=act.side,
        counterparty_id=act.counterparty_id,
        pyramid_parent_cid=act.pyramid_parent_cid,
    )
    existing = ledger.find_by_client_intent_id(cid)
    if existing is not None and entry_blocks_retry(existing):
        return {
            "kind": act.kind,
            "side": act.side,
            "symbol": symbol,
            "status": "skipped",
            "reason": f"idempotent:{cid}",
            "client_intent_id": cid,
        }

    if act.side == "sell":
        qty = min(int(act.quantity_shares), int(holdings.get(symbol) or 0))
        if qty <= 0:
            return {
                "kind": act.kind,
                "side": act.side,
                "symbol": symbol,
                "status": "skipped",
                "reason": "no_holdings",
                "client_intent_id": cid,
            }
        px = float(chase_bid_price(session, symbol))
    else:
        px = float(chase_ask_price(session, symbol))
        qty = int(act.quantity_shares)
        if px > 0 and cfg.budget_twd_per_slot > 0:
            # Always cap to slot budget · never trust a pre-filled qty alone
            budget_qty = shares_for_budget(cfg.budget_twd_per_slot, px)
            qty = budget_qty if qty <= 0 else min(qty, budget_qty)
        elif qty <= 0 and px > 0:
            qty = shares_for_budget(cfg.budget_twd_per_slot, px)
        if qty <= 0:
            return {
                "kind": act.kind,
                "side": act.side,
                "symbol": symbol,
                "status": "skipped",
                "reason": "qty_zero",
                "client_intent_id": cid,
            }
        notional = qty * px
        if available_cash is not None:
            ok, cap_reason = can_afford_buy(
                notional_twd=notional,
                available_balance=available_cash,
                cfg=risk_cfg,
            )
            if not ok:
                if act.kind == "swap" and act.side == "buy":
                    _queue_pending_buy(
                        ledger,
                        action=_serialize_action(act),
                        session_date=session_date,
                        poll_minute=poll_minute,
                        reason=cap_reason,
                        retries_left=risk_cfg.swap_buy_retry_polls,
                    )
                return {
                    "kind": act.kind,
                    "side": act.side,
                    "symbol": symbol,
                    "status": "deferred",
                    "reason": cap_reason,
                    "client_intent_id": cid,
                    "queued_swap_buy": act.kind == "swap",
                }

    resolved = _resolved_from_action(
        act, cfg=cfg, session_date=session_date, poll_minute=poll_minute, price=px, qty=qty
    )
    preview: dict[str, Any] = {
        "kind": act.kind,
        "action_kind": act.kind,
        "side": act.side,
        "symbol": symbol,
        "stock_name": act.stock_name or "",
        "status": "dry_run" if dry_run else "submitted",
        "client_intent_id": cid,
        "price": px,
        "ask_price": px,
        "quantity_shares": qty,
        "note": act.note,
        "session_date": session_date,
        "poll_minute": poll_minute,
        "counterparty_id": act.counterparty_id,
        "counterparty_name": act.counterparty_name,
    }
    if dry_run:
        preview["resolved"] = asdict(resolved)
        return preview

    chase_resolved = apply_chase_prices(session, [resolved])[0]
    reconciled = reconcile_before_submit(
        session,
        client_intent_id=cid,
        fallback_ask=float(chase_resolved.price or px),
        fallback_qty=qty,
    )
    if reconciled is not None:
        base = {
            "symbol": symbol,
            "session_date": session_date,
            "poll_minute": poll_minute,
            "client_intent_id": cid,
            "action_kind": act.kind,
            "side": act.side,
            "submitted_at": datetime.now().isoformat(timespec="seconds"),
            "note": act.note,
            "counterparty_id": act.counterparty_id,
            "pyramid_parent_cid": act.pyramid_parent_cid,
        }
        _append_entry(ledger, {**base, **reconciled})
        preview.update(reconciled)
        preview["status"] = "reconciled"
        return preview

    submit_result = place_resolved_order(session, chase_resolved)
    preview["submit_result"] = submit_result
    if not submit_result.get("is_success"):
        preview["status"] = "failed"
        preview["reason"] = str(submit_result.get("message") or "place_order_failed")
        return preview

    lifecycle = poll_order_lifecycle(
        session,
        client_intent_id=cid,
        order_no=str(submit_result.get("order_no") or "") or None,
        fallback_ask=float(chase_resolved.price or px),
        fallback_qty=qty,
        max_attempts=cfg.poll_max_attempts,
        interval_sec=cfg.poll_interval_sec,
    )
    outer = outer_status_from_lifecycle(str(lifecycle.get("lifecycle_status") or ""))
    base = {
        "symbol": symbol,
        "session_date": session_date,
        "poll_minute": poll_minute,
        "client_intent_id": cid,
        "action_kind": act.kind,
        "side": act.side,
        "submitted_at": datetime.now().isoformat(timespec="seconds"),
        "note": act.note,
        "counterparty_id": act.counterparty_id,
        "pyramid_parent_cid": act.pyramid_parent_cid,
        "status": outer,
    }
    _append_entry(ledger, {**base, **lifecycle})
    preview.update(lifecycle)
    preview["status"] = outer
    return preview


def _drain_pending_buys(
    session: Any,
    *,
    cfg: C18accOrderConfig,
    session_date: str,
    poll_minute: str,
    ledger: C18accOrderLedger,
    dry_run: bool,
    available_cash: int | None,
    risk_cfg: Any,
) -> tuple[list[dict[str, Any]], list[Any]]:
    results: list[dict[str, Any]] = []
    applied: list[Any] = []
    if dry_run:
        return results, applied
    from order.fubon_orders import holdings_shares_by_symbol

    holdings = holdings_shares_by_symbol(session)
    kept: list[dict[str, Any]] = []
    for pending in ledger.pending_buys:
        retries = int(pending.get("retries_left") or 0)
        if retries <= 0:
            results.append(
                {
                    "kind": "swap",
                    "side": "buy",
                    "status": "expired",
                    "reason": str(pending.get("reason") or "retry_exhausted"),
                }
            )
            continue
        act = _deserialize_action(pending["action"])
        row = _submit_one(
            session,
            act,
            cfg=cfg,
            session_date=session_date,
            poll_minute=poll_minute,
            ledger=ledger,
            dry_run=False,
            available_cash=available_cash,
            holdings=holdings,
            risk_cfg=risk_cfg,
        )
        row["pending_retry"] = True
        results.append(row)
        if row.get("status") in ("filled", "partial", "reconciled", "working", "ambiguous", "submitted"):
            applied.append(act)
            if available_cash is not None and entry_counts_toward_cash(row):
                available_cash = max(
                    0,
                    int(available_cash - float(row.get("notional_twd") or cfg.budget_twd_per_slot)),
                )
        elif row.get("status") == "deferred":
            pending["retries_left"] = retries - 1
            kept.append(pending)
        else:
            kept.append({**pending, "retries_left": retries - 1})
    ledger.pending_buys = kept
    save_c18acc_order_ledger(ledger)
    return results, applied


def process_c18acc_orders(
    actions: list[Any],
    *,
    session_date: str,
    poll_minute: str,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    """Submit C18acc screen actions · sell-first · defer swap buys on cash shortfall."""
    cfg = load_c18acc_order_config()
    risk_cfg = load_account_risk_config()

    if not cfg.order_enabled:
        return {
            "session_date": session_date,
            "poll_minute": poll_minute,
            "skipped_reason": "ORDER_C18ACC_ORDER_ENABLED=0",
            "results": [],
            "applied_actions": [],
        }

    if dry_run is None:
        dry_run = cfg.dry_run or not cfg.auto_submit

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
                "applied_actions": [],
            }

    if not dry_run and cfg.auto_submit and actions:
        from order.fubon_subprocess import host_needs_fubon_venv, run_fubon_order_worker

        if host_needs_fubon_venv():
            payload = {
                "kind": "c18acc_actions",
                "session_date": session_date,
                "poll_minute": poll_minute,
                "dry_run": False,
                "actions": [
                    {
                        "kind": getattr(act, "kind", ""),
                        "stock_id": getattr(act, "stock_id", ""),
                        "stock_name": getattr(act, "stock_name", "") or "",
                        "side": getattr(act, "side", ""),
                        "price": float(getattr(act, "price", 0) or 0),
                        "quantity_shares": int(getattr(act, "quantity_shares", 0) or 0),
                        "note": getattr(act, "note", "") or "",
                        "pyramid_parent_cid": getattr(act, "pyramid_parent_cid", None),
                        "slot": getattr(act, "slot", None),
                        "counterparty_id": getattr(act, "counterparty_id", None),
                        "counterparty_name": getattr(act, "counterparty_name", None),
                    }
                    for act in actions
                ],
            }
            try:
                out = run_fubon_order_worker(payload)
            except (FileNotFoundError, RuntimeError, OSError, ValueError) as exc:
                return {
                    "session_date": session_date,
                    "poll_minute": poll_minute,
                    "status": "error",
                    "reason": f"fubon_subprocess_failed:{exc}",
                    "results": [],
                    "applied_actions": [],
                }
            if isinstance(out, dict):
                # Worker cannot return live action objects; rebuild applied from
                # successful rows so APPLY_STATE still advances slots.
                applied_ids = {
                    str(r.get("symbol"))
                    for r in (out.get("results") or [])
                    if isinstance(r, dict)
                    and r.get("status")
                    in (
                        "submitted",
                        "reconciled",
                        "filled",
                        "partial",
                        "working",
                        "ambiguous",
                    )
                }
                out["applied_actions"] = [
                    a for a in actions if str(getattr(a, "stock_id", "")) in applied_ids
                ]
                return out
            return {
                "session_date": session_date,
                "poll_minute": poll_minute,
                "status": "error",
                "reason": "fubon_subprocess_bad_result",
                "results": [],
                "applied_actions": [],
            }

    ordered = sort_actions_sell_first(actions)
    results: list[dict[str, Any]] = []
    applied: list[Any] = []

    if dry_run:
        for act in ordered:
            results.append(
                {
                    "kind": act.kind,
                    "side": act.side,
                    "symbol": act.stock_id,
                    "status": "dry_run",
                    "note": act.note,
                }
            )
            applied.append(act)
        return {
            "session_date": session_date,
            "poll_minute": poll_minute,
            "dry_run": True,
            "priority": risk_cfg.priority_on_conflict,
            "results": results,
            "applied_actions": applied,
        }

    # No intents → do not open Fubon (avoids .venv missing fubon_neo on empty ticks).
    if not ordered:
        return {
            "session_date": session_date,
            "poll_minute": poll_minute,
            "dry_run": False,
            "priority": risk_cfg.priority_on_conflict,
            "results": [],
            "applied_actions": [],
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
    except (FileNotFoundError, RuntimeError, OSError, ValueError, ModuleNotFoundError) as exc:
        return {
            "session_date": session_date,
            "poll_minute": poll_minute,
            "status": "error",
            "reason": f"fubon_connect_failed:{exc}",
            "results": results,
            "applied_actions": [],
        }

    ledger = load_c18acc_order_ledger()
    assert session is not None
    if refresh_open_ledger_entries(session, ledger.entries, max_attempts=1, interval_sec=0.0):
        save_c18acc_order_ledger(ledger)

    for act in ordered:
        row = _submit_one(
            session,
            act,
            cfg=cfg,
            session_date=session_date,
            poll_minute=poll_minute,
            ledger=ledger,
            dry_run=False,
            available_cash=available_cash,
            holdings=holdings,
            risk_cfg=risk_cfg,
        )
        results.append(row)
        if row.get("status") in (
            "filled",
            "partial",
            "reconciled",
            "working",
            "ambiguous",
            "submitted",
            "dry_run",
        ):
            applied.append(act)
            if act.side == "sell" and entry_counts_toward_cash(row):
                sym = act.stock_id
                sold = int(row.get("filled_qty") or row.get("quantity_shares") or act.quantity_shares)
                holdings[sym] = max(0, int(holdings.get(sym) or 0) - sold)
                if available_cash is not None:
                    proceeds = float(row.get("notional_twd") or 0)
                    if proceeds > 0:
                        available_cash = int(available_cash + proceeds)
            elif act.side == "buy" and entry_counts_toward_cash(row):
                if available_cash is not None:
                    available_cash = max(
                        0,
                        int(available_cash - float(row.get("notional_twd") or cfg.budget_twd_per_slot)),
                    )

    pending_results, pending_applied = _drain_pending_buys(
        session,
        cfg=cfg,
        session_date=session_date,
        poll_minute=poll_minute,
        ledger=ledger,
        dry_run=False,
        available_cash=available_cash,
        risk_cfg=risk_cfg,
    )
    results.extend(pending_results)
    applied.extend(pending_applied)

    return {
        "session_date": session_date,
        "poll_minute": poll_minute,
        "dry_run": False,
        "priority": risk_cfg.priority_on_conflict,
        "results": results,
        "applied_actions": applied,
    }
