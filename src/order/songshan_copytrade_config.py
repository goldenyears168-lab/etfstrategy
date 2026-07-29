"""Songshan copytrade Order config · 五日累積淨比95 ∩ !mega + entry_25m_nonfail."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from order.fubon_orders import order_master_enabled

ROOT = Path(__file__).resolve().parents[2]
ORDER_YAML = ROOT / "config" / "order.yaml"


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class SongshanCopytradeOrderConfig:
    enabled: bool
    dry_run: bool
    auto_submit: bool
    budget_twd: float | None  # 預算制（優先；None = 用 quantity_shares）
    quantity_shares: int | None  # 固定股數（fallback；None = 用 budget_twd）
    branch_id: str
    buy_floor: float
    net_min: float
    entry_after: str  # HH:MM local · 25m nonfail gate
    entry_until: str
    price_type: str
    market_type: str
    user_def: str
    ledger_path: Path
    mega_path: Path
    hold_days: int
    submit_notify: bool
    allow_pyramid: bool  # False = never rebuy symbol already submitted/filled/working


def load_songshan_copytrade_order_config() -> SongshanCopytradeOrderConfig:
    raw: dict = {}
    if ORDER_YAML.is_file():
        data = yaml.safe_load(ORDER_YAML.read_text(encoding="utf-8")) or {}
        raw = dict((data.get("strategies") or {}).get("songshan-copytrade") or {})
    enabled = _env_flag(
        "ORDER_SONGSHAN_COPYTRADE_ENABLED",
        "1" if raw.get("enabled", False) else "0",
    )
    dry = _env_flag(
        "ORDER_SONGSHAN_COPYTRADE_DRY_RUN",
        "0" if raw.get("dry_run") is False else "1",
    )
    auto = _env_flag(
        "ORDER_SONGSHAN_COPYTRADE_AUTO_SUBMIT",
        "1" if raw.get("auto_submit", True) else "0",
    ) and order_master_enabled()
    
    # 預算制優先（env > yaml）
    budget_env = os.environ.get("ORDER_SONGSHAN_COPYTRADE_BUDGET_TWD", "").strip()
    budget_yaml = raw.get("budget_twd")
    if budget_env:
        budget = float(budget_env)
    elif budget_yaml is not None:
        budget = float(budget_yaml)
    else:
        budget = None
    
    # 固定股數 fallback
    qty_env = os.environ.get("ORDER_SONGSHAN_COPYTRADE_QTY", "").strip()
    qty_yaml = raw.get("quantity_shares")
    if qty_env:
        qty = int(qty_env)
    elif qty_yaml is not None:
        qty = int(qty_yaml)
    else:
        qty = None
    
    # 至少一個必須有值
    if budget is None and qty is None:
        budget = 100000.0  # 預設 10 萬
    
    ledger = Path(
        str(
            raw.get("ledger")
            or "data/order/songshan_copytrade_ledger.json"
        )
    )
    if not ledger.is_absolute():
        ledger = ROOT / ledger
    mega = Path(
        str(
            raw.get("mega_path")
            or "reports/research/branch-footprint-screen/ab58_xMega_copytrade/mega_blacklist_v1.json"
        )
    )
    if not mega.is_absolute():
        mega = ROOT / mega
    return SongshanCopytradeOrderConfig(
        enabled=enabled,
        dry_run=dry,
        auto_submit=auto,
        budget_twd=budget,
        quantity_shares=qty,
        branch_id=str(raw.get("branch_id") or "9217"),
        buy_floor=float(raw.get("buy_floor") or 0.5e8),
        net_min=float(raw.get("net_min") or 0.95),
        entry_after=str(raw.get("entry_after") or "09:25"),
        entry_until=str(raw.get("entry_until") or "09:40"),
        price_type=str(raw.get("price_type") or "chase_ask"),
        market_type=str(raw.get("market_type") or "intraday_odd"),
        user_def=str(raw.get("user_def") or "ss5d"),
        ledger_path=ledger,
        mega_path=mega,
        hold_days=int(raw.get("hold_days") or 7),
        submit_notify=_env_flag(
            "ORDER_SONGSHAN_COPYTRADE_SUBMIT_EMAIL",
            "1" if raw.get("submit_notify", True) else "0",
        ),
        # Default off: no Songshan pyramid research adopted; block rebuy if already bought.
        allow_pyramid=_env_flag(
            "ORDER_SONGSHAN_COPYTRADE_ALLOW_PYRAMID",
            "1" if raw.get("allow_pyramid", False) else "0",
        ),
    )
