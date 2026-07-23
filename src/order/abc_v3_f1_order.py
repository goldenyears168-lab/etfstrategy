"""ABC v3+f1 · radar 新命中 → intent + 自動 chase_ask 送單（Order layer）。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any, Protocol

from abc_v3_f1_intent_bridge import (
    build_client_intent_id,
    build_intent_payload,
    quarantine_intent_file,
    update_intent_submit_status,
    write_intent_file,
)
from order.abc_v3_f1_config import ABC_V3_F1_POOL_ID, AbcV3F1OrderConfig, load_abc_v3_f1_order_config
from order.abc_v3_f1_lifecycle import (
    build_client_intent_id as _lifecycle_cid,
    entry_blocks_retry,
    entry_counts_toward_cash,
    outer_status_from_lifecycle,
    poll_order_lifecycle,
    reconcile_before_submit,
    refresh_open_ledger_entries,
)
from order.abc_v3_f1_reentry import (
    AbcV3F1Ledger,
    evaluate_reentry,
    load_ledger,
    load_tw_trading_dates,
    save_ledger,
)
from order.chase import shares_for_budget
from order.intent import ResolvedOrder


class _BuySignalLike(Protocol):
    stock_id: str
    stock_name: str
    action: str
    reason: str
    price: float
    poll_minute: str
    pool_id: str


def _client_intent_id(
    *,
    cfg: AbcV3F1OrderConfig,
    session_date: str,
    poll_minute: str,
    symbol: str,
) -> str:
    return _lifecycle_cid(
        strategy_id=cfg.user_def,
        session_date=session_date,
        poll_minute=poll_minute,
        symbol=symbol,
    )


def _build_resolved_buy(
    *,
    symbol: str,
    ask_price: float,
    budget_twd: int,
    cfg: AbcV3F1OrderConfig,
    note: str,
    client_intent_id: str,
) -> ResolvedOrder:
    qty = shares_for_budget(budget_twd, ask_price)
    return ResolvedOrder(
        symbol=symbol,
        side="buy",
        quantity_shares=qty,
        price=f"{ask_price:.2f}",
        price_type="limit",
        market_type=cfg.market_type,  # type: ignore[arg-type]
        time_in_force=cfg.time_in_force,  # type: ignore[arg-type]
        order_type="stock",
        user_def=client_intent_id,
        note=note,
        source="delta",
    )


def _append_ledger_entry(
    ledger: AbcV3F1Ledger,
    *,
    base: dict[str, Any],
    lifecycle_fields: dict[str, Any],
) -> dict[str, Any]:
    entry = {**base, **lifecycle_fields}
    ledger.entries.append(entry)
    save_ledger(ledger)
    return entry


def _submit_one(
    session: Any,
    *,
    cfg: AbcV3F1OrderConfig,
    signal: _BuySignalLike,
    session_date: str,
    available_cash: int | None,
    ledger: AbcV3F1Ledger,
    trading_dates: list[str],
    dry_run: bool,
) -> dict[str, Any]:
    from order.chase_runner import chase_ask_price
    from order.fubon_orders import cancel_open_buys_for_symbols, place_resolved_order

    symbol = str(signal.stock_id).strip()
    client_intent_id = _client_intent_id(
        cfg=cfg,
        session_date=session_date,
        poll_minute=signal.poll_minute,
        symbol=symbol,
    )

    existing = ledger.find_by_client_intent_id(client_intent_id)
    if existing is not None and str(existing.get("session_date") or "") == session_date:
        if entry_blocks_retry(existing):
            return {
                "symbol": symbol,
                "status": "skipped",
                "reason": f"idempotent_ledger:{client_intent_id}",
                "client_intent_id": client_intent_id,
                "lifecycle_status": existing.get("lifecycle_status"),
            }

    ask = float(chase_ask_price(session, symbol))
    allowed, reason = evaluate_reentry(
        symbol=symbol,
        session_date=session_date,
        ask_price=ask,
        cfg=cfg,
        ledger=ledger,
        trading_dates=trading_dates,
        available_cash=available_cash,
    )
    if not allowed:
        return {"symbol": symbol, "status": "skipped", "reason": reason, "ask_price": ask}

    payload = build_intent_payload(
        symbol=symbol,
        session_date=session_date,
        poll_minute=signal.poll_minute,
        signal_price=float(signal.price) if signal.price else None,
        stock_name=signal.stock_name,
        reason=signal.reason,
        budget_twd=cfg.budget_twd,
        price_type=cfg.price_type,
        market_type=cfg.market_type,
        client_intent_id=client_intent_id,
    )
    intent_path = write_intent_file(
        payload, session_date=session_date, poll_minute=signal.poll_minute, symbol=symbol
    )

    resolved = _build_resolved_buy(
        symbol=symbol,
        ask_price=ask,
        budget_twd=cfg.budget_twd,
        cfg=cfg,
        note=f"budget={cfg.budget_twd} · reentry={reason} · cid={client_intent_id}",
        client_intent_id=client_intent_id,
    )
    preview: dict[str, Any] = {
        "symbol": symbol,
        "status": "dry_run" if dry_run else "submitted",
        "reentry": reason,
        "client_intent_id": client_intent_id,
        "intent_file": str(intent_path),
        "ask_price": ask,
        "quantity_shares": resolved.quantity_shares,
        "notional_twd": round(resolved.quantity_shares * ask, 2),
        "budget_twd": cfg.budget_twd,
        "resolved": asdict(resolved),
    }
    if dry_run:
        return preview

    reconciled = reconcile_before_submit(
        session,
        client_intent_id=client_intent_id,
        fallback_ask=ask,
        fallback_qty=resolved.quantity_shares,
    )
    if reconciled is not None:
        update_intent_submit_status(intent_path, status="reconciled", reason="broker_existing_order")
        base = {
            "symbol": symbol,
            "session_date": session_date,
            "poll_minute": signal.poll_minute,
            "budget_twd": cfg.budget_twd,
            "ask_price": ask,
            "signal_price_kbar": signal.price,
            "reentry": reason,
            "submitted_at": datetime.now().isoformat(timespec="seconds"),
            "intent_file": str(intent_path),
            "client_intent_id": client_intent_id,
        }
        _append_ledger_entry(ledger, base=base, lifecycle_fields=reconciled)
        preview.update(reconciled)
        preview["status"] = "reconciled"
        preview["daily_entries"] = ledger.daily_count(session_date)
        preview["notify_submit"] = cfg.submit_notify
        return preview

    if cfg.cancel_open_buys:
        preview["cancelled_open_buys"] = cancel_open_buys_for_symbols(session, {symbol})

    submit_result = place_resolved_order(session, resolved)
    preview["submit_result"] = submit_result
    if not submit_result.get("is_success"):
        preview["status"] = "failed"
        preview["reason"] = str(submit_result.get("message") or "place_order_failed")
        update_intent_submit_status(intent_path, status="failed", reason=preview["reason"])
        quarantine_intent_file(intent_path, reason=preview["reason"])
        return preview

    order_no = submit_result.get("order_no")
    lifecycle = poll_order_lifecycle(
        session,
        client_intent_id=client_intent_id,
        order_no=str(order_no) if order_no else None,
        fallback_ask=ask,
        fallback_qty=resolved.quantity_shares,
        max_attempts=cfg.poll_max_attempts,
        interval_sec=cfg.poll_interval_sec,
    )
    outer = outer_status_from_lifecycle(str(lifecycle.get("lifecycle_status") or ""))
    update_intent_submit_status(intent_path, status=outer, reason=str(lifecycle.get("lifecycle_status") or ""))

    base = {
        "symbol": symbol,
        "session_date": session_date,
        "poll_minute": signal.poll_minute,
        "budget_twd": cfg.budget_twd,
        "ask_price": ask,
        "signal_price_kbar": signal.price,
        "reentry": reason,
        "submitted_at": datetime.now().isoformat(timespec="seconds"),
        "intent_file": str(intent_path),
        "client_intent_id": client_intent_id,
        "status": outer,
    }
    _append_ledger_entry(ledger, base=base, lifecycle_fields=lifecycle)
    preview.update(lifecycle)
    preview["status"] = outer
    preview["daily_entries"] = ledger.daily_count(session_date)
    preview["notional_twd"] = lifecycle.get("notional_twd", preview["notional_twd"])
    preview["notify_submit"] = cfg.submit_notify and outer in ("filled", "partial", "working", "ambiguous")
    return preview


def _preview_without_fubon(
    *,
    cfg: AbcV3F1OrderConfig,
    signal: _BuySignalLike,
    session_date: str,
    poll_minute: str,
    ledger: AbcV3F1Ledger,
    trading_dates: list[str],
    available_cash: int | None,
    exc: Exception,
) -> dict[str, Any]:
    symbol = str(signal.stock_id).strip()
    px = float(signal.price) if signal.price and signal.price > 0 else 0.0
    if px <= 0:
        return {"symbol": symbol, "status": "skipped", "reason": "no_signal_price"}
    allowed, reason = evaluate_reentry(
        symbol=symbol,
        session_date=session_date,
        ask_price=px,
        cfg=cfg,
        ledger=ledger,
        trading_dates=trading_dates,
        available_cash=available_cash,
    )
    if not allowed:
        return {"symbol": symbol, "status": "skipped", "reason": reason, "estimated_ask": px}
    cid = build_client_intent_id(
        session_date=session_date,
        poll_minute=signal.poll_minute or poll_minute,
        symbol=symbol,
    )
    payload = build_intent_payload(
        symbol=symbol,
        session_date=session_date,
        poll_minute=signal.poll_minute or poll_minute,
        signal_price=px,
        stock_name=signal.stock_name,
        reason=signal.reason,
        budget_twd=cfg.budget_twd,
        price_type=cfg.price_type,
        market_type=cfg.market_type,
        client_intent_id=cid,
    )
    intent_path = write_intent_file(
        payload,
        session_date=session_date,
        poll_minute=signal.poll_minute or poll_minute,
        symbol=symbol,
    )
    qty = shares_for_budget(cfg.budget_twd, px)
    return {
        "symbol": symbol,
        "status": "dry_run",
        "reentry": reason,
        "reason": f"no_fubon_session:{exc}",
        "client_intent_id": cid,
        "intent_file": str(intent_path),
        "estimated_ask": px,
        "quantity_shares": qty,
        "budget_twd": cfg.budget_twd,
    }


def filter_abc_v3_f1_hits(signals: list[_BuySignalLike]) -> list[_BuySignalLike]:
    """Current-poll abc-v3-f1 observe hits (not dedup-filtered · allows submit retry)."""
    return [
        s
        for s in signals
        if str(getattr(s, "pool_id", "") or "") == ABC_V3_F1_POOL_ID
        and str(getattr(s, "action", "") or "") == "observe"
    ]


def process_abc_v3_f1_orders(
    *,
    session_date: str,
    poll_minute: str,
    signals: list[_BuySignalLike],
    dry_run: bool | None = None,
) -> list[dict[str, Any]]:
    """Process abc-v3-f1 observe hits · write intent · optional auto-submit.

    Order sleeve removed 2026-07-16. Live callers must not submit; unit tests may
    set ABC_V3_F1_ORDER_FORCE_LEGACY=1 to exercise the legacy library path.
    """
    import os

    hits = filter_abc_v3_f1_hits(signals)
    if not hits:
        return []
    force_legacy = os.environ.get("ABC_V3_F1_ORDER_FORCE_LEGACY", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if not force_legacy:
        return [
            {
                "symbol": sig.stock_id,
                "status": "retired",
                "reason": "abc_order_removed:ABC removed from Order layer 2026-07-16",
            }
            for sig in hits
        ]

    cfg = load_abc_v3_f1_order_config()

    if dry_run is None:
        dry_run = not cfg.auto_submit

    if not dry_run:
        from order.live_submit_guard import live_submit_block_reason

        blocked = live_submit_block_reason(session_date=session_date)
        if blocked:
            return [
                {
                    "symbol": sig.stock_id,
                    "status": "error",
                    "reason": blocked,
                }
                for sig in hits
            ]

    ledger = load_ledger()
    trading_dates = load_tw_trading_dates()
    results: list[dict[str, Any]] = []

    if not cfg.order_enabled:
        for sig in hits:
            results.append(
                {
                    "symbol": sig.stock_id,
                    "status": "skipped",
                    "reason": "ABC_V3_F1_ORDER_ENABLED=0",
                }
            )
        return results

    if cfg.auto_submit and not dry_run:
        from order.fubon_subprocess import host_needs_fubon_venv, run_fubon_order_worker

        if host_needs_fubon_venv():
            payload = {
                "kind": "abc_v3_f1_entry",
                "session_date": session_date,
                "poll_minute": poll_minute,
                "dry_run": False,
                "signals": [
                    {
                        "stock_id": sig.stock_id,
                        "stock_name": getattr(sig, "stock_name", "") or "",
                        "action": getattr(sig, "action", "observe") or "observe",
                        "reason": getattr(sig, "reason", "") or "",
                        "price": float(getattr(sig, "price", 0) or 0),
                        "poll_minute": getattr(sig, "poll_minute", "") or poll_minute,
                        "pool_id": getattr(sig, "pool_id", "") or ABC_V3_F1_POOL_ID,
                    }
                    for sig in hits
                ],
            }
            try:
                out = run_fubon_order_worker(payload)
            except (FileNotFoundError, RuntimeError, OSError, ValueError) as exc:
                return [
                    {
                        "symbol": sig.stock_id,
                        "status": "error",
                        "reason": f"fubon_subprocess_failed:{exc}",
                    }
                    for sig in hits
                ]
            return list(out) if isinstance(out, list) else []

    session = None
    available_cash: int | None = None

    try:
        from order.fubon_account import bank_remain
        from order.fubon_session import connect_fubon

        session = connect_fubon()
        cash = bank_remain(session)
        available_cash = int(cash.get("available_balance") or cash.get("balance") or 0)
        if refresh_open_ledger_entries(session, ledger.entries, max_attempts=1, interval_sec=0.0):
            save_ledger(ledger)
    except (FileNotFoundError, RuntimeError, OSError, ValueError) as exc:
        if cfg.auto_submit and not dry_run:
            return [
                {
                    "symbol": sig.stock_id,
                    "status": "error",
                    "reason": f"fubon_connect_failed:{exc}",
                }
                for sig in hits
            ]
        for sig in hits:
            results.append(
                _preview_without_fubon(
                    cfg=cfg,
                    signal=sig,
                    session_date=session_date,
                    poll_minute=poll_minute,
                    ledger=ledger,
                    trading_dates=trading_dates,
                    available_cash=available_cash,
                    exc=exc,
                )
            )
        return results

    assert session is not None
    for sig in hits:
        results.append(
            _submit_one(
                session,
                cfg=cfg,
                signal=sig,
                session_date=session_date,
                available_cash=available_cash,
                ledger=ledger,
                trading_dates=trading_dates,
                dry_run=bool(dry_run),
            )
        )
        last = results[-1]
        if available_cash is not None and entry_counts_toward_cash(last):
            notional = float(last.get("notional_twd") or cfg.budget_twd)
            available_cash = max(0, int(available_cash - notional))

    return results
