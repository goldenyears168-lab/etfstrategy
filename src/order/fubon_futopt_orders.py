"""Fubon Neo · futopt (futures/options) place / cancel / query.

Mirrors ``fubon_orders`` stock path but uses ``sdk.futopt`` + ``FutOptOrder``.
All live submits go through ``live_submit_guard.assert_live_submit_allowed``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from fubon_neo.constant import (
    BSAction,
    FutOptMarketType,
    FutOptOrderType,
    FutOptPriceType,
    TimeInForce,
)
from fubon_neo.sdk import FutOptOrder

from .fubon_session import FubonSession, account_label
from .live_submit_guard import assert_live_submit_allowed


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _result_ok(res: Any) -> bool:
    ok = getattr(res, "is_success", None)
    if ok is None:
        ok = getattr(res, "isSuccess", False)
    return bool(ok)


def _result_data(res: Any) -> Any:
    if not _result_ok(res):
        msg = getattr(res, "message", "") or "futopt request failed"
        raise RuntimeError(msg)
    return getattr(res, "data", None)


def pick_futopt_account(session: FubonSession, prefer: str | None = None) -> Any:
    """Pick futures account from login list.

    Prefer ``ORDER_TMF_ACCOUNT`` / ``prefer`` substring match on account number,
    else first account whose type looks like futures. Never silently use a
    stock-only account when a futures account exists.
    """
    prefer = (prefer or _env_str("ORDER_TMF_ACCOUNT")).strip()
    accounts = list(session.accounts or [])
    if not accounts:
        raise RuntimeError("No Fubon accounts after login")

    def _atype(acc: Any) -> str:
        return str(
            getattr(acc, "account_type", None)
            or getattr(acc, "accountType", None)
            or ""
        ).lower()

    def _anum(acc: Any) -> str:
        return str(getattr(acc, "account", "") or "")

    if prefer:
        for acc in accounts:
            if prefer in _anum(acc) or prefer.lower() in _atype(acc):
                return acc
        raise RuntimeError(f"ORDER_TMF_ACCOUNT={prefer!r} not in login accounts")

    futish = []
    for acc in accounts:
        at = _atype(acc)
        if any(k in at for k in ("fut", "future", "期貨", "futo")):
            futish.append(acc)
    if len(futish) == 1:
        return futish[0]
    if len(futish) > 1:
        # Prefer day futures account if labeled; else first
        return futish[0]
    # Last resort: single account login (some retail logins return one multi-asset)
    if len(accounts) == 1:
        return accounts[0]
    labels = [f"{account_label(a)} type={_atype(a)!r}" for a in accounts]
    raise RuntimeError(
        "Cannot pick futopt account — set ORDER_TMF_ACCOUNT. accounts=" + "; ".join(labels)
    )


def market_type_for_hhmm(hm: str) -> FutOptMarketType:
    """Day 08:45–13:45 → Future; night 15:00–05:00 → FutureNight."""
    if hm >= "15:00" or hm < "05:00":
        return FutOptMarketType.FutureNight
    return FutOptMarketType.Future


@dataclass(frozen=True)
class FutOptResolvedOrder:
    symbol: str
    buy_sell: str  # Buy | Sell
    lot: int
    price: float | None  # None → market
    price_type: str  # limit | market
    time_in_force: str  # rod | ioc | fok
    order_type: str  # auto | new | close
    market_type: str  # future | future_night
    user_def: str = "tmfch"
    session_date: str | None = None


def build_futopt_order(resolved: FutOptResolvedOrder) -> FutOptOrder:
    bs = BSAction.Buy if resolved.buy_sell.lower().startswith("b") else BSAction.Sell
    mt = (
        FutOptMarketType.FutureNight
        if "night" in resolved.market_type.lower()
        else FutOptMarketType.Future
    )
    pt = (
        FutOptPriceType.Market
        if resolved.price_type.lower() == "market"
        else FutOptPriceType.Limit
    )
    tif_l = resolved.time_in_force.lower()
    if tif_l == "ioc":
        tif = TimeInForce.IOC
    elif tif_l == "fok":
        tif = TimeInForce.FOK
    else:
        tif = TimeInForce.ROD
    ot_l = resolved.order_type.lower()
    if ot_l == "new":
        ot = FutOptOrderType.New
    elif ot_l == "close":
        ot = FutOptOrderType.Close
    else:
        ot = FutOptOrderType.Auto
    price = None if resolved.price is None or pt == FutOptPriceType.Market else str(int(round(resolved.price)))
    return FutOptOrder(
        market_type=mt,
        price_type=pt,
        time_in_force=tif,
        order_type=ot,
        buy_sell=bs,
        symbol=resolved.symbol,
        lot=int(resolved.lot),
        price=price,
        user_def=(resolved.user_def or "tmfch")[:10],
    )


def place_futopt_order(
    session: FubonSession,
    resolved: FutOptResolvedOrder,
    *,
    acc: Any | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Place one futopt order. dry_run=True never hits broker."""
    account = acc or pick_futopt_account(session)
    order = build_futopt_order(resolved)
    payload = {
        "dry_run": bool(dry_run),
        "account": account_label(account),
        "symbol": resolved.symbol,
        "buy_sell": resolved.buy_sell,
        "lot": resolved.lot,
        "price": resolved.price,
        "price_type": resolved.price_type,
        "tif": resolved.time_in_force,
        "order_type": resolved.order_type,
        "market_type": resolved.market_type,
        "user_def": resolved.user_def,
    }
    if dry_run:
        payload["status"] = "dry_run"
        payload["ok"] = True
        return payload
    assert_live_submit_allowed(session_date=resolved.session_date)
    res = session.sdk.futopt.place_order(account, order)
    payload["ok"] = _result_ok(res)
    payload["message"] = getattr(res, "message", "") or ""
    payload["data"] = _serialize(getattr(res, "data", None))
    if not payload["ok"]:
        raise RuntimeError(payload["message"] or "futopt place_order failed")
    payload["status"] = "submitted"
    return payload


