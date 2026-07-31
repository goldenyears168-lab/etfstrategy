"""ABC v3+f1 order config · config/order.yaml + env overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from order.config import load_order_config
from order.fubon_orders import order_master_enabled

ABC_V3_F1_POOL_ID = "abc-v3-f1-pullback"
ABC_V3_F1_STRATEGY_ID = "abc-v3-f1-pullback"


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class AbcV3F1OrderConfig:
    order_enabled: bool
    auto_submit: bool
    budget_twd: int
    max_entries_per_day: int
    price_type: str
    market_type: str
    time_in_force: str
    user_def: str
    cancel_open_buys: bool
    # Re-entry policy（對齊 event hold 3d · 非 skip_if_held）
    hold_days: int
    reentry_discount_pct: float
    reentry_min_trading_days: int
    max_entries_per_symbol_rolling: int
    rolling_lookback_trading_days: int
    max_notional_per_symbol_twd: int
    poll_max_attempts: int
    poll_interval_sec: float
    notional_basis: str
    submit_notify: bool


def load_abc_v3_f1_order_config(cfg: dict[str, Any] | None = None) -> AbcV3F1OrderConfig:
    raw = load_order_config() if cfg is None else cfg
    block = raw.get("strategies") if isinstance(raw.get("strategies"), dict) else {}
    spec = block.get("abc-v3-f1-pullback") if isinstance(block, dict) else {}
    if not isinstance(spec, dict):
        spec = {}
    reentry = spec.get("reentry") if isinstance(spec.get("reentry"), dict) else {}
    lifecycle = raw.get("lifecycle") if isinstance(raw.get("lifecycle"), dict) else {}

    # YAML + env. Live Order paths (buy-radar · fubon_subprocess) no longer call this
    # sleeve; retired_from_order in order.yaml keeps defaults off.
    yaml_enabled = bool(spec.get("enabled", False)) if spec else False
    order_enabled = _env_flag("ABC_V3_F1_ORDER_ENABLED", "1" if yaml_enabled else "0")
    auto_submit = _env_flag("ABC_V3_F1_AUTO_SUBMIT", "1" if yaml_enabled else "0") and order_master_enabled()
    if bool(spec.get("retired_from_order")) and not _env_flag("ABC_V3_F1_ORDER_FORCE_LEGACY", "0"):
        # Env alone cannot revive live Order; tests may set FORCE_LEGACY=1.
        order_enabled = False
        auto_submit = False
    budget = _env_int("ABC_V3_F1_BUDGET_TWD", int(spec.get("budget_twd") or 10_000))

    return AbcV3F1OrderConfig(
        order_enabled=order_enabled,
        auto_submit=auto_submit,
        budget_twd=budget,
        max_entries_per_day=_env_int(
            "ABC_V3_F1_MAX_ENTRIES_DAY", int(spec.get("max_entries_per_day") or 10)
        ),
        price_type=str(spec.get("price_type") or "chase_ask"),
        market_type=str(spec.get("market_type") or "intraday_odd"),
        time_in_force=str(spec.get("time_in_force") or "rod"),
        user_def=str(spec.get("user_def") or ABC_V3_F1_STRATEGY_ID),
        cancel_open_buys=bool(spec.get("cancel_open_buys", True)),
        hold_days=_env_int("ABC_V3_F1_HOLD_DAYS", int(reentry.get("hold_days") or spec.get("hold_days") or 3)),
        reentry_discount_pct=_env_float(
            "ABC_V3_F1_REENTRY_DISCOUNT_PCT",
            float(reentry.get("reentry_discount_pct") or 2.0),
        ),
        reentry_min_trading_days=_env_int(
            "ABC_V3_F1_REENTRY_MIN_TD",
            int(reentry.get("reentry_min_trading_days") or 1),
        ),
        max_entries_per_symbol_rolling=_env_int(
            "ABC_V3_F1_MAX_ENTRIES_SYMBOL_ROLLING",
            int(reentry.get("max_entries_per_symbol_rolling") or 3),
        ),
        rolling_lookback_trading_days=_env_int(
            "ABC_V3_F1_ROLLING_LOOKBACK_TD",
            int(reentry.get("rolling_lookback_trading_days") or 20),
        ),
        max_notional_per_symbol_twd=_env_int(
            "ABC_V3_F1_MAX_NOTIONAL_SYMBOL",
            int(reentry.get("max_notional_per_symbol_twd") or budget * 3),
        ),
        poll_max_attempts=_env_int(
            "ABC_V3_F1_POLL_MAX_ATTEMPTS",
            int(lifecycle.get("poll_max_attempts") or 3),
        ),
        poll_interval_sec=_env_float(
            "ABC_V3_F1_POLL_INTERVAL_SEC",
            float(lifecycle.get("poll_interval_sec") or 2.0),
        ),
        notional_basis=str(
            os.environ.get("ABC_V3_F1_NOTIONAL_BASIS")
            or lifecycle.get("notional_basis")
            or "filled"
        ).strip().lower(),
        submit_notify=_env_flag(
            "RUN_ABC_V3_F1_SUBMIT_EMAIL",
            "1" if bool(lifecycle.get("submit_notify", True)) else "0",
        ),
    )
