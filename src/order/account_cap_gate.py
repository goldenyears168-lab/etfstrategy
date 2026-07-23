"""Account-level cash gate · reserved buffer · sell-first policy SSOT."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from order.config import load_order_config


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class AccountRiskConfig:
    reserved_cash_twd: int
    max_daily_turnover_twd: int | None
    priority_on_conflict: str
    swap_buy_retry_polls: int


def load_account_risk_config(cfg: dict[str, Any] | None = None) -> AccountRiskConfig:
    raw = load_order_config() if cfg is None else cfg
    block = raw.get("account_risk") if isinstance(raw.get("account_risk"), dict) else {}
    if not isinstance(block, dict):
        block = {}
    max_turn = block.get("max_daily_turnover_twd")
    return AccountRiskConfig(
        reserved_cash_twd=_env_int(
            "ORDER_RESERVED_CASH_TWD",
            int(block.get("reserved_cash_twd") or 50_000),
        ),
        max_daily_turnover_twd=int(max_turn) if max_turn is not None else None,
        priority_on_conflict=str(block.get("priority_on_conflict") or "sell_first"),
        swap_buy_retry_polls=int(block.get("swap_buy_retry_polls") or 3),
    )


def spendable_cash(available_balance: int, *, cfg: AccountRiskConfig | None = None) -> int:
    risk = cfg or load_account_risk_config()
    return max(0, int(available_balance) - risk.reserved_cash_twd)


def can_afford_buy(
    *,
    notional_twd: float,
    available_balance: int,
    cfg: AccountRiskConfig | None = None,
) -> tuple[bool, str]:
    spendable = spendable_cash(available_balance, cfg=cfg)
    need = int(round(notional_twd))
    if need <= spendable:
        return True, f"ok spendable={spendable}"
    return False, f"insufficient_cash need={need} spendable={spendable}"


def sort_actions_sell_first(actions: list[Any]) -> list[Any]:
    """C18acc · sells before buys (swap / exit / entry / pyramid)."""

    def _prio(act: Any) -> tuple[int, int]:
        kind = str(getattr(act, "kind", "") or "")
        side = str(getattr(act, "side", "") or "")
        if kind in ("max_hold_exit", "extension_exit"):
            return (0, 0)
        if kind == "swap" and side == "sell":
            return (1, 0)
        if kind == "swap" and side == "buy":
            return (2, 1)
        if kind == "entry":
            return (3, 1)
        if kind == "pyramid_add":
            return (4, 1)
        return (9, 0 if side == "sell" else 1)

    return sorted(actions, key=_prio)
