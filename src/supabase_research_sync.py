"""Sync scheduled research briefs (13:00 / 16:30) to Supabase PostgREST."""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from market_benchmark import is_trading_date, latest_trading_date, resolve_brief_trade_date
from stock_db import DEFAULT_DB_PATH, PROJECT_ROOT, connect
from strategy_backtest_reference import build_backtest_reference

_TPE = ZoneInfo("Asia/Taipei")

# brief_type → (schedule_slot, candidate paths relative to project root)
# strategy screen briefs · layer strategy · snapshot_json.strategy_id for Readdy routing
BRIEF_CATALOG: dict[str, tuple[str, tuple[str, ...]]] = {
    "vcp_funnel_specs": (
        "1300",
        (
            "reports/daily/{date}_vcp_funnel_specs_daily_brief.md",
            "reports/daily/vcp_funnel_specs_daily_brief.md",
        ),
    ),
    "vcp_pivot_gate": (
        "1300",
        (
            "reports/daily/{date}_vcp_pivot_gate_daily_brief.md",
            "reports/daily/vcp_pivot_gate_daily_brief.md",
        ),
    ),
    "vcp_coil_close": (
        "1300",
        (
            "reports/daily/{date}_vcp_coil_close_daily_brief.md",
            "reports/daily/vcp_coil_close_daily_brief.md",
        ),
    ),
    "rrg_mono_intraday": (
        "1300",
        (
            "reports/daily/{date}_rrg_mono_intraday_watch.md",
            "reports/daily/rrg_mono_intraday_watch.md",
            "reports/{date}_rrg_mono_intraday_watch.md",
            "reports/rrg_mono_intraday_watch.md",
        ),
    ),
    "etf_daily": (
        "1630",
        (
            "reports/daily/{date}_etf_daily.md",
            "reports/daily/etf-daily/daily_brief.md",
        ),
    ),
    "regime_daily": (
        "1630",
        (
            "reports/daily/regime/snapshots/{date}/daily_brief.md",
            "reports/daily/regime/daily_brief.md",
        ),
    ),
    "rrg_mono_daily": (
        "1630",
        (
            "reports/daily/{date}_rrg_mono_daily.md",
            "reports/daily/rrg_mono_daily.md",
        ),
    ),
    "copytrade_l1h9": (
        "1630",
        (
            "reports/daily/{date}_copytrade_l1h9_daily.md",
            "reports/daily/copytrade_l1h9_daily.md",
        ),
    ),
}

# Readdy · brief_type → adopted strategy_id (site_content strategy_* pages)
STRATEGY_SCREEN_META: dict[str, dict[str, str]] = {
    "copytrade_l1h9": {
        "strategy_id": "00981a-l1h9",
        "title_zh": "ETF00981A 跟單策略",
        "layer": "strategy",
    },
    "rrg_mono_daily": {
        "strategy_id": "rrg-mono-hold7",
        "title_zh": "RRG 單軌（持7日）",
        "layer": "strategy",
    },
    "vcp_pivot_gate": {
        "strategy_id": "vcp-pivot-gate",
        "title_zh": "VCP 突破確認",
        "layer": "strategy",
    },
    "vcp_coil_close": {
        "strategy_id": "vcp-coil-close",
        "title_zh": "VCP 訊號收盤",
        "layer": "strategy",
    },
    "vcp_funnel_specs": {
        "strategy_id": "vcp-pivot-gate",
        "title_zh": "VCP 漏斗研究",
        "layer": "research",
    },
}

# 盤中預警 · 非收盤策略掃描；snapshot_json.contract 供 Readdy 分區
INTRADAY_WATCH_META: dict[str, dict[str, str]] = {
    "rrg_mono_intraday": {
        "strategy_id": "rrg-mono-hold7",
        "title_zh": "RRG 盤中預警",
        "layer": "strategy",
    },
}

SLOT_BRIEF_TYPES: dict[str, tuple[str, ...]] = {
    "1300": (
        "vcp_funnel_specs",
        "vcp_pivot_gate",
        "vcp_coil_close",
        "rrg_mono_intraday",
    ),
    "1630": (
        "etf_daily",
        "regime_daily",
        "rrg_mono_daily",
        "copytrade_l1h9",
    ),
}


