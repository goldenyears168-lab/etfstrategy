"""Probe domestic mutual fund monthly disclosure and notify on new snapshots."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

import requests

from notify_email import send_alert
from stock_db import connect, list_mutual_fund_snapshot_dates
from sync_mutual_fund_holdings import (
    ALLIANZ_TW_TECH,
    DISCLOSURE_MONTHLY,
    FundProfile,
    fetch_moneydj_holdings,
    resolve_profile,
    sync_latest_moneydj,
)

WatchStatus = Literal["unchanged", "new", "error"]


@dataclass(frozen=True)
class DisclosureWatchResult:
    status: WatchStatus
    fund_code: str
    fund_name: str
    db_latest: str | None = None
    remote_latest: str | None = None
    remote_source: str | None = None
    holdings_written: int = 0
    top_holdings: tuple[tuple[str, str | None, float | None], ...] = ()
    error: str | None = None


def _email_enabled() -> bool:
    return os.environ.get("RUN_MUTUAL_FUND_DISCLOSURE_EMAIL", "1").strip() not in (
        "0",
        "false",
        "False",
    )


def db_latest_monthly_snapshot(conn, fund_code: str) -> str | None:
    dates = list_mutual_fund_snapshot_dates(
        conn,
        fund_code,
        disclosure_type=DISCLOSURE_MONTHLY,
    )
    return dates[0] if dates else None


def probe_remote_latest(
    profile: FundProfile,
    session: requests.Session | None = None,
) -> tuple[str, list[dict], float | None, str]:
    snapshot_date, rows, fund_size = fetch_moneydj_holdings(profile, session)
    return snapshot_date, rows, fund_size, "moneydj_wr04"


def run_disclosure_watch(
    conn,
    profile: FundProfile,
    *,
    sync_on_new: bool = True,
) -> DisclosureWatchResult:
    db_latest = db_latest_monthly_snapshot(conn, profile.fund_code)
    try:
        remote_date, rows, _fund_size, source = probe_remote_latest(profile)
    except Exception as exc:
        return DisclosureWatchResult(
            status="error",
            fund_code=profile.fund_code,
            fund_name=profile.fund_name,
            db_latest=db_latest,
            error=str(exc),
        )

    if not rows:
        return DisclosureWatchResult(
            status="error",
            fund_code=profile.fund_code,
            fund_name=profile.fund_name,
            db_latest=db_latest,
            remote_latest=remote_date,
            error="remote holdings empty",
        )

    top_holdings = tuple(
        (row["stock_id"], row.get("stock_name"), row.get("weight_pct"))
        for row in rows[:5]
    )

    if db_latest and remote_date <= db_latest:
        return DisclosureWatchResult(
            status="unchanged",
            fund_code=profile.fund_code,
            fund_name=profile.fund_name,
            db_latest=db_latest,
            remote_latest=remote_date,
            remote_source=source,
            top_holdings=top_holdings,
        )

    holdings_written = 0
    if sync_on_new:
        holdings_written = sync_latest_moneydj(conn, profile)

    return DisclosureWatchResult(
        status="new",
        fund_code=profile.fund_code,
        fund_name=profile.fund_name,
        db_latest=db_latest,
        remote_latest=remote_date,
        remote_source=source,
        holdings_written=holdings_written,
        top_holdings=top_holdings,
    )


def format_new_disclosure_body(result: DisclosureWatchResult) -> str:
    lines = [
        f"{result.fund_name}（{result.fund_code}）月前十大持股已公布。",
        f"快照日：{result.remote_latest}",
        f"資料庫先前最新：{result.db_latest or '（無）'}",
        f"來源：{result.remote_source}",
        f"寫入筆數：{result.holdings_written}",
        "",
        "前五大：",
    ]
    for stock_id, stock_name, weight_pct in result.top_holdings:
        suffix = f" {weight_pct:.2f}%" if weight_pct is not None else ""
        name = stock_name or ""
        lines.append(f"  {stock_id} {name}{suffix}".rstrip())
    lines.append("")
    lines.append("已同步至 SQLite mutual_fund_holdings。")
    return "\n".join(lines)


def maybe_send_new_disclosure_alert(result: DisclosureWatchResult) -> bool:
    if result.status != "new" or not _email_enabled():
        return False
    subject = f"[ETF研究] {result.fund_name} 月報 {result.remote_latest}"
    send_alert(subject, format_new_disclosure_body(result))
    return True


def watch_fund(
    fund_code: str = ALLIANZ_TW_TECH.fund_code,
    *,
    db_path=None,
    sync_on_new: bool = True,
    notify: bool = True,
) -> DisclosureWatchResult:
    profile = resolve_profile(fund_code)
    conn = connect(db_path) if db_path is not None else connect()
    try:
        result = run_disclosure_watch(conn, profile, sync_on_new=sync_on_new)
        if notify:
            maybe_send_new_disclosure_alert(result)
        return result
    finally:
        conn.close()