def cancel_futopt_order(
    session: FubonSession,
    order_res: Any,
    *,
    acc: Any | None = None,
    dry_run: bool = True,
    session_date: str | None = None,
) -> dict[str, Any]:
    account = acc or pick_futopt_account(session)
    if dry_run:
        return {"ok": True, "status": "dry_run_cancel", "data": _serialize(order_res)}
    assert_live_submit_allowed(session_date=session_date)
    res = session.sdk.futopt.cancel_order(account, order_res)
    ok = _result_ok(res)
    if not ok:
        raise RuntimeError(getattr(res, "message", "") or "futopt cancel failed")
    return {"ok": True, "status": "cancelled", "data": _serialize(getattr(res, "data", None))}


def get_futopt_order_results(
    session: FubonSession,
    *,
    acc: Any | None = None,
    market_type: FutOptMarketType | None = None,
) -> list[Any]:
    account = acc or pick_futopt_account(session)
    res = session.sdk.futopt.get_order_results(account, market_type)
    data = _result_data(res)
    return list(data or [])


def _bs_to_side(buy_sell: Any) -> str | None:
    name = str(getattr(buy_sell, "name", buy_sell) or "").lower()
    if "sell" in name:
        return "S"
    if "buy" in name:
        return "L"
    return None


def is_tmf_acct_symbol(sym: str, *, front_symbol: str | None = None) -> bool:
    """Accounting / order book often uses FITM; marketdata uses TMFH6."""
    s = (sym or "").upper()
    if not s:
        return False
    if s.startswith("TMF") or s == "FITM":
        return True
    if front_symbol and s == str(front_symbol).upper():
        return True
    return False


# Back-compat alias
_is_tmf_acct_symbol = is_tmf_acct_symbol


def query_tmf_broker_net(
    session: FubonSession,
    *,
    acc: Any | None = None,
    front_symbol: str | None = None,
) -> dict[str, Any] | None:
    """Net TMF position from ``futopt_accounting.query_hybrid_position``.

    Returns ``{"s": "S"|"L", "n": int, "ep": float|None, "acct_symbol": str}``
    or None if flat / query failed empty.
    """
    account = acc or pick_futopt_account(session)
    fa = getattr(session.sdk, "futopt_accounting", None)
    if fa is None:
        return None
    res = fa.query_hybrid_position(account)
    rows = _result_data(res) or []
    net_s: str | None = None
    net_n = 0
    px_sum = 0.0
    px_n = 0
    acct_sym = ""
    for row in rows:
        sym = str(getattr(row, "symbol", "") or "")
        if not _is_tmf_acct_symbol(sym, front_symbol=front_symbol):
            continue
        side = _bs_to_side(getattr(row, "buy_sell", None))
        try:
            lots = int(getattr(row, "orig_lots", None) or 0)
        except (TypeError, ValueError):
            lots = 0
        if side is None or lots <= 0:
            continue
        if net_s is None:
            net_s = side
            net_n = lots
            acct_sym = sym
        elif side == net_s:
            net_n += lots
        else:
            # rare: both sides present — net down
            if lots >= net_n:
                net_s = side
                net_n = lots - net_n
                acct_sym = sym
            else:
                net_n -= lots
        try:
            px = float(getattr(row, "price", None))
            px_sum += px * lots
            px_n += lots
        except (TypeError, ValueError):
            pass
    if not net_s or net_n <= 0:
        return None
    return {
        "s": net_s,
        "n": int(net_n),
        "ep": (px_sum / px_n) if px_n else None,
        "acct_symbol": acct_sym,
    }


# Never call dir()/vars() on Fubon Neo SDK objects — dir() on some handles
# walks native graphs and can hang / balloon RSS (seen 2026-08-05 open poll).
_SERIALIZE_KEYS = (
    "order_no",
    "orderNo",
    "seq_no",
    "seqNo",
    "symbol",
    "buy_sell",
    "buySell",
    "price",
    "quantity",
    "filled_qty",
    "filledQty",
    "status",
    "status_code",
    "statusCode",
    "message",
    "error",
    "account",
    "account_type",
    "time_in_force",
    "timeInForce",
    "order_type",
    "orderType",
    "price_type",
    "priceType",
    "market_type",
    "marketType",
)


def _serialize(val: Any) -> Any:
    if val is None or isinstance(val, (str, int, float, bool)):
        return val
    if isinstance(val, list):
        return [_serialize(x) for x in val[:50]]
    if isinstance(val, dict):
        return {str(k): _serialize(v) for k, v in list(val.items())[:40]}
    name = getattr(val, "name", None)
    if isinstance(name, str) and not isinstance(val, (str, bytes)):
        return name
    out: dict[str, Any] = {}
    for k in _SERIALIZE_KEYS:
        if not hasattr(val, k):
            continue
        try:
            v = getattr(val, k)
        except Exception:
            continue
        if callable(v):
            continue
        try:
            if isinstance(v, (str, int, float, bool)) or v is None:
                out[k] = v
            elif hasattr(v, "name") and isinstance(getattr(v, "name", None), str):
                out[k] = v.name
            else:
                out[k] = str(v)[:120]
        except Exception:
            out[k] = "<err>"
    return out or type(val).__name__
