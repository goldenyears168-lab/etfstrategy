"""Report output paths: daily (scheduled) vs research (ad-hoc / backtest)."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Literal

from stock_db import PROJECT_ROOT

REPORTS_ROOT = PROJECT_ROOT / "reports"
REPORTS_DAILY = REPORTS_ROOT / "daily"
REPORTS_RESEARCH = REPORTS_ROOT / "research"

# Research HTML 子目錄（對齊 reports/README.md）
RESEARCH_BREADTH = REPORTS_RESEARCH / "breadth"
RESEARCH_RRG = REPORTS_RESEARCH / "rrg"
RESEARCH_COPYTRADE_00981A = REPORTS_RESEARCH / "00981a-copytrade"
RESEARCH_VCP = REPORTS_RESEARCH / "vcp"

RESEARCH_HTML_DIRS: dict[str, Path] = {
    "breadth": RESEARCH_BREADTH,
    "rrg": RESEARCH_RRG,
    "00981a-copytrade": RESEARCH_COPYTRADE_00981A,
    "vcp": RESEARCH_VCP,
}

# Daily pipeline + strategy publish (canonical scheduled outputs).
REPORTS_DIR = REPORTS_DAILY


def ensure_daily_dir() -> Path:
    REPORTS_DAILY.mkdir(parents=True, exist_ok=True)
    return REPORTS_DAILY


def ensure_research_dir() -> Path:
    REPORTS_RESEARCH.mkdir(parents=True, exist_ok=True)
    for d in RESEARCH_HTML_DIRS.values():
        d.mkdir(parents=True, exist_ok=True)
    return REPORTS_RESEARCH


def research_html_dir(category: str) -> Path:
    """Return (and create) the canonical HTML directory for a research category."""
    root = RESEARCH_HTML_DIRS.get(category)
    if root is None:
        raise ValueError(
            f"unknown research HTML category {category!r}; "
            f"expected one of {sorted(RESEARCH_HTML_DIRS)}"
        )
    root.mkdir(parents=True, exist_ok=True)
    return root


def research_html_path(category: str, filename: str) -> Path:
    """Canonical path for a research HTML artifact."""
    return research_html_dir(category) / filename


def latest_research_html(category: str, pattern: str) -> Path | None:
    """Newest file matching glob under a research HTML category."""
    root = research_html_dir(category)
    hits = sorted(root.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return hits[0] if hits else None


def daily_track_dir(track_id: str) -> Path:
    """Canonical daily output folder for a strategy track id."""
    return REPORTS_DAILY / track_id


def canonical_daily_brief_path(strategy_id: str) -> Path:
    """Published daily brief path from strategies.yaml aliases."""
    from strategy_registry import load_strategy_registry

    spec = load_strategy_registry().get(strategy_id)
    if spec is None:
        return daily_track_dir(strategy_id) / "daily_brief.md"
    alias = spec.aliases.get("daily_brief.md")
    if alias:
        return REPORTS_DAILY / alias
    return daily_track_dir(strategy_id) / "daily_brief.md"


def canonical_daily_track_dir(strategy_id: str) -> Path:
    return canonical_daily_brief_path(strategy_id).parent


# Regime daily · layered artifact layout (axis charts + dated snapshots).
REGIME_CHART_BREADTH = "axis/breadth/spark.svg"
REGIME_CHART_ZWEIG_EMA = "axis/breadth/zweig_ema_spark.svg"
REGIME_CHART_WEINSTEIN = "axis/trend/weinstein_weekly.svg"
REGIME_CHART_RRG = "axis/rrg/scatter.svg"
REGIME_CHART_STAGE2 = "axis/stage2/participation_spark.svg"


def regime_axis_dir(track_dir: Path, axis: Literal["breadth", "rrg"]) -> Path:
    return track_dir / "axis" / axis


def regime_snapshot_dir(track_dir: Path, as_of: str) -> Path:
    return track_dir / "snapshots" / as_of.replace("-", "")


def regime_snapshot_brief_path(track_dir: Path, as_of: str) -> Path:
    return regime_snapshot_dir(track_dir, as_of) / "daily_brief.md"


def classify_research_html_filename(filename: str) -> str | None:
    """Map a research HTML basename to its canonical category (or None if unknown)."""
    name = filename.lower()
    if any(k in name for k in ("00981a", "copytrade", "holdings_change", "l1h9")):
        return "00981a-copytrade"
    if "vcp" in name:
        return "vcp"
    if any(k in name for k in ("rrg", "universe")):
        return "rrg"
    if any(
        k in name
        for k in (
            "breadth",
            "momentum",
            "h30",
            "dual_momentum",
            "tanish",
            "luxalgo",
            "weather",
            "trajectory",
        )
    ):
        return "breadth"
    return None


def _is_redirect_stub(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return 'http-equiv="refresh"' in text and path.name in text


def _is_research_alias(path: Path, *, canonical: Path | None = None) -> bool:
    if path.is_symlink():
        if canonical is None:
            return True
        try:
            return path.resolve() == canonical.resolve()
        except OSError:
            return False
    return _is_redirect_stub(path)


def organize_research_html(*, dry_run: bool = False) -> list[tuple[Path, Path]]:
    """Move stray *.html under reports/ or reports/research/ root into category subdirs."""
    ensure_research_dir()
    moves: list[tuple[Path, Path]] = []
    search_roots = [REPORTS_ROOT, REPORTS_RESEARCH]
    seen: set[Path] = set()

    for root in search_roots:
        if not root.is_dir():
            continue
        for src in sorted(root.glob("*.html")):
            if src in seen:
                continue
            seen.add(src)
            if src.is_symlink() or _is_redirect_stub(src):
                continue
            category = classify_research_html_filename(src.name)
            if category is None:
                continue
            dest = research_html_path(category, src.name)
            if dest.resolve() == src.resolve():
                continue
            if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
                if not dry_run:
                    src.unlink()
                moves.append((src, dest))
                continue
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    backup = dest.with_suffix(dest.suffix + ".bak")
                    dest.replace(backup)
                src.replace(dest)
            moves.append((src, dest))
    return moves


def research_html_redirect_stub(canonical: Path) -> str:
    """Minimal HTML redirect from reports/research/{basename} → subdir file."""
    rel = canonical.relative_to(REPORTS_RESEARCH)
    target = html.escape(str(rel).replace("\\", "/"), quote=True)
    title = html.escape(canonical.name)
    return (
        "<!DOCTYPE html><html><head>"
        f'<meta charset="utf-8"/><meta http-equiv="refresh" content="0;url={target}"/>'
        f"<title>{title}</title></head>"
        f'<body><p>已移至 <a href="{target}">{title}</a></p></body></html>'
    )


def write_research_html_redirects(*, dry_run: bool = False) -> list[Path]:
    """Create symlinks at research/ root pointing to canonical HTML in subdirs."""
    ensure_research_dir()
    written: list[Path] = []
    for category_dir in RESEARCH_HTML_DIRS.values():
        if not category_dir.is_dir():
            continue
        for canonical in sorted(category_dir.glob("*.html")):
            alias = REPORTS_RESEARCH / canonical.name
            if alias.resolve() == canonical.resolve():
                continue
            rel = canonical.relative_to(REPORTS_RESEARCH)
            if alias.is_symlink() and _is_research_alias(alias, canonical=canonical):
                continue
            if not dry_run:
                if alias.exists() or alias.is_symlink():
                    alias.unlink()
                try:
                    alias.symlink_to(rel)
                except OSError:
                    alias.write_text(research_html_redirect_stub(canonical), encoding="utf-8")
            written.append(alias)
    return written