@dataclass(frozen=True)
class BriefRecord:
    trade_date: date
    schedule_slot: str
    brief_type: str
    title: str
    content_md: str
    source_path: str
    content_html: str | None = None
    snapshot_json: dict[str, object] | None = None


@dataclass(frozen=True)
class SyncResult:
    uploaded: list[str]
    skipped: list[str]
    errors: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def supabase_configured() -> bool:
    return bool(_supabase_url() and _env("SUPABASE_SERVICE_ROLE_KEY"))


def _supabase_url() -> str:
    return _env("SUPABASE_URL") or _env("VITE_PUBLIC_SUPABASE_URL")


_SUPABASE_SCHEMA = "stock_research"
_SUPABASE_TABLE = "daily_briefs"


def _headers() -> dict[str, str]:
    key = _env("SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY 未設定（見 .env.example）")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
        "Accept-Profile": _SUPABASE_SCHEMA,
        "Content-Profile": _SUPABASE_SCHEMA,
    }


def _rest_url() -> str:
    base = _supabase_url().rstrip("/")
    if not base:
        raise RuntimeError(
            "SUPABASE_URL 或 VITE_PUBLIC_SUPABASE_URL 未設定（見 .env.example）"
        )
    return f"{base}/rest/v1/{_SUPABASE_TABLE}"


def _today_tpe() -> date:
    return datetime.now(_TPE).date()


def allow_scheduled_supabase_push(
    conn: sqlite3.Connection,
    *,
    scheduled: bool = True,
) -> bool:
    """排程推送僅在台股交易日執行；backfill 傳 scheduled=False。"""
    if not scheduled:
        return True
    return is_trading_date(conn, _today_tpe())


def _extract_title(content: str, fallback: str) -> str:
    for line in content.splitlines():
        text = line.strip()
        if text.startswith("# "):
            return text[2:].strip()
    return fallback


def _extract_trade_date(content: str, brief_type: str, fallback: date) -> date:
    patterns = (
        r"(\d{4}-\d{2}-\d{2})",
        r"(\d{8})",
    )
    for pattern in patterns:
        match = re.search(pattern, content[:500])
        if not match:
            continue
        raw = match.group(1)
        if len(raw) == 8:
            return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        return date.fromisoformat(raw)
    return fallback


def _resolve_path(template: str, trade_date: date) -> Path:
    stamp = trade_date.strftime("%Y%m%d")
    iso = trade_date.isoformat()
    rel = template.format(date=stamp, iso=iso)
    return PROJECT_ROOT / rel


def _is_dated_template(template: str) -> bool:
    return "{date}" in template or "{iso}" in template


def _brief_templates(brief_type: str, trade_date: date | None) -> tuple[str, ...]:
    _, templates = BRIEF_CATALOG[brief_type]
    dated = tuple(t for t in templates if _is_dated_template(t))
    undated = tuple(t for t in templates if not _is_dated_template(t))
    if trade_date is None:
        return undated + dated
    day = trade_date
    if day == _today_tpe():
        return dated + undated
    return dated


def _find_brief_file(brief_type: str, trade_date: date | None = None) -> Path | None:
    if brief_type not in BRIEF_CATALOG:
        return None
    day = trade_date or _today_tpe()
    for template in _brief_templates(brief_type, trade_date):
        path = _resolve_path(template, day)
        if path.is_file():
            return path
    return None


def _regime_snapshot_json_for(
    day: date, db_path: Path | None = None
) -> dict[str, object] | None:
    from regime_snapshot_json import build_regime_snapshot_json

    conn = connect(db_path or DEFAULT_DB_PATH)
    try:
        return build_regime_snapshot_json(conn, day.isoformat())
    finally:
        conn.close()


def _default_lookup_date(conn: sqlite3.Connection) -> date:
    """Sync 未指定 --date 時：用最近交易日，不用日曆今天（週末不產新日報）。"""
    return resolve_brief_trade_date(conn, _today_tpe())


def _intraday_data_baseline(conn: sqlite3.Connection, session_date: date) -> date:
    """盤中 brief 的 trade_date = 收盤 panel 基準日（非產出日／session 日）。"""
    if is_trading_date(conn, session_date):
        prev = latest_trading_date(
            conn, on_or_before=session_date - timedelta(days=1)
        )
        if prev:
            return date.fromisoformat(prev)
    resolved = latest_trading_date(conn, on_or_before=session_date)
    if resolved:
        return date.fromisoformat(resolved)
    return session_date


