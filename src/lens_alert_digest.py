"""lens_daily_alert headline · optional email digest."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from lens_ui_copy import EMPTY_EMAIL_LIST_ZH, format_headline_zh
from stock_db import load_lens_daily_alert, load_stock_daily_lens_for_date, upsert_lens_daily_alert

_TPE = ZoneInfo("Asia/Taipei")
_PIT_DISCLAIMER = "研究情報 · PIT 快照 · 非投資建議 · 非 live gate"


def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "stock_id": row["stock_id"],
        "narrative_zh": row["narrative_zh"],
        "signal_convergence": int(row["signal_convergence"] or 0),
        "highlight_tier": row["highlight_tier"],
        "delta_any_signal": bool(int(row["delta_any_signal"] or 0)),
        "delta_new_to_watchlist": bool(int(row["delta_new_to_watchlist"] or 0)),
    }


def build_lens_daily_alert(
    conn: sqlite3.Connection,
    trade_date: str,
    *,
    top_n: int = 5,
) -> dict[str, Any]:
    lens_rows = load_stock_daily_lens_for_date(conn, trade_date)
    fire_count = sum(1 for r in lens_rows if str(r["highlight_tier"]) == "fire")
    delta_new_count = sum(1 for r in lens_rows if int(r["delta_new_to_watchlist"] or 0))

    ranked = sorted(
        lens_rows,
        key=lambda r: (
            int(r["delta_any_signal"] or 0),
            1 if str(r["highlight_tier"]) == "fire" else 0,
            int(r["signal_convergence"] or 0),
            float(r["lens_score"] or 0),
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
        "fire_count": fire_count,
        "delta_new_count": delta_new_count,
        "headline_zh": headline,
        "items_json": items,
        "computed_at": datetime.now(_TPE).isoformat(),
    }


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
