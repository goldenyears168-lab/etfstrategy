"""Leading Dip order config · config/order.yaml + env overrides.

Dual-sleeve (not buy-signal-radar, not C18acc slots). Spec SSOT:
``research.backtest.leading_dip_sleeve_validate`` —
  * main = coverage track (含開盤 · excess vs 昨收)
  * mid  = ≥10:00 · gap∩excess vs 今開 (互補衛星)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from order.config import load_order_config
from order.fubon_orders import order_master_enabled

STRATEGY_ID = "leading-dip"
MID_STRATEGY_ID = "leading-dip-mid"
USER_DEF_PREFIX = "ldip"
MID_USER_DEF_PREFIX = "ldipm"


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


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name, "").strip()
    return raw if raw else default


@dataclass(frozen=True)
class LeadingDipOrderConfig:
    order_enabled: bool
    auto_submit: bool
    dry_run: bool
    budget_twd: int
    max_entries_per_day: int
    day_slot_cap_default: int
    day_slot_cap_busy_crash: int
    hard_day_cap: int
    total_open_cap: int
    hold_days: int
    price_type_entry: str
    price_type_exit: str
    market_type: str
    time_in_force: str
    user_def: str
    require_t2: bool  # False = coverage (含開盤) · True = quality T2+
    exclude_c18acc_slots: bool
    exclude_abc_ledger: bool
    poll_max_attempts: int
    poll_interval_sec: float
    submit_notify: bool
    # Mid sleeve (≥10:00 · open-anchor excess)
    mid_enabled: bool
    mid_strategy_id: str
    mid_user_def: str
    mid_budget_twd: int
    mid_minute_lo: str
    mid_max_entries_per_day: int
    mid_day_slot_cap_default: int
    mid_day_slot_cap_busy_crash: int
    mid_hard_day_cap: int
    mid_total_open_cap: int


def load_leading_dip_order_config(cfg: dict[str, Any] | None = None) -> LeadingDipOrderConfig:
    raw = load_order_config() if cfg is None else cfg
    strategies = raw.get("strategies") if isinstance(raw.get("strategies"), dict) else {}
    spec = strategies.get(STRATEGY_ID) if isinstance(strategies, dict) else {}
    if not isinstance(spec, dict):
        spec = {}
    slots = spec.get("slots") if isinstance(spec.get("slots"), dict) else {}
    mid = spec.get("mid") if isinstance(spec.get("mid"), dict) else {}
    mid_slots = mid.get("slots") if isinstance(mid.get("slots"), dict) else {}
    lifecycle = raw.get("lifecycle") if isinstance(raw.get("lifecycle"), dict) else {}

    yaml_enabled = bool(spec.get("enabled", False))
    order_enabled = _env_flag("ORDER_LEADING_DIP_ORDER_ENABLED", "1" if yaml_enabled else "0")
    auto_submit = _env_flag("ORDER_LEADING_DIP_AUTO_SUBMIT", "0") and order_master_enabled()
    dry_run = _env_flag("ORDER_LEADING_DIP_DRY_RUN", "1")
    mid_yaml_on = bool(mid.get("enabled", True))

    return LeadingDipOrderConfig(
        order_enabled=order_enabled,
        auto_submit=auto_submit,
        dry_run=dry_run,
        budget_twd=_env_int("ORDER_LEADING_DIP_BUDGET_TWD", int(spec.get("budget_twd") or 20_000)),
        max_entries_per_day=_env_int(
            "ORDER_LEADING_DIP_MAX_ENTRIES_DAY",
            int(spec.get("max_entries_per_day") or 4),
        ),
        day_slot_cap_default=_env_int(
            "ORDER_LEADING_DIP_DAY_CAP",
            int(slots.get("default_day_cap") or 2),
        ),
        day_slot_cap_busy_crash=_env_int(
            "ORDER_LEADING_DIP_BUSY_CRASH_CAP",
            int(slots.get("expand_cap") or 4),
        ),
        hard_day_cap=_env_int(
            "ORDER_LEADING_DIP_HARD_DAY_CAP",
            int(slots.get("hard_cap") or 4),
        ),
        total_open_cap=_env_int(
            "ORDER_LEADING_DIP_TOTAL_OPEN_CAP",
            int(slots.get("total_open_cap") or 6),
        ),
        hold_days=_env_int("ORDER_LEADING_DIP_HOLD_DAYS", int(spec.get("hold_days") or 3)),
        price_type_entry=str(spec.get("price_type") or "chase_ask"),
        price_type_exit=str(spec.get("exit_price_type") or "chase_bid"),
        market_type=str(spec.get("market_type") or "intraday_odd"),
        time_in_force=str(spec.get("time_in_force") or "rod"),
        user_def=str(spec.get("user_def") or USER_DEF_PREFIX),
        require_t2=_env_flag(
            "ORDER_LEADING_DIP_REQUIRE_T2",
            "1" if bool(spec.get("require_t2", False)) else "0",
        ),
        exclude_c18acc_slots=_env_flag("ORDER_LEADING_DIP_EXCLUDE_C18ACC", "1"),
        # ABC Order removed · old fills treated as manual; default off
        exclude_abc_ledger=_env_flag("ORDER_LEADING_DIP_EXCLUDE_ABC_LEDGER", "0"),
        poll_max_attempts=_env_int(
            "ORDER_LEADING_DIP_POLL_MAX_ATTEMPTS",
            int(lifecycle.get("poll_max_attempts") or 3),
        ),
        poll_interval_sec=float(lifecycle.get("poll_interval_sec") or 2.0),
        submit_notify=_env_flag(
            "RUN_LEADING_DIP_SUBMIT_EMAIL",
            "1" if bool(lifecycle.get("submit_notify", True)) else "0",
        ),
        mid_enabled=_env_flag("ORDER_LEADING_DIP_MID_ENABLED", "1" if mid_yaml_on else "0"),
        mid_strategy_id=str(mid.get("strategy_id") or MID_STRATEGY_ID),
        mid_user_def=str(mid.get("user_def") or MID_USER_DEF_PREFIX),
        mid_budget_twd=_env_int(
            "ORDER_LEADING_DIP_MID_BUDGET_TWD",
            int(mid.get("budget_twd") or spec.get("budget_twd") or 20_000),
        ),
        mid_minute_lo=_env_str(
            "ORDER_LEADING_DIP_MID_MINUTE_LO",
            str(mid.get("minute_lo") or "10:00"),
        ),
        mid_max_entries_per_day=_env_int(
            "ORDER_LEADING_DIP_MID_MAX_ENTRIES_DAY",
            int(mid.get("max_entries_per_day") or 2),
        ),
        mid_day_slot_cap_default=_env_int(
            "ORDER_LEADING_DIP_MID_DAY_CAP",
            int(mid_slots.get("default_day_cap") or 1),
        ),
        mid_day_slot_cap_busy_crash=_env_int(
            "ORDER_LEADING_DIP_MID_BUSY_CRASH_CAP",
            int(mid_slots.get("expand_cap") or 2),
        ),
        mid_hard_day_cap=_env_int(
            "ORDER_LEADING_DIP_MID_HARD_DAY_CAP",
            int(mid_slots.get("hard_cap") or 2),
        ),
        mid_total_open_cap=_env_int(
            "ORDER_LEADING_DIP_MID_TOTAL_OPEN_CAP",
            int(mid_slots.get("total_open_cap") or 3),
        ),
    )
