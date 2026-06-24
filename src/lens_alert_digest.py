"""lens_daily_alert headline · optional email digest."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from lens_ui_copy import EMPTY_EMAIL_LIST_ZH, format_headline_zh
from stock_db import load_lens_daily_alert, upsert_lens_daily_alert

_TPE = ZoneInfo("Asia/Taipei")
_PIT_DISCLAIMER = "研究情報 · 非投資建議 · 非 live gate"


def build_lens_daily_alert_from_rows(
    lens_rows: list[Any],
    trade_date: str,
    *,
    top_n: int = 5,
) -> dict[str, Any]:
    fire_count = sum(1 for r in lens_rows if _row_attr(r, "highlight_tier") == "fire")
    delta_new_count = sum(1 for r in lens_rows if _row_bool(r, "delta_new_to_watchlist"))
    total_count = len(lens_rows)
    consensus_add_count = sum(1 for r in lens_rows if _row_bool(r, "consensus_add"))

    ranked = sorted(
        lens_rows,
        key=lambda r: (
            int(_row_bool(r, "delta_any_signal")),
            1 if _row_attr(r, "highlight_tier") == "fire" else 0,
            int(_row_attr(r, "signal_convergence") or 0),
            float(_row_attr(r, "lens_score") or 0),
        ),
        reverse=True,
    )
    items = [_row_to_item(r) for r in ranked[:top_n]]

    if fire_count and delta_new_count:
        headline = format_headline_zh(
            trade_date,
            fire_count=fire_count,
            delta_new_count=delta_new_count,
        )
    elif fire_count:
        headline = format_headline_zh(trade_date, fire_count=fire_count)
    elif delta_new_count:
        headline = format_headline_zh(trade_date, delta_new_count=delta_new_count)
    else:
        headline = format_headline_zh(trade_date)

    return {
        "trade_date": trade_date,
        "total_count": total_count,
        "fire_count": fire_count,
        "delta_new_count": delta_new_count,
        "consensus_add_count": consensus_add_count,
        "headline_zh": headline,
        "items_json": items,
        "computed_at": datetime.now(_TPE).isoformat(),
    }


def _row_attr(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _row_bool(row: Any, key: str) -> bool:
    val = _row_attr(row, key)
    if isinstance(val, bool):
        return val
    return bool(int(val or 0))


def _row_to_item(row: Any) -> dict[str, Any]:
    return {
        "stock_id": _row_attr(row, "stock_id"),
        "narrative_zh": _row_attr(row, "narrative_zh"),
        "signal_convergence": int(_row_attr(row, "signal_convergence") or 0),
        "highlight_tier": _row_attr(row, "highlight_tier"),
        "delta_any_signal": _row_bool(row, "delta_any_signal"),
        "delta_new_to_watchlist": _row_bool(row, "delta_new_to_watchlist"),
    }


def build_lens_daily_alert(
    conn: sqlite3.Connection,
    trade_date: str,
    *,
    top_n: int = 5,
) -> dict[str, Any]:
    from stock_daily_lens import build_stock_daily_lens_rows

    lens_rows = build_stock_daily_lens_rows(conn, trade_date)
    return build_lens_daily_alert_from_rows(lens_rows, trade_date, top_n=top_n)


def persist_lens_daily_alert(conn: sqlite3.Connection, trade_date: str) -> dict[str, Any]:
    alert = build_lens_daily_alert(conn, trade_date)
    upsert_lens_daily_alert(conn, alert)
    return alert


def format_alert_email(alert: dict[str, Any]) -> str:
    lines = [alert["headline_zh"], "", _PIT_DISCLAIMER, ""]
    items = alert.get("items_json") or []
    if isinstance(items, str):
        items = json.loads(items)
    for i, item in enumerate(items, 1):
        tier = item.get("highlight_tier")
        mark = " 🔥" if tier == "fire" else ""
        lines.append(f"{i}. {item.get('narrative_zh', '')}{mark}")
    if not items:
        lines.append(EMPTY_EMAIL_LIST_ZH)
    lines.extend(["", _PIT_DISCLAIMER])
    return "\n".join(lines)


def maybe_send_lens_daily_email(alert: dict[str, Any]) -> bool:
    flag = os.environ.get("RUN_LENS_DAILY_NOTIFY", "0").strip()
    if flag in ("0", "false", "False", ""):
        return False
    from notify_email import send_alert

    subject = alert["headline_zh"]
    body = format_alert_email(alert)
    send_alert(subject, body)
    return True
