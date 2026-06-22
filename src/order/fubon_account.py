"""Fubon Neo API · account / inventory queries."""

from __future__ import annotations

from typing import Any

from .fubon_session import FubonSession


def _result_data(res: Any) -> Any:
    if not getattr(res, "is_success", getattr(res, "isSuccess", False)):
        msg = getattr(res, "message", "") or "request failed"
        raise RuntimeError(msg)
    return getattr(res, "data", None)


def _account_fields(acc: Any) -> dict[str, str]:
    return {
        "name": str(getattr(acc, "name", "")),
        "branch_no": str(getattr(acc, "branch_no", getattr(acc, "branchNo", ""))),
        "account": str(getattr(acc, "account", "")),
        "account_type": str(
            getattr(acc, "account_type", getattr(acc, "accountType", ""))
        ),
    }


def _odd_lot_fields(odd: Any) -> dict[str, int]:
    if odd is None:
        return {}
    out: dict[str, int] = {}
    for key in (
        "lastday_qty",
        "today_qty",
        "tradable_qty",
        "buy_qty",
        "sell_qty",
        "buy_filled_qty",
        "sell_filled_qty",
    ):
        val = getattr(odd, key, None)
        if val is not None:
            out[key] = int(val)
    return out


def _inventory_row(item: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "stock_no": str(getattr(item, "stock_no", "")),
        "date": str(getattr(item, "date", "")),
        "lastday_qty": int(getattr(item, "lastday_qty", 0) or 0),
        "today_qty": int(getattr(item, "today_qty", 0) or 0),
        "tradable_qty": int(getattr(item, "tradable_qty", 0) or 0),
    }
    odd = _odd_lot_fields(getattr(item, "odd", None))
    if odd:
        row["odd"] = odd
    return row


def _unrealized_row(item: Any) -> dict[str, Any]:
    return {
        "stock_no": str(getattr(item, "stock_no", "")),
        "date": str(getattr(item, "date", "")),
        "today_qty": int(getattr(item, "today_qty", 0) or 0),
        "tradable_qty": int(getattr(item, "tradable_qty", 0) or 0),
        "cost_price": float(getattr(item, "cost_price", 0) or 0),
        "unrealized_profit": int(getattr(item, "unrealized_profit", 0) or 0),
        "unrealized_loss": int(getattr(item, "unrealized_loss", 0) or 0),
    }


def bank_remain(session: FubonSession, acc: Any | None = None) -> dict[str, Any]:
    account = acc or session.primary
    data = _result_data(session.sdk.accounting.bank_remain(account))
    return {
        "balance": int(getattr(data, "balance", 0) or 0),
        "available_balance": int(getattr(data, "available_balance", 0) or 0),
        "currency": str(getattr(data, "currency", "TWD")),
    }


def inventories(session: FubonSession, acc: Any | None = None) -> list[dict[str, Any]]:
    account = acc or session.primary
    data = _result_data(session.sdk.accounting.inventories(account))
    rows = list(data or [])
    return [_inventory_row(item) for item in rows]


def unrealized_pnl(session: FubonSession, acc: Any | None = None) -> list[dict[str, Any]]:
    account = acc or session.primary
    data = _result_data(session.sdk.accounting.unrealized_gains_and_loses(account))
    rows = list(data or [])
    return [_unrealized_row(item) for item in rows]


def account_snapshot(session: FubonSession, acc: Any | None = None) -> dict[str, Any]:
    account = acc or session.primary
    cash = bank_remain(session, account)
    holdings = inventories(session, account)
    pnl = unrealized_pnl(session, account)
    active_holdings = [
        h
        for h in holdings
        if h.get("tradable_qty", 0) > 0
        or (h.get("odd") or {}).get("tradable_qty", 0) > 0
    ]
    return {
        "account": _account_fields(account),
        "cash": cash,
        "holdings": active_holdings or holdings,
        "unrealized_pnl": pnl,
    }
