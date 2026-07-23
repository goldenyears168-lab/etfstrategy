"""ABC v3+f1 order lifecycle · poll · reconcile · client_intent_id."""

from __future__ import annotations

import time
from typing import Any, Literal

from order.fubon_orders import is_open_order, order_results, order_status

NotionalBasis = Literal["submitted", "filled"]

LIFECYCLE_FAILED = "failed"
LIFECYCLE_CANCELLED = "cancelled"
LIFECYCLE_FILLED = "filled"
LIFECYCLE_PARTIAL = "partial"
LIFECYCLE_WORKING = "working"
LIFECYCLE_SUBMITTED = "submitted"
LIFECYCLE_AMBIGUOUS = "ambiguous"  # placed but broker row not found after poll

_STATUS_FAILED = 90

# Outer preview/ledger ``status`` mirrors lifecycle (no longer force "submitted").
_OPEN_STATUSES = frozenset(
    {LIFECYCLE_WORKING, LIFECYCLE_SUBMITTED, LIFECYCLE_PARTIAL, LIFECYCLE_AMBIGUOUS}
)
_CASH_STATUSES = frozenset({LIFECYCLE_FILLED, LIFECYCLE_PARTIAL})


def outer_status_from_lifecycle(lifecycle_status: str | None) -> str:
    """Map lifecycle → result status shown in preview / cash gates."""
    lc = str(lifecycle_status or "").strip().lower()
    if lc == LIFECYCLE_SUBMITTED:
        return LIFECYCLE_WORKING
    if lc in (
        LIFECYCLE_FILLED,
        LIFECYCLE_PARTIAL,
        LIFECYCLE_WORKING,
        LIFECYCLE_CANCELLED,
        LIFECYCLE_FAILED,
        LIFECYCLE_AMBIGUOUS,
    ):
        return lc
    return LIFECYCLE_WORKING if lc else LIFECYCLE_AMBIGUOUS


def entry_counts_toward_cash(entry: dict[str, Any]) -> bool:
    """True only when fill (or partial) is known — avoids P1 over-count."""
    lc = str(entry.get("lifecycle_status") or entry.get("status") or "").strip().lower()
    if lc in _CASH_STATUSES:
        return True
    st = str(entry.get("status") or "").strip().lower()
    return st in _CASH_STATUSES


def entry_is_open(entry: dict[str, Any]) -> bool:
    lc = str(entry.get("lifecycle_status") or "").strip().lower()
    return lc in _OPEN_STATUSES


def build_client_intent_id(
    *,
    strategy_id: str,
    session_date: str,
    poll_minute: str,
    symbol: str,
) -> str:
    """Deterministic id · stable across submit retries for the same poll hit."""
    safe_poll = str(poll_minute or "").replace(":", "")
    stamp = str(session_date or "").replace("-", "")
    sym = str(symbol).strip()
    return f"{strategy_id}_{stamp}_{safe_poll}_{sym}"


def lifecycle_status_from_row(row: dict[str, Any]) -> str:
    """Map Fubon order_results row → lifecycle status string."""
    raw_status = row.get("status")
    try:
        st = int(raw_status) if raw_status is not None else -1
    except (TypeError, ValueError):
        st = -1
    qty = int(row.get("after_qty") or row.get("quantity") or 0)
    filled = int(row.get("filled_qty") or 0)
    if st == _STATUS_FAILED:
        return LIFECYCLE_FAILED
    if st == 30:
        return LIFECYCLE_CANCELLED
    if st == 50 or (qty > 0 and filled >= qty):
        return LIFECYCLE_FILLED
    if filled > 0 and qty > 0 and filled < qty:
        return LIFECYCLE_PARTIAL
    if is_open_order(_row_as_obj(row)) or st in (0, 10):
        return LIFECYCLE_WORKING
    if filled > 0:
        return LIFECYCLE_PARTIAL
    return LIFECYCLE_SUBMITTED


def _row_as_obj(row: dict[str, Any]) -> Any:
    class _Row:
        pass

    o = _Row()
    o.status = row.get("status")
    return o


def row_filled_qty(row: dict[str, Any]) -> int:
    return int(row.get("filled_qty") or 0)


def row_order_qty(row: dict[str, Any]) -> int:
    return int(row.get("after_qty") or row.get("quantity") or 0)


def entry_blocks_retry(entry: dict[str, Any]) -> bool:
    """False for failed ledger rows → same-day retry allowed."""
    status = str(entry.get("lifecycle_status") or LIFECYCLE_SUBMITTED)
    return status not in (LIFECYCLE_FAILED,)


def entry_notional(entry: dict[str, Any], *, basis: NotionalBasis) -> float:
    if basis == "filled":
        filled = int(entry.get("filled_qty") or 0)
        if filled <= 0:
            return 0.0
        px = float(entry.get("avg_fill_price") or entry.get("ask_price") or 0.0)
        if px <= 0:
            return float(entry.get("notional_twd") or entry.get("budget_twd") or 0.0)
        return filled * px
    qty = int(entry.get("quantity_shares") or 0)
    px = float(entry.get("ask_price") or 0.0)
    if px > 0 and qty > 0:
        return px * qty
    return float(entry.get("notional_twd") or entry.get("budget_twd") or 0.0)


