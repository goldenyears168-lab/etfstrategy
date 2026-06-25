"""Fubon Neo API · order placement / query."""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any

from .fubon_session import FubonSession
from .intent import OrderIntentBatch, ResolvedOrder, resolve_intents

_STATUS_OPEN = 10
# 富邦 API：文件寫 10=委託中；盤中零股實際常回 0（不可用 `status or 10` 判斷）
_STATUS_OPEN_VALUES = frozenset({0, 10})
_STATUS_FILLED = 50
_STATUS_CANCELLED = 30


def order_status(item: Any) -> int:
    val = getattr(item, "status", None)
    if val is None:
        return -1
    return int(val)


def is_open_order(item: Any) -> bool:
    return order_status(item) in _STATUS_OPEN_VALUES


def _serialize_order_field(val: Any) -> Any:
    if hasattr(val, "name") and not isinstance(val, (str, bytes)):
        return val.name
    return val


def _result_ok(res: Any) -> bool:
    ok = getattr(res, "is_success", None)
    if ok is None:
        ok = getattr(res, "isSuccess", False)
    return bool(ok)


def _result_data(res: Any) -> Any:
    if not _result_ok(res):
        msg = getattr(res, "message", "") or "request failed"
        raise RuntimeError(msg)
    return getattr(res, "data", None)


def holdings_shares_by_symbol(session: FubonSession, acc: Any | None = None) -> dict[str, int]:
    """整股 + 零股可賣量合計（股）。"""
    account = acc or session.primary
    data = _result_data(session.sdk.accounting.inventories(account))
    out: dict[str, int] = {}
    for item in list(data or []):
        sym = str(getattr(item, "stock_no", "") or "")
        if not sym:
            continue
        whole = int(getattr(item, "tradable_qty", 0) or 0)
        odd_obj = getattr(item, "odd", None)
        odd = int(getattr(odd_obj, "tradable_qty", 0) or 0) if odd_obj is not None else 0
        total = whole + odd
        if total > 0 or sym in out:
            out[sym] = total
    return out


def _is_buy_order(item: Any) -> bool:
    bs = getattr(item, "buy_sell", None)
    if bs is None:
        return False
    name = str(getattr(bs, "name", bs)).lower()
    return "buy" in name


def cancel_open_buys_for_symbols(
    session: FubonSession,
    symbols: set[str],
    *,
    acc: Any | None = None,
) -> list[dict[str, Any]]:
    """撤銷指定標的之委託中買單（含人工掛單）。"""
    account = acc or session.primary
    data = _result_data(session.sdk.stock.get_order_results(account))
    out: list[dict[str, Any]] = []
    for item in list(data or []):
        sym = str(getattr(item, "stock_no", "") or "")
        if sym not in symbols:
            continue
        if not is_open_order(item):
            continue
        if not _is_buy_order(item):
            continue
        ok = _result_ok(session.sdk.stock.cancel_order(account, item))
        out.append(
            {
                "symbol": sym,
                "order_no": getattr(item, "order_no", None),
                "cancelled": ok,
            }
        )
    return out


def apply_chase_ask_prices(
    session: FubonSession,
    resolved: list[ResolvedOrder],
    *,
    acc: Any | None = None,
) -> list[ResolvedOrder]:
    """chase_ask → 送單當下賣一限價（盤中零股優先）。"""
    from .chase_runner import chase_ask_price

    out: list[ResolvedOrder] = []
    for item in resolved:
        if item.price_type != "chase_ask":
            out.append(item)
            continue
        ask = chase_ask_price(session, item.symbol, acc)
        market_type = item.market_type
        if item.quantity_shares < 1000 and market_type in ("odd", "common"):
            market_type = "intraday_odd"
        out.append(
            replace(
                item,
                price=f"{ask:.2f}",
                price_type="limit",
                market_type=market_type,  # type: ignore[arg-type]
            )
        )
    return out


def resolve_batch_orders(
    session: FubonSession,
    batch: OrderIntentBatch,
    *,
    acc: Any | None = None,
) -> list[ResolvedOrder]:
    account = acc or session.primary
    holdings = holdings_shares_by_symbol(session, account)
    resolved = resolve_intents(batch, holdings)
    return apply_chase_ask_prices(session, resolved, acc=account)


def _map_bs_action(side: str) -> Any:
    from fubon_neo.constant import BSAction

    if side == "buy":
        return BSAction.Buy
    if side == "sell":
        return BSAction.Sell
    raise ValueError(f"unsupported side: {side}")


