"""Website layer VFP · reports/publish/ (SSOT for web + Supabase sync)."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from stock_db import PROJECT_ROOT

_TPE = ZoneInfo("Asia/Taipei")

REPORTS_PUBLISH = PROJECT_ROOT / "reports" / "publish"
PUBLISH_FACTS_ETF = REPORTS_PUBLISH / "facts" / "etf-daily"
PUBLISH_REGIME = REPORTS_PUBLISH / "regime"
PUBLISH_RESEARCH_VCP = REPORTS_PUBLISH / "research" / "vcp_funnel_specs"
PUBLISH_STRATEGY = REPORTS_PUBLISH / "strategy"

BRIEF_TYPE_DIRS: dict[str, Path] = {
    "etf_daily": PUBLISH_FACTS_ETF,
    "regime_daily": PUBLISH_REGIME,
    "vcp_funnel_specs": PUBLISH_RESEARCH_VCP,
}


def ensure_publish_dirs() -> None:
    for path in (
        PUBLISH_FACTS_ETF,
        PUBLISH_REGIME / "snapshots",
        PUBLISH_RESEARCH_VCP,
        PUBLISH_STRATEGY,
    ):
        path.mkdir(parents=True, exist_ok=True)


def _stamp(day: date | str) -> str:
    if isinstance(day, date):
        return day.strftime("%Y%m%d")
    return day.replace("-", "")


def _iso(day: date | str) -> str:
    if isinstance(day, date):
        return day.isoformat()
    return day


def publish_etf_daily(content_md: str, trade_date: date | str) -> list[Path]:
    """Write latest + dated archive under publish/facts/etf-daily/."""
    ensure_publish_dirs()
    stamp = _stamp(trade_date)
    written: list[Path] = []
    latest = PUBLISH_FACTS_ETF / "daily_brief.md"
    dated = PUBLISH_FACTS_ETF / f"{stamp}.md"
    latest.write_text(content_md, encoding="utf-8")
    dated.write_text(content_md, encoding="utf-8")
    written.extend([latest, dated])
    return written


def publish_regime_daily(
    content_md: str,
    trade_date: date | str,
    *,
    content_html: str | None = None,
    embed_html: str | None = None,
) -> list[Path]:
    ensure_publish_dirs()
    stamp = _stamp(trade_date)
    snap = PUBLISH_REGIME / "snapshots" / stamp
    snap.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    snap_md = snap / "daily_brief.md"
    snap_md.write_text(content_md, encoding="utf-8")
    written.append(snap_md)

    if content_html:
        p = snap / "daily_brief.html"
        p.write_text(content_html, encoding="utf-8")
        written.append(p)
    if embed_html:
        p = snap / "daily_brief.embed.html"
        p.write_text(embed_html, encoding="utf-8")
        written.append(p)

    latest_md = PUBLISH_REGIME / "daily_brief.md"
    latest_md.write_text(content_md, encoding="utf-8")
    written.append(latest_md)
    if content_html:
        p = PUBLISH_REGIME / "daily_brief.html"
        p.write_text(content_html, encoding="utf-8")
        written.append(p)
    if embed_html:
        p = PUBLISH_REGIME / "daily_brief.embed.html"
        p.write_text(embed_html, encoding="utf-8")
        written.append(p)

    return written


def publish_vcp_funnel_specs(content_md: str, trade_date: date | str) -> Path:
    ensure_publish_dirs()
    path = PUBLISH_RESEARCH_VCP / f"{_stamp(trade_date)}.md"
    path.write_text(content_md, encoding="utf-8")
    return path


def publish_strategy_catalog(content_md: str) -> Path:
    ensure_publish_dirs()
    path = PUBLISH_STRATEGY / "catalog.md"
    path.write_text(content_md, encoding="utf-8")
    return path


def build_strategy_catalog_markdown() -> str:
    """Render strategy/catalog.md from config/strategy.yaml."""
    import yaml

    cfg_path = PROJECT_ROOT / "config" / "strategy.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    principles = raw.get("principles") or []
    strategies = raw.get("strategies") or {}

    lines = [
        "# Strategy layer · 策略層 catalog",
        "",
        "SSOT：`config/strategy.yaml` · frozen specs · parallel strategies · no ensemble.",
        "",
        "## Principles",
        "",
    ]
    for p in principles:
        lines.append(f"- {p}")
    lines.extend(["", "## Strategies", ""])
    for sid, spec in strategies.items():
        if not isinstance(spec, dict):
            continue
        title = spec.get("title") or sid
        enabled = spec.get("enabled", False)
        schedule = spec.get("schedule") or "—"
        desc = (spec.get("description") or "").strip().replace("\n", " ")
        lines.append(f"### {title}")
        lines.append("")
        lines.append(f"- **id**: `{sid}` · **enabled**: `{enabled}` · **schedule**: {schedule}")
        if desc:
            lines.append(f"- {desc}")
        bt = spec.get("backtest") or {}
        summary = bt.get("source_summary")
        if summary:
            lines.append(f"- backtest: `{summary}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def sync_strategy_catalog() -> Path:
    return publish_strategy_catalog(build_strategy_catalog_markdown())


def _parse_stamp(name: str) -> date | None:
    if len(name) != 8 or not name.isdigit():
        return None
    return date(int(name[:4]), int(name[4:6]), int(name[6:8]))


def discover_publish_dates(days: int = 30) -> list[date]:
    """Collect trade dates from publish/ tree."""
    end = datetime.now(_TPE).date()
    start = end - timedelta(days=days)
    found: set[date] = set()

    snap_root = PUBLISH_REGIME / "snapshots"
    if snap_root.is_dir():
        for child in snap_root.iterdir():
            if not child.is_dir():
                continue
            day = _parse_stamp(child.name)
            if day and start <= day <= end and (child / "daily_brief.md").is_file():
                found.add(day)

    if PUBLISH_RESEARCH_VCP.is_dir():
        for path in PUBLISH_RESEARCH_VCP.glob("*.md"):
            day = _parse_stamp(path.stem)
            if day and start <= day <= end:
                found.add(day)

    if PUBLISH_FACTS_ETF.is_dir():
        for path in PUBLISH_FACTS_ETF.glob("*.md"):
            if path.name == "daily_brief.md":
                continue
            day = _parse_stamp(path.stem)
            if day and start <= day <= end:
                found.add(day)

    return sorted(found)


def publish_path_etf_dated(trade_date: date | str) -> Path:
    return PUBLISH_FACTS_ETF / f"{_stamp(trade_date)}.md"


def publish_path_etf_latest() -> Path:
    return PUBLISH_FACTS_ETF / "daily_brief.md"


def publish_path_regime_snapshot(trade_date: date | str) -> Path:
    return PUBLISH_REGIME / "snapshots" / _stamp(trade_date) / "daily_brief.md"


def publish_path_regime_latest() -> Path:
    return PUBLISH_REGIME / "daily_brief.md"


def publish_path_vcp(trade_date: date | str) -> Path:
    return PUBLISH_RESEARCH_VCP / f"{_stamp(trade_date)}.md"


def extract_title(content_md: str, brief_type: str) -> str:
    for line in content_md.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    defaults = {
        "etf_daily": "ETF 持股日報",
        "regime_daily": "市場結構日報",
        "vcp_funnel_specs": "VCP 漏斗研究",
    }
    return defaults.get(brief_type, brief_type)


def load_publish_brief(brief_type: str, trade_date: date) -> dict[str, object] | None:
    """Load one brief from publish/ for Supabase upsert or local API."""
    if brief_type == "etf_daily":
        path = publish_path_etf_dated(trade_date)
        if not path.is_file():
            path = publish_path_etf_latest()
        html = None
    elif brief_type == "regime_daily":
        snap = publish_path_regime_snapshot(trade_date)
        path = snap if snap.is_file() else publish_path_regime_latest()
        embed = path.parent / "daily_brief.embed.html"
        if not embed.is_file() and path.parent != PUBLISH_REGIME:
            embed = PUBLISH_REGIME / "daily_brief.embed.html"
        html = embed.read_text(encoding="utf-8") if embed.is_file() else None
    elif brief_type == "vcp_funnel_specs":
        path = publish_path_vcp(trade_date)
        html = None
    else:
        return None

    if not path.is_file():
        return None

    content_md = path.read_text(encoding="utf-8")
    m = re.search(r"(\d{4}-\d{2}-\d{2})", content_md[:500])
    day = date.fromisoformat(m.group(1)) if m else trade_date
    if day != trade_date:
        return None

    slot = "1300" if brief_type in ("vcp_funnel_specs", "rrg_mono_intraday") else "1630"
    stat = path.stat()
    return {
        "trade_date": day.isoformat(),
        "schedule_slot": slot,
        "brief_type": brief_type,
        "title": extract_title(content_md, brief_type),
        "content_md": content_md,
        "content_html": html,
        "source_path": str(path.relative_to(PROJECT_ROOT)),
        "synced_at": datetime.fromtimestamp(stat.st_mtime, tz=_TPE).isoformat(),
    }