def _intraday_session_date(content: str, fallback: date) -> date:
    """從 MD 標題取 session（產出日）；fallback 為檔案 lookup。"""
    for line in content.splitlines()[:5]:
        text = line.strip()
        if not text.startswith("# "):
            continue
        match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        if match:
            return date.fromisoformat(match.group(1))
    return fallback


def load_brief(
    brief_type: str,
    trade_date: date | None = None,
    *,
    db_path: Path | None = None,
) -> BriefRecord | None:
    conn = connect(db_path or DEFAULT_DB_PATH)
    try:
        lookup = (
            resolve_brief_trade_date(conn, trade_date)
            if trade_date is not None
            else _default_lookup_date(conn)
        )
        file_lookup = trade_date if brief_type in INTRADAY_WATCH_META and trade_date else lookup
        path = _find_brief_file(brief_type, file_lookup)
        if path is None:
            return None
        slot, _ = BRIEF_CATALOG[brief_type]
        content = path.read_text(encoding="utf-8")
        if brief_type in INTRADAY_WATCH_META:
            if trade_date is not None:
                session_day = trade_date
            else:
                session_day = _intraday_session_date(content, lookup)
            day = _intraday_data_baseline(conn, session_day)
        else:
            if trade_date is not None:
                raw_day = trade_date
            else:
                raw_day = _extract_trade_date(content, brief_type, lookup)
            day = resolve_brief_trade_date(conn, raw_day)
        title = _extract_title(content, brief_type)
        html_path = path.parent / "daily_brief.embed.html"
        if not html_path.is_file() and brief_type == "regime_daily":
            html_path = path.parent.parent / "daily_brief.embed.html"
        if not html_path.is_file():
            html_path = path.with_suffix(".html")
        html = html_path.read_text(encoding="utf-8") if html_path.is_file() else None
        snapshot_json: dict[str, object] | None = None
        if brief_type == "regime_daily":
            from regime_snapshot_json import build_regime_snapshot_json

            snapshot_json = build_regime_snapshot_json(conn, day.isoformat())
        elif brief_type in INTRADAY_WATCH_META:
            session_day = _intraday_session_date(content, lookup)
            meta = INTRADAY_WATCH_META[brief_type]
            snapshot_json = {
                "contract": "intraday-watch-v1",
                "session_date": session_day.isoformat(),
                "data_baseline_date": day.isoformat(),
                **meta,
            }
            ref = build_backtest_reference(meta["strategy_id"], conn)
            if ref:
                snapshot_json["backtest_reference"] = ref
        elif brief_type in STRATEGY_SCREEN_META:
            meta = STRATEGY_SCREEN_META[brief_type]
            snapshot_json = {
                "contract": "strategy-screen-v1",
                **meta,
            }
            ref = build_backtest_reference(meta["strategy_id"], conn)
            if ref:
                snapshot_json["backtest_reference"] = ref
        return BriefRecord(
            trade_date=day,
            schedule_slot=slot,
            brief_type=brief_type,
            title=title,
            content_md=content,
            source_path=str(path.relative_to(PROJECT_ROOT)),
            content_html=html,
            snapshot_json=snapshot_json,
        )
    finally:
        conn.close()


def _record_payload(record: BriefRecord) -> dict[str, object]:
    payload: dict[str, object] = {
        "trade_date": record.trade_date.isoformat(),
        "schedule_slot": record.schedule_slot,
        "brief_type": record.brief_type,
        "title": record.title,
        "content_md": record.content_md,
        "source_path": record.source_path,
        "synced_at": datetime.now(_TPE).isoformat(),
    }
    if record.content_html:
        payload["content_html"] = record.content_html
    if record.snapshot_json is not None:
        payload["snapshot_json"] = record.snapshot_json
    return payload


def _delete_intraday_miskeyed(record: BriefRecord) -> None:
    """Remove rows keyed by session/production date (pre-fix orphan keys)."""
    if record.brief_type not in INTRADAY_WATCH_META:
        return
    sj = record.snapshot_json or {}
    session = sj.get("session_date")
    baseline = record.trade_date.isoformat()
    if not session or session == baseline:
        return
    resp = requests.delete(
        _rest_url(),
        headers=_headers(),
        params={
            "brief_type": f"eq.{record.brief_type}",
            "trade_date": f"eq.{session}",
        },
        timeout=60,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Supabase delete stale intraday failed ({record.brief_type}): "
            f"{resp.status_code} {resp.text[:500]}"
        )