def _map_enum(cls: Any, name: str, *, field: str) -> Any:
    norm = name.strip().lower()
    aliases = {
        "intraday_odd": "IntradayOdd",
        "emg_odd": "EmgOdd",
        "limit_up": "LimitUp",
        "limit_down": "LimitDown",
        "daytrade": "DayTrade",
        "rod": "ROD",
        "fok": "FOK",
        "ioc": "IOC",
    }
    candidates: list[str] = []
    if norm in aliases:
        candidates.append(aliases[norm])
    candidates.append(norm.upper())
    candidates.append("".join(part[:1].upper() + part[1:] for part in norm.split("_")))
    seen: set[str] = set()
    for key in candidates:
        if key in seen:
            continue
        seen.add(key)
        if hasattr(cls, key):
            return getattr(cls, key)
    raise ValueError(f"unsupported {field}: {name}")


def build_order(resolved: ResolvedOrder) -> Any:
    from fubon_neo.constant import MarketType, OrderType, PriceType, TimeInForce
    from fubon_neo.sdk import Order

    price_type = _map_enum(PriceType, resolved.price_type, field="price_type")
    price = resolved.price if resolved.price_type == "limit" else None
    if resolved.market_type == "intraday_odd" and resolved.price_type == "market":
        raise ValueError(
            f"{resolved.symbol}: 盤中零股不支援市價單，請改用 reference 或 limit"
        )
    return Order(
        buy_sell=_map_bs_action(resolved.side),
        symbol=resolved.symbol,
        price=price,
        quantity=int(resolved.quantity_shares),
        market_type=_map_enum(MarketType, resolved.market_type, field="market_type"),
        price_type=price_type,
        time_in_force=_map_enum(TimeInForce, resolved.time_in_force, field="time_in_force"),
        order_type=_map_enum(OrderType, resolved.order_type, field="order_type"),
        user_def=resolved.user_def,
    )


def place_resolved_order(
    session: FubonSession,
    resolved: ResolvedOrder,
    *,
    acc: Any | None = None,
) -> dict[str, Any]:
    account = acc or session.primary
    order = build_order(resolved)
    res = session.sdk.stock.place_order(account, order)
    payload: dict[str, Any] = {
        "symbol": resolved.symbol,
        "side": resolved.side,
        "quantity_shares": resolved.quantity_shares,
        "source": resolved.source,
        "is_success": _result_ok(res),
        "message": getattr(res, "message", None),
    }
    data = getattr(res, "data", None)
    if data is not None:
        payload["order_no"] = getattr(data, "order_no", getattr(data, "orderNo", None))
    return payload


def place_resolved_orders(
    session: FubonSession,
    resolved: list[ResolvedOrder],
    *,
    acc: Any | None = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for item in resolved:
        results.append(place_resolved_order(session, item, acc=acc))
    return {
        "resolved_count": len(resolved),
        "results": results,
    }


def place_batch(
    session: FubonSession,
    batch: OrderIntentBatch,
    *,
    acc: Any | None = None,
    resolved: list[ResolvedOrder] | None = None,
) -> dict[str, Any]:
    items = resolved if resolved is not None else resolve_batch_orders(session, batch, acc=acc)
    payload = place_resolved_orders(session, items, acc=acc)
    payload["strategy_id"] = batch.strategy_id
    payload["as_of"] = batch.as_of
    return payload


def order_results(session: FubonSession, acc: Any | None = None) -> list[dict[str, Any]]:
    account = acc or session.primary
    data = _result_data(session.sdk.stock.get_order_results(account))
    rows: list[dict[str, Any]] = []
    for item in list(data or []):
        row: dict[str, Any] = {}
        for key in (
            "order_no",
            "stock_no",
            "buy_sell",
            "price",
            "after_price",
            "quantity",
            "after_qty",
            "filled_qty",
            "filled_money",
            "status",
            "order_type",
            "market_type",
            "price_type",
            "time_in_force",
            "user_def",
            "seq_no",
        ):
            val = getattr(item, key, None)
            if val is not None:
                row[key] = _serialize_order_field(val)
        if row:
            rows.append(row)
    return rows


def resolved_orders_preview(
    session: FubonSession,
    batch: OrderIntentBatch,
    *,
    acc: Any | None = None,
) -> tuple[list[ResolvedOrder], list[dict[str, Any]]]:
    resolved = resolve_batch_orders(session, batch, acc=acc)
    return resolved, [asdict(x) for x in resolved]
