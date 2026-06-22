"""Fubon chase round · 撤單重掛限價追賣一。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Any

from fubon_neo.constant import MarketType

from .chase import (
    CHASE_TAG,
    ChaseSessionState,
    ChaseSpec,
    SymbolChaseState,
    default_state_path,
    init_session_state,
    load_chase_spec,
    load_session_state,
    save_session_state,
    shares_for_budget,
)
from .fubon_orders import _result_ok, place_resolved_order
from .fubon_session import FubonSession
from .intent import ResolvedOrder


# Fubon order status: 10=委託中 50=完全成交 30=刪除 90=失敗
_STATUS_OPEN = 10
_STATUS_FILLED = 50
_STATUS_CANCELLED = 30


def _order_row(session: FubonSession, order_no: str, acc: Any | None = None) -> Any | None:
    account = acc or session.primary
    data = session.sdk.stock.get_order_results(account).data
    for item in list(data or []):
        if str(getattr(item, "order_no", "") or "") == order_no:
            return item
    return None


def _is_chase_order(item: Any) -> bool:
    tag = str(getattr(item, "user_def", "") or "")
    return tag == CHASE_TAG


def _filled_shares(item: Any) -> int:
    return int(getattr(item, "filled_qty", 0) or 0)


def _order_status(item: Any) -> int:
    return int(getattr(item, "status", 0) or 0)


def _cancel_order(session: FubonSession, item: Any, acc: Any | None = None) -> bool:
    account = acc or session.primary
    res = session.sdk.stock.cancel_order(account, item)
    return _result_ok(res)


def chase_ask_price(session: FubonSession, symbol: str, acc: Any | None = None) -> float:
    """盤中零股賣一；fallback 整股賣一／最新價／漲停。"""
    account = acc or session.primary
    candidates: list[float] = []
    limit_up: float | None = None

    for mt in (MarketType.IntradayOdd, MarketType.Common):
        res = session.sdk.stock.query_symbol_quote(account, symbol, mt)
        if not _result_ok(res) or res.data is None:
            continue
        d = res.data
        lu = getattr(d, "limitup_price", None)
        if lu is not None and float(lu) > 0:
            limit_up = float(lu)
        for key in ("ask_price", "last_price", "open_price"):
            val = getattr(d, key, None)
            if val is not None and float(val) > 0:
                candidates.append(float(val))

    if not candidates:
        if limit_up is not None:
            return limit_up
        raise RuntimeError(f"{symbol}: 無法取得追價報價")

    price = max(candidates)
    if limit_up is not None:
        price = min(price, limit_up)
    return price


def _sync_symbol_from_order(st: SymbolChaseState, item: Any | None) -> None:
    if item is None:
        return
    filled = _filled_shares(item)
    if filled > st.filled_shares:
        st.filled_shares = filled
    target = int(getattr(item, "after_qty", 0) or getattr(item, "quantity", 0) or 0)
    if target > 0:
        st.target_shares = target
    if _order_status(item) == _STATUS_FILLED and st.filled_shares >= st.target_shares > 0:
        st.status = "filled"
    elif _order_status(item) in (_STATUS_CANCELLED, 90) and st.filled_shares >= st.target_shares > 0:
        st.status = "filled"


def run_chase_round(
    session: FubonSession,
    spec: ChaseSpec,
    *,
    state_path: Any | None = None,
    trade_date: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    td = trade_date or date.today().strftime("%Y-%m-%d")
    path = state_path or default_state_path(td)
    state = load_session_state(path)
    if state is None or state.trade_date != td:
        state = init_session_state(spec, td)
    for sym in spec.symbols:
        state.symbols.setdefault(sym, SymbolChaseState())

    acc = session.primary
    log: list[dict[str, Any]] = []

    for symbol in spec.symbols:
        st = state.symbols[symbol]
        entry: dict[str, Any] = {"symbol": symbol, "rounds_before": st.rounds}

        if st.status in ("filled", "timeout", "disabled"):
            entry["action"] = "skip"
            entry["reason"] = st.status
            log.append(entry)
            continue

        if st.order_no:
            item = _order_row(session, st.order_no, acc)
            _sync_symbol_from_order(st, item)
            if st.status == "filled":
                entry["action"] = "already_filled"
                entry["filled_shares"] = st.filled_shares
                log.append(entry)
                continue
            if item is not None and _order_status(item) == _STATUS_OPEN and _is_chase_order(item):
                if not dry_run:
                    _cancel_order(session, item, acc)
                entry["cancelled"] = st.order_no

        if st.rounds >= spec.max_rounds:
            st.status = "timeout"
            entry["action"] = "timeout"
            entry["rounds"] = st.rounds
            log.append(entry)
            continue

        try:
            ask = chase_ask_price(session, symbol, acc)
        except RuntimeError as exc:
            entry["action"] = "quote_error"
            entry["error"] = str(exc)
            log.append(entry)
            continue

        qty = shares_for_budget(spec.budget_twd_per_symbol, ask)
        st.rounds += 1
        entry["rounds"] = st.rounds
        entry["ask"] = ask
        entry["target_shares"] = qty

        if dry_run:
            entry["action"] = "dry_run"
            log.append(entry)
            continue

        resolved = ResolvedOrder(
            symbol=symbol,
            side="buy",
            quantity_shares=qty,
            price=str(ask),
            price_type="limit",
            market_type=spec.market_type,  # type: ignore[arg-type]
            time_in_force="rod",
            order_type="stock",
            user_def=CHASE_TAG,
            note="chase",
            source="delta",
        )
        result = place_resolved_order(session, resolved, acc=acc)
        entry["action"] = "placed"
        entry["place"] = result
        if result.get("is_success"):
            st.order_no = str(result.get("order_no") or "")
            st.target_shares = qty
            st.limit_price = ask
            item = _order_row(session, st.order_no, acc)
            _sync_symbol_from_order(st, item)
        else:
            entry["action"] = "place_failed"

        log.append(entry)

    save_session_state(path, state)
    return {
        "trade_date": td,
        "state_path": str(path),
        "dry_run": dry_run,
        "state": state.to_dict(),
        "log": log,
    }


def run_chase_from_spec_file(
    spec_path: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    from .fubon_session import connect_fubon

    spec = load_chase_spec(spec_path)
    session = connect_fubon()
    return run_chase_round(session, spec, dry_run=dry_run)