def upsert_brief(record: BriefRecord) -> None:
    resp = requests.post(
        _rest_url(),
        headers=_headers(),
        json=_record_payload(record),
        params={"on_conflict": "trade_date,brief_type"},
        timeout=120,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Supabase upsert failed ({record.brief_type}): "
            f"{resp.status_code} {resp.text[:500]}"
        )
    _delete_intraday_miskeyed(record)


def sync_slot(schedule_slot: str, trade_date: date | None = None) -> SyncResult:
    if schedule_slot not in SLOT_BRIEF_TYPES:
        return SyncResult([], [], [f"unknown slot: {schedule_slot}"])
    if not supabase_configured():
        return SyncResult([], list(SLOT_BRIEF_TYPES[schedule_slot]), [])

    conn = connect()
    try:
        if trade_date is None and not allow_scheduled_supabase_push(conn):
            label = f"non-trading-day ({_today_tpe().isoformat()})"
            return SyncResult([], [label], [])
    finally:
        conn.close()

    uploaded: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    for brief_type in SLOT_BRIEF_TYPES[schedule_slot]:
        try:
            record = load_brief(brief_type, trade_date)
            if record is None:
                skipped.append(brief_type)
                continue
            upsert_brief(record)
            uploaded.append(brief_type)
        except Exception as exc:
            errors.append(f"{brief_type}: {exc}")

    return SyncResult(uploaded, skipped, errors)


def sync_all(trade_date: date | None = None) -> SyncResult:
    merged = SyncResult([], [], [])
    for slot in ("1300", "1630"):
        result = sync_slot(slot, trade_date)
        merged = SyncResult(
            merged.uploaded + result.uploaded,
            merged.skipped + result.skipped,
            merged.errors + result.errors,
        )
    return merged


def discover_report_dates(days: int = 14) -> list[date]:
    """Collect trade dates from on-disk report files (no SQLite)."""
    end = _today_tpe()
    start = end - timedelta(days=days)
    found: set[date] = set()

    snap_root = PROJECT_ROOT / "reports/daily/regime/snapshots"
    if snap_root.is_dir():
        for child in snap_root.iterdir():
            if not child.is_dir() or len(child.name) != 8 or not child.name.isdigit():
                continue
            day = date(int(child.name[:4]), int(child.name[4:6]), int(child.name[6:8]))
            if start <= day <= end and (child / "daily_brief.md").is_file():
                found.add(day)

    daily_dir = PROJECT_ROOT / "reports/daily"
    if daily_dir.is_dir():
        for path in daily_dir.glob("*_*.md"):
            stamp = path.name.split("_", 1)[0]
            if len(stamp) != 8 or not stamp.isdigit():
                continue
            day = date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))
            if start <= day <= end:
                found.add(day)

    return sorted(found)


def backfill(days: int = 14) -> SyncResult:
    """Upload existing report MD/HTML files to Supabase (website payload only)."""
    if not supabase_configured():
        return SyncResult([], [], ["Supabase 未設定"])

    uploaded: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    for trade_date in discover_report_dates(days):
        label = trade_date.isoformat()
        for brief_type in BRIEF_CATALOG:
            key = f"{label}/{brief_type}"
            try:
                record = load_brief(brief_type, trade_date)
                if record is None:
                    skipped.append(key)
                    continue
                if brief_type in INTRADAY_WATCH_META:
                    sj = record.snapshot_json or {}
                    if sj.get("session_date") != trade_date.isoformat():
                        skipped.append(
                            f"{key} (session mismatch {sj.get('session_date')})"
                        )
                        continue
                elif record.trade_date != trade_date:
                    skipped.append(f"{key} (date mismatch {record.trade_date})")
                    continue
                upsert_brief(record)
                uploaded.append(key)
            except Exception as exc:
                errors.append(f"{key}: {exc}")

    return SyncResult(uploaded, skipped, errors)


def dashboard_url() -> str:
    base = _supabase_url().rstrip("/")
    ref = _env("SUPABASE_PROJECT_REF")
    if ref:
        return f"https://supabase.com/dashboard/project/{ref}/editor"
    return base or "(未設定 SUPABASE_URL)"
