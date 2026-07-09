"""ABC v3+f1 · strategy-safe OrderIntent JSON writer（勿 import order package）。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from stock_db import PROJECT_ROOT

SCHEMA_VERSION = "order-intent-v1"
ABC_V3_F1_POOL_ID = "abc-v3-f1-pullback"
ABC_V3_F1_STRATEGY_ID = "abc-v3-f1-pullback"
INTENTS_DIR = PROJECT_ROOT / "reports" / "order" / "intents"


def build_client_intent_id(
    *,
    session_date: str,
    poll_minute: str,
    symbol: str,
    strategy_id: str = ABC_V3_F1_STRATEGY_ID,
) -> str:
    safe_poll = str(poll_minute or "").replace(":", "")
    stamp = str(session_date or "").replace("-", "")
    return f"{strategy_id}_{stamp}_{safe_poll}_{str(symbol).strip()}"


def build_intent_payload(
    *,
    symbol: str,
    session_date: str,
    poll_minute: str,
    signal_price: float | None,
    stock_name: str = "",
    reason: str = "",
    budget_twd: int = 20_000,
    price_type: str = "chase_ask",
    market_type: str = "intraday_odd",
    user_def: str = ABC_V3_F1_STRATEGY_ID,
    client_intent_id: str | None = None,
) -> dict[str, Any]:
    """Build order-intent-v1 · quantity resolved at submit via budget + chase_ask."""
    cid = client_intent_id or build_client_intent_id(
        session_date=session_date, poll_minute=poll_minute, symbol=symbol
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "strategy_id": ABC_V3_F1_STRATEGY_ID,
        "as_of": session_date,
        "metadata": {
            "pool_id": ABC_V3_F1_POOL_ID,
            "poll_minute": poll_minute,
            "signal_price_kbar": signal_price,
            "stock_name": stock_name,
            "reason": reason,
            "budget_twd": int(budget_twd),
            "cancel_open_buys": True,
            "client_intent_id": cid,
            "abc_v3_f1_only": True,
            "resolve_qty_from_budget": True,
            "written_at": datetime.now().isoformat(timespec="seconds"),
        },
        "intents": [
            {
                "symbol": str(symbol).strip(),
                "side": "buy",
                "quantity_shares": 1,
                "price_type": price_type,
                "market_type": market_type,
                "time_in_force": "rod",
                "order_type": "stock",
                "user_def": cid,
                "note": f"budget={budget_twd} · qty resolved at submit · cid={cid}",
            }
        ],
    }


def write_intent_file(payload: dict[str, Any], *, session_date: str, poll_minute: str, symbol: str) -> Path:
    INTENTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = session_date.replace("-", "")
    safe_poll = poll_minute.replace(":", "")
    path = INTENTS_DIR / f"{ABC_V3_F1_STRATEGY_ID}_{stamp}_{safe_poll}_{symbol}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def update_intent_submit_status(path: Path, *, status: str, reason: str = "") -> None:
    """Mark intent after submit attempt · manual submit_intents.py must check metadata."""
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    meta["submit_status"] = status
    meta["submit_updated_at"] = datetime.now().isoformat(timespec="seconds")
    if reason:
        meta["submit_reason"] = reason
    payload["metadata"] = meta
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def format_order_log_lines(results: list[dict[str, Any]]) -> list[str]:
    if not results:
        return []
    lines = ["▸ ABC v3+f1 自動下單"]
    for row in results:
        sym = row.get("symbol") or "?"
        status = row.get("status") or "?"
        lifecycle = row.get("lifecycle_status") or ""
        if status in ("submitted", "reconciled"):
            reentry = row.get("reentry")
            extra = f" · {reentry}" if reentry else ""
            lc = f" · {lifecycle}" if lifecycle else ""
            lines.append(
                f"  · 已送單 {sym} · {row.get('quantity_shares')} 股 @ {row.get('ask_price')} "
                f"≈ {row.get('notional_twd')} NTD · 今日第 {row.get('daily_entries')} 檔"
                f"{lc}{extra}"
            )
            if int(row.get("filled_qty") or 0) > 0:
                lines.append(f"    成交 {row.get('filled_qty')} 股 · order_no={row.get('order_no')}")
        elif status == "dry_run":
            lines.append(
                f"  · dry-run {sym} · {row.get('quantity_shares')} 股 "
                f"@ {row.get('estimated_ask') or row.get('ask_price')} · {row.get('reason', '')}"
            )
        else:
            lines.append(f"  · 略過 {sym} · {row.get('reason') or status}")
    return lines
