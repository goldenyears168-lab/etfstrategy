"""VCP-TM markdown reports: Section A (entry_ready) / Section B (extended)."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def build_section_ab_markdown(
    *,
    as_of_date: str,
    model_id: str,
    benchmark: str,
    candidates: list[dict[str, Any]],
    universe_note: str = "ETF 成分股聯集",
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    section_a = [c for c in candidates if c.get("entry_ready")]
    section_b = [c for c in candidates if not c.get("entry_ready") and c.get("valid_vcp")]

    section_a.sort(key=lambda x: float(x.get("composite_score") or 0), reverse=True)
    section_b.sort(key=lambda x: float(x.get("composite_score") or 0), reverse=True)

    lines = [
        f"# VCP Screener · {as_of_date}",
        "",
        f"> 產出 {now} · model `{model_id}` · universe **{universe_note}** · "
        f"benchmark **{benchmark}** · tradermonty lineage",
        "",
        "## Section A: Pre-Breakout Watchlist (`entry_ready=True`)",
        "",
    ]
    if not section_a:
        lines.append("_No entry-ready candidates today._")
    else:
        lines.extend(_table_header())
        for c in section_a:
            lines.append(_table_row(c))

    lines.extend(
        [
            "",
            "## Section B: Extended / Quality VCP (`entry_ready=False`, `valid_vcp=True`)",
            "",
        ]
    )
    if not section_b:
        lines.append("_No extended quality VCP candidates._")
    else:
        lines.extend(_table_header())
        for c in section_b:
            lines.append(_table_row(c))

    return "\n".join(lines) + "\n"


def _table_header() -> list[str]:
    return [
        "| Symbol | Name | Composite | Rating | Execution State | Pattern | Pivot | Dist% | Risk% | RS |",
        "|--------|------|-----------|--------|-----------------|---------|-------|-------|-------|-----|",
    ]


def _table_row(c: dict[str, Any]) -> str:
    cap = " ★" if c.get("state_cap_applied") else ""
    rs = (c.get("relative_strength") or {}).get("weighted_rs", "—")
    return (
        f"| {c.get('stock_id', '—')} | {c.get('stock_name', '')} | "
        f"{float(c.get('composite_score') or 0):.0f} | {c.get('rating', '—')}{cap} | "
        f"{c.get('execution_state', '—')} | {c.get('pattern_type', '—')} | "
        f"{c.get('pivot_price') or c.get('pivot') or '—'} | "
        f"{c.get('distance_from_pivot_pct', '—')}% | "
        f"{c.get('risk_pct', '—')}% | {rs} |"
    )
