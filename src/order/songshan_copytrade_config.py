"""Songshan copytrade Order config · 五日累積淨比95 ∩ !mega + entry_25m_nonfail."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ORDER_YAML = ROOT / "config" / "order.yaml"


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class SongshanCopytradeOrderConfig:
    enabled: bool
    dry_run: bool
    auto_submit: bool
    quantity_shares: int  # 1 張 = 1000
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
    )
    qty = int(
        os.environ.get(
            "ORDER_SONGSHAN_COPYTRADE_QTY",
            str(raw.get("quantity_shares", 1000)),
        )
    )
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
        quantity_shares=max(1000, qty),  # 整張
        branch_id=str(raw.get("branch_id") or "9217"),
        buy_floor=float(raw.get("buy_floor") or 0.5e8),
        net_min=float(raw.get("net_min") or 0.95),
        entry_after=str(raw.get("entry_after") or "09:25"),
        entry_until=str(raw.get("entry_until") or "09:40"),
        price_type=str(raw.get("price_type") or "chase_ask"),
        market_type=str(raw.get("market_type") or "common"),
        user_def=str(raw.get("user_def") or "ss5d"),
        ledger_path=ledger,
        mega_path=mega,
        hold_days=int(raw.get("hold_days") or 7),
        submit_notify=_env_flag(
            "ORDER_SONGSHAN_COPYTRADE_SUBMIT_EMAIL",
            "1" if raw.get("submit_notify", True) else "0",
        ),
    )
