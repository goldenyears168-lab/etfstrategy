"""Post-close Supabase publish-layer health checks for Readdy."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from market_benchmark import latest_trading_date, resolve_brief_trade_date
from stock_db import DEFAULT_DB_PATH, connect
from supabase_research_sync import (
    INTRADAY_WATCH_META,
    SLOT_BRIEF_TYPES,
    _headers,
    _supabase_url,
    supabase_configured,
)

_TPE = ZoneInfo("Asia/Taipei")
_SCHEMA = "stock_research"
_EXPECTED_STRATEGY_REGISTRY = 5


@dataclass(frozen=True)
class HealthCheck:
    name: str
    ok: bool
    level: str  # ok | warn | fail
    detail: str


def _rest_table(table: str) -> str:
    base = _supabase_url().rstrip("/")
    if not base:
        raise RuntimeError("SUPABASE_URL 或 VITE_PUBLIC_SUPABASE_URL 未設定")
    return f"{base}/rest/v1/{table}"


def _get_rows(table: str, *, params: dict[str, str]) -> list[dict[str, Any]]:
    resp = requests.get(
        _rest_table(table),
        headers=_headers(),
        params=params,
        timeout=60,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"{table} GET failed: {resp.status_code} {resp.text[:400]}")
    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError(f"{table} GET returned unexpected payload")
    return data


def _env_flag(name: str) -> bool:
    val = os.environ.get(name, "").strip().lower()
    return val not in ("", "0", "false", "no")


def resolve_check_trade_date(
    conn: sqlite3.Connection,
    explicit: str | None = None,
) -> str:
    if explicit:
        day = date.fromisoformat(explicit)
        return resolve_brief_trade_date(conn, day).isoformat()
    today = datetime.now(_TPE).date()
    resolved = latest_trading_date(conn, on_or_before=today)
    if resolved:
        return resolved
    return today.isoformat()


def _check_env_flags() -> list[HealthCheck]:
    checks: list[HealthCheck] = []
    for flag, label in (
        ("RUN_SUPABASE_RESEARCH_SYNC", "daily_briefs 自動 sync"),
        ("RUN_SUPABASE_LENS_SYNC", "今日亮點自動 sync"),
    ):
        on = _env_flag(flag)
        checks.append(
            HealthCheck(
                name=f"env:{flag}",
                ok=on,
                level="warn" if not on else "ok",
                detail=f"{label} {'已開' if on else '關閉（公開站可能 stale）'}",
            )
        )
    return checks


def _check_briefs(trade_date: str, *, slot: str) -> HealthCheck:
    expected = set(SLOT_BRIEF_TYPES[slot])
    intraday = {bt for bt in expected if bt in INTRADAY_WATCH_META}
    regular = expected - intraday
    rows: list[dict[str, Any]] = []
    present: set[str] = set()

    if regular:
        regular_rows = _get_rows(
            "daily_briefs",
            params={
                "trade_date": f"eq.{trade_date}",
                "schedule_slot": f"eq.{slot}",
                "select": "brief_type,synced_at",
            },
        )
        rows.extend(regular_rows)
        present.update(str(row["brief_type"]) for row in regular_rows)

    for brief_type in sorted(intraday):
        intraday_rows = _get_rows(
            "daily_briefs",
            params={
                "schedule_slot": f"eq.{slot}",
                "brief_type": f"eq.{brief_type}",
                "snapshot_json->>session_date": f"eq.{trade_date}",
                "select": "brief_type,synced_at",
            },
        )
        if intraday_rows:
            rows.extend(intraday_rows)
            present.add(brief_type)

    missing = sorted(expected - present)
    if missing:
        return HealthCheck(
            name=f"daily_briefs:{slot}",
            ok=False,
            level="fail",
            detail=f"{trade_date} 缺 {', '.join(missing)}",
        )
    stale = [
        row["brief_type"]
        for row in rows
        if row.get("synced_at")
        and str(row["synced_at"])[:10] < trade_date
    ]
    if stale:
        return HealthCheck(
            name=f"daily_briefs:{slot}",
            ok=True,
            level="warn",
            detail=f"{trade_date} 齊全；synced_at 偏舊：{', '.join(sorted(set(stale)))}",
        )
    detail = f"{trade_date} · {len(expected)} 種 brief 齊全"
    if intraday:
        detail += f"（盤中：session_date={trade_date}）"
    return HealthCheck(
        name=f"daily_briefs:{slot}",
        ok=True,
        level="ok",
        detail=detail,
    )


def _check_highlight_rows(trade_date: str) -> HealthCheck:
    rows = _get_rows(
        "stock_daily_highlight",
        params={
            "trade_date": f"eq.{trade_date}",
            "select": "stock_id",
            "limit": "1",
        },
    )
    if not rows:
        return HealthCheck(
            name="stock_daily_highlight",
            ok=False,
            level="fail",
            detail=f"{trade_date} 無列",
        )
    count_rows = _get_rows(
        "stock_daily_highlight",
        params={
            "trade_date": f"eq.{trade_date}",
            "select": "stock_id",
        },
    )
    return HealthCheck(
        name="stock_daily_highlight",
        ok=True,
        level="ok",
        detail=f"{trade_date} · {len(count_rows)} 檔",
    )


def _check_highlight_alert(trade_date: str) -> HealthCheck:
    rows = _get_rows(
        "daily_highlight_alert",
        params={
            "trade_date": f"eq.{trade_date}",
            "select": "headline_zh",
            "limit": "1",
        },
    )
    if not rows:
        return HealthCheck(
            name="daily_highlight_alert",
            ok=False,
            level="fail",
            detail=f"{trade_date} 無 headline",
        )
    headline = str(rows[0].get("headline_zh") or "")[:60]
    return HealthCheck(
        name="daily_highlight_alert",
        ok=True,
        level="ok",
        detail=headline or f"{trade_date} 有列",
    )


def _check_strategy_registry() -> HealthCheck:
    rows = _get_rows(
        "site_content",
        params={
            "layer_id": "eq.strategy",
            "strategy_id": "not.is.null",
            "select": "strategy_id,research_page_id,page_id",
            "order": "sort_order",
        },
    )
    n = len(rows)
    if n != _EXPECTED_STRATEGY_REGISTRY:
        return HealthCheck(
            name="site_content:registry",
            ok=False,
            level="fail",
            detail=f"期望 {_EXPECTED_STRATEGY_REGISTRY} 軌 · 實際 {n} 軌",
        )
    all_pages = {
        str(row["page_id"])
        for row in _get_rows("site_content", params={"select": "page_id"})
    }
    broken = [
        str(row["strategy_id"])
        for row in rows
        if row.get("research_page_id")
        and str(row["research_page_id"]) not in all_pages
    ]
    if broken:
        return HealthCheck(
            name="site_content:registry",
            ok=False,
            level="fail",
            detail=f"research_page_id 無效：{', '.join(broken)}",
        )
    return HealthCheck(
        name="site_content:registry",
        ok=True,
        level="ok",
        detail=f"{n} 軌 registry · research_page_id 有效",
    )


def _check_signal_hits(trade_date: str) -> HealthCheck | None:
    if not (_env_flag("RUN_SUPABASE_RESEARCH_SYNC") and _env_flag("RUN_SUPABASE_SIGNAL_SYNC")):
        return None
    rows = _get_rows(
        "stock_signal_hits",
        params={
            "trade_date": f"eq.{trade_date}",
            "select": "stock_id",
            "limit": "1",
        },
    )
    if not rows:
        return HealthCheck(
            name="stock_signal_hits",
            ok=False,
            level="warn",
            detail=f"{trade_date} 無索引（搜尋頁可能空）",
        )
    return HealthCheck(
        name="stock_signal_hits",
        ok=True,
        level="ok",
        detail=f"{trade_date} 有列",
    )


def run_health_checks(
    trade_date: str,
    *,
    check_1300: bool = True,
    db_path: str | None = None,
) -> list[HealthCheck]:
    checks: list[HealthCheck] = []
    if not supabase_configured():
        checks.append(
            HealthCheck(
                name="supabase:config",
                ok=False,
                level="fail",
                detail="SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 未設定",
            )
        )
        return checks

    checks.append(
        HealthCheck(
            name="supabase:config",
            ok=True,
            level="ok",
            detail="連線設定齊全",
        )
    )
    checks.extend(_check_env_flags())

    try:
        checks.append(_check_briefs(trade_date, slot="1630"))
        if check_1300:
            checks.append(_check_briefs(trade_date, slot="1300"))
        checks.append(_check_highlight_rows(trade_date))
        checks.append(_check_highlight_alert(trade_date))
        checks.append(_check_strategy_registry())
        signal = _check_signal_hits(trade_date)
        if signal is not None:
            checks.append(signal)
    except RuntimeError as exc:
        checks.append(
            HealthCheck(
                name="supabase:query",
                ok=False,
                level="fail",
                detail=str(exc),
            )
        )

    _ = db_path  # reserved for future SQLite cross-check
    return checks


def format_report(trade_date: str, checks: list[HealthCheck]) -> str:
    icon = {"ok": "✓", "warn": "!", "fail": "✗"}
    lines = [f"Supabase 健康檢查 · trade_date={trade_date}", ""]
    for check in checks:
        lines.append(f"  {icon.get(check.level, '?')} {check.name}: {check.detail}")
    fails = sum(1 for c in checks if c.level == "fail")
    warns = sum(1 for c in checks if c.level == "warn")
    lines.append("")
    if fails:
        lines.append(f"結果：FAIL（{fails} 項失敗" + (f" · {warns} 警告" if warns else "") + "）")
    elif warns:
        lines.append(f"結果：WARN（{warns} 項警告）")
    else:
        lines.append("結果：OK")
    return "\n".join(lines)


def overall_ok(checks: list[HealthCheck]) -> bool:
    return not any(c.level == "fail" for c in checks)


def macos_notify(title: str, message: str) -> None:
    safe = message.replace('"', '\\"')[:180]
    safe_title = title.replace('"', '\\"')
    subprocess.run(
        [
            "osascript",
            "-e",
            f'display notification "{safe}" with title "{safe_title}"',
        ],
        check=False,
        capture_output=True,
    )


def run_cli(
    *,
    trade_date: str | None = None,
    check_1300: bool = True,
    notify: bool = False,
    db_path: str | None = None,
) -> int:
    conn = connect(db_path or DEFAULT_DB_PATH)
    try:
        resolved = resolve_check_trade_date(conn, trade_date)
    finally:
        conn.close()

    checks = run_health_checks(resolved, check_1300=check_1300, db_path=db_path)
    report = format_report(resolved, checks)
    print(report)

    ok = overall_ok(checks)
    if notify and not ok:
        fails = [c.name for c in checks if c.level == "fail"]
        macos_notify("ETF研究 · Supabase", f"健康檢查 FAIL · {', '.join(fails[:3])}")
    return 0 if ok else 1
