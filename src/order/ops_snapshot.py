"""Minimal order-ops snapshot · Phase 2 lite（唯讀 JSON，非完整 OMS UI）。"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from stock_db import PROJECT_ROOT

_TZ = ZoneInfo("Asia/Taipei")
SNAPSHOT_DIR = PROJECT_ROOT / "reports" / "order" / "snapshots"
SCHEMA = "order-ops-snapshot-v1"


def _env_flag(name: str, default: str = "0") -> bool:
    return str(os.environ.get(name, default)).strip().lower() in {"1", "true", "yes", "on"}


def _ledger_summary(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"path": str(path), "exists": False, "entries": 0, "open": 0}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"path": str(path), "exists": True, "error": "unreadable"}
    entries = raw.get("entries") if isinstance(raw, dict) else []
    if not isinstance(entries, list):
        entries = []
    open_n = sum(
        1
        for e in entries
        if isinstance(e, dict)
        and str(e.get("lifecycle_status") or "") in {"working", "submitted", "partial", "ambiguous"}
    )
    return {"path": str(path), "exists": True, "entries": len(entries), "open": open_n}


def _leading_dip_ledger_summary(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"path": str(path), "exists": False, "positions": 0, "open": 0}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"path": str(path), "exists": True, "error": "unreadable"}
    positions = raw.get("positions") if isinstance(raw, dict) else []
    if not isinstance(positions, list):
        positions = []
    closed = {"closed", "failed", "cancelled", "skipped"}
    open_n = sum(
        1 for p in positions if isinstance(p, dict) and str(p.get("status") or "") not in closed
    )
    return {"path": str(path), "exists": True, "positions": len(positions), "open": open_n}


def build_order_ops_snapshot(*, as_of: datetime | None = None) -> dict[str, Any]:
    now = as_of or datetime.now(tz=_TZ)
    day = now.strftime("%Y-%m-%d")
    return {
        "schema": SCHEMA,
        "as_of": now.isoformat(timespec="seconds"),
        "session_date": day,
        "switches": {
            "ORDER_LIVE_FORBIDDEN": _env_flag("ORDER_LIVE_FORBIDDEN"),
            "ORDER_C18ACC_DRY_RUN": _env_flag("ORDER_C18ACC_DRY_RUN"),
            "ORDER_C18ACC_AUTO_SUBMIT": _env_flag("ORDER_C18ACC_AUTO_SUBMIT", "1"),
            "ORDER_LEADING_DIP_ORDER_ENABLED": _env_flag("ORDER_LEADING_DIP_ORDER_ENABLED", "0"),
            "ORDER_LEADING_DIP_DRY_RUN": _env_flag("ORDER_LEADING_DIP_DRY_RUN", "1"),
            "ORDER_LEADING_DIP_AUTO_SUBMIT": _env_flag("ORDER_LEADING_DIP_AUTO_SUBMIT", "0"),
            "C18ACC_NO_TRADE_BEFORE": os.environ.get("C18ACC_NO_TRADE_BEFORE", "13:00"),
            # ABC switches retained as telemetry only（sleeve retired · expect false）
            "ABC_V3_F1_ORDER_ENABLED": _env_flag("ABC_V3_F1_ORDER_ENABLED", "0"),
            "ABC_ORDER_RETIRED": True,
        },
        "ledgers": {
            "c18acc": _ledger_summary(PROJECT_ROOT / "data" / "order" / "c18acc_order_ledger.json"),
            "leading_dip": _leading_dip_ledger_summary(
                PROJECT_ROOT / "data" / "order" / "leading_dip_ledger.json"
            ),
            "leading_dip_mid": _leading_dip_ledger_summary(
                PROJECT_ROOT / "data" / "order" / "leading_dip_mid_ledger.json"
            ),
        },
        "logs": {
            "c18_poll": str(PROJECT_ROOT / "logs" / "intraday" / "launchd_rrg-c18acc-poll.log"),
            "buy_radar": str(PROJECT_ROOT / "logs" / "intraday" / "launchd_buy-signal-radar.log"),
            "leading_dip": str(
                PROJECT_ROOT / "logs" / "intraday" / "launchd_leading-dip-poll.log"
            ),
        },
        "notes": [
            "Snapshot is read-only · not a broker reconciliation.",
            "Live Order sleeves: C18acc + Leading Dip. ABC removed from Order · manual holdings.",
        ],
    }


def write_order_ops_snapshot(*, out_dir: Path | None = None) -> Path:
    snap = build_order_ops_snapshot()
    dest_dir = out_dir or SNAPSHOT_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{snap['session_date'].replace('-', '')}_ops.json"
    path.write_text(json.dumps(snap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    latest = dest_dir / "latest_ops.json"
    latest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return path