def find_order_row(session: Any, *, client_intent_id: str, order_no: str | None = None) -> dict[str, Any] | None:
    rows = order_results(session)
    if order_no:
        for row in rows:
            if str(row.get("order_no") or "") == str(order_no):
                return row
    for row in rows:
        if str(row.get("user_def") or "") == client_intent_id:
            return row
    return None


def ledger_fields_from_row(row: dict[str, Any], *, fallback_ask: float, fallback_qty: int) -> dict[str, Any]:
    filled = row_filled_qty(row)
    qty = row_order_qty(row) or fallback_qty
    lifecycle = lifecycle_status_from_row(row)
    ask = float(row.get("after_price") or row.get("price") or fallback_ask)
    notional = filled * ask if filled > 0 else qty * ask
    return {
        "order_no": row.get("order_no"),
        "broker_status": row.get("status"),
        "lifecycle_status": lifecycle,
        "filled_qty": filled,
        "quantity_shares": qty,
        "avg_fill_price": ask if filled > 0 else None,
        "notional_twd": round(notional, 2),
    }


def poll_order_lifecycle(
    session: Any,
    *,
    client_intent_id: str,
    order_no: str | None,
    fallback_ask: float,
    fallback_qty: int,
    max_attempts: int,
    interval_sec: float,
) -> dict[str, Any]:
    """Poll get_order_results until terminal or attempts exhausted."""
    last: dict[str, Any] | None = None
    attempts = max(1, int(max_attempts))
    for attempt in range(attempts):
        row = find_order_row(session, client_intent_id=client_intent_id, order_no=order_no)
        if row is not None:
            fields = ledger_fields_from_row(row, fallback_ask=fallback_ask, fallback_qty=fallback_qty)
            last = fields
            if fields["lifecycle_status"] in (
                LIFECYCLE_FILLED,
                LIFECYCLE_CANCELLED,
                LIFECYCLE_FAILED,
            ):
                return fields
        if attempt + 1 < attempts and interval_sec > 0:
            time.sleep(interval_sec)
    if last is not None:
        # Still open after poll window — keep working/partial; do not pretend filled.
        return last
    return {
        "lifecycle_status": LIFECYCLE_AMBIGUOUS,
        "filled_qty": 0,
        "quantity_shares": fallback_qty,
        "notional_twd": 0.0,  # unknown fill → zero exposure until refresh
        "order_no": order_no,
        "broker_status": None,
    }


def refresh_open_ledger_entries(
    session: Any,
    entries: list[dict[str, Any]],
    *,
    max_attempts: int = 1,
    interval_sec: float = 0.0,
) -> int:
    """Re-poll broker for open/ambiguous rows · mutate entries in place · return update count."""
    updated = 0
    for entry in entries:
        if not entry_is_open(entry):
            continue
        cid = str(entry.get("client_intent_id") or "")
        if not cid:
            continue
        order_no = str(entry.get("order_no") or "") or None
        fallback_ask = float(entry.get("ask_price") or entry.get("avg_fill_price") or 0.0)
        fallback_qty = int(entry.get("quantity_shares") or 0)
        fields = poll_order_lifecycle(
            session,
            client_intent_id=cid,
            order_no=order_no,
            fallback_ask=fallback_ask or 1.0,
            fallback_qty=fallback_qty or 1,
            max_attempts=max_attempts,
            interval_sec=interval_sec,
        )
        # Don't overwrite a known working row with ambiguous if poll finds nothing again.
        if (
            fields.get("lifecycle_status") == LIFECYCLE_AMBIGUOUS
            and str(entry.get("lifecycle_status") or "") in (LIFECYCLE_WORKING, LIFECYCLE_PARTIAL)
        ):
            continue
        before = (
            entry.get("lifecycle_status"),
            entry.get("filled_qty"),
            entry.get("broker_status"),
        )
        entry.update(fields)
        entry["status"] = outer_status_from_lifecycle(str(fields.get("lifecycle_status") or ""))
        after = (
            entry.get("lifecycle_status"),
            entry.get("filled_qty"),
            entry.get("broker_status"),
        )
        if before != after:
            updated += 1
    return updated


def reconcile_before_submit(
    session: Any,
    *,
    client_intent_id: str,
    fallback_ask: float,
    fallback_qty: int,
) -> dict[str, Any] | None:
    """If broker already has this client_intent_id, return ledger patch without re-placing."""
    row = find_order_row(session, client_intent_id=client_intent_id)
    if row is None:
        return None
    fields = ledger_fields_from_row(row, fallback_ask=fallback_ask, fallback_qty=fallback_qty)
    if fields["lifecycle_status"] == LIFECYCLE_FAILED:
        return None
    return fields
