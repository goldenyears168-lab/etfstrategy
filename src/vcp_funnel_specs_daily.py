"""VCP Pivot Gate / Coil Close — 盤中 daily brief（讀 vcp-funnel screen DB）。"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

from research.backtest.chunge_funnel_backtest import (
    VCP_COIL_CLOSE,
    VCP_PIVOT_GATE,
    VCP_COIL_CLOSE_VARIANT,
    VCP_PIVOT_GATE_VARIANT,
    ChungeCandidate,
    build_chunge_candidates_calendar,
)
from vcp_funnel_screen import FUNNEL_MODEL_IDS, MODEL_ID
from report_paths import REPORTS_DIR
from stock_db import connect, load_vcp_screen_v2_for_date
from website_publish import publish_vcp_funnel_specs

SPEC_REGISTRY: dict[str, dict[str, Any]] = {
    "pivot_gate": VCP_PIVOT_GATE,
    "coil_close": VCP_COIL_CLOSE,
}
SPEC_TITLES: dict[str, str] = {
    "pivot_gate": "VCP Pivot Gate",
    "coil_close": "VCP Coil Close",
}
SPEC_VARIANTS: dict[str, str] = {
    "pivot_gate": VCP_PIVOT_GATE_VARIANT,
    "coil_close": VCP_COIL_CLOSE_VARIANT,
}
ENTRY_HINTS: dict[str, str] = {
    "pivot_gate": "breakout_close · close≥pivot · 最長等 10 交易日",
    "coil_close": "訊號日 close · 可低於 pivot",
}


def resolve_spec_key(spec_key: str) -> str:
    key = spec_key.strip().lower().replace("-", "_")
    aliases = {
        "vcp_pivot_gate": "pivot_gate",
        "vcp_coil_close": "coil_close",
        VCP_PIVOT_GATE_VARIANT.replace("-", "_"): "pivot_gate",
        VCP_COIL_CLOSE_VARIANT.replace("-", "_"): "coil_close",
    }
    return aliases.get(key, key)


def latest_screen_date(conn: sqlite3.Connection, *, on_or_before: str) -> str | None:
    placeholders = ",".join("?" * len(FUNNEL_MODEL_IDS))
    row = conn.execute(
        f"""
        SELECT MAX(as_of_date) AS d
        FROM vcp_screen_scores_v2
        WHERE model_id IN ({placeholders}) AND as_of_date <= ?
        """,
        (*FUNNEL_MODEL_IDS, on_or_before),
    ).fetchone()
    if not row or not row["d"]:
        return None
    return str(row["d"])


def load_spec_candidates(
    conn: sqlite3.Connection,
    signal_day: str,
    spec_key: str,
    *,
    top_n: int = 15,
) -> tuple[str | None, list[ChungeCandidate]]:
    key = resolve_spec_key(spec_key)
    spec = SPEC_REGISTRY.get(key)
    if spec is None:
        raise KeyError(f"unknown funnel spec: {spec_key}")
    screen_day = latest_screen_date(conn, on_or_before=signal_day) or signal_day
    by_date = build_chunge_candidates_calendar(
        conn,
        [screen_day],
        model_id=MODEL_ID,
        min_composite=float(spec["min_composite"]),
        execution_states=tuple(spec["execution_states"]),
        entry_ready_only=bool(spec["entry_ready_only"]),
        require_pivot=bool(spec["require_pivot"]),
        min_dist_pivot_pct=spec.get("min_dist_pivot_pct"),
        max_dist_pivot_pct=spec.get("max_dist_pivot_pct"),
    )
    cands = by_date.get(screen_day, [])
    cands.sort(key=lambda c: (-c.composite_score, c.stock_id))
    return screen_day, cands[:top_n]


def build_spec_gate_summary(
    conn: sqlite3.Connection,
    signal_day: str,
    spec_key: str,
) -> tuple[tuple[tuple[str, str, int], ...], tuple[str, ...]]:
    key = resolve_spec_key(spec_key)
    spec = SPEC_REGISTRY[key]
    screen_day = latest_screen_date(conn, on_or_before=signal_day)
    if not screen_day:
        return (("screen", "vcp-funnel DB", 0),), ("尚無 vcp_screen_scores_v2（vcp-funnel）",)
    all_rows = load_vcp_screen_v2_for_date(
        conn, screen_day, model_id=MODEL_ID, min_score=0.0
    )
    scored = [r for r in all_rows if float(r["composite_score"] or 0) >= spec["min_composite"]]
    _, filtered = load_spec_candidates(conn, signal_day, key, top_n=999)
    layers = (
        ("screen", f"as_of {screen_day}", len(all_rows)),
        (f"score≥{spec['min_composite']:.0f}", "composite", len(scored)),
        ("spec", SPEC_TITLES[key], len(filtered)),
    )
    notes = (f"variant `{SPEC_VARIANTS[key]}` · {ENTRY_HINTS[key]}",)
    return layers, notes


def build_spec_brief_markdown(
    conn: sqlite3.Connection,
    *,
    spec_key: str,
    as_of_date: str | None = None,
    top_n: int = 15,
) -> str:
    key = resolve_spec_key(spec_key)
    ref = as_of_date or date.today().isoformat()
    title = SPEC_TITLES[key]
    variant = SPEC_VARIANTS[key]
    screen_day, cands = load_spec_candidates(conn, ref, key, top_n=top_n)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# {title} · daily brief · {ref}",
        "",
        f"> 產出 {now} · model `{MODEL_ID}` · variant `{variant}` · **非交易主檔**",
        f"> screen as_of **{screen_day or '—'}**（13:00 讀最新 DB；當日 16:30 screen 尚未跑）",
        f"> 進場規則：**{ENTRY_HINTS[key]}**",
        "",
        f"## 候選 Top {top_n}（near pivot −8%～+5% · Pre/Breakout/Early · composite≥45）",
        "",
    ]
    if not cands:
        lines.append("_今日無符合 spec 的候選（需先跑 vcp_funnel_screen --run 寫入 DB）_")
    else:
        lines.extend(
            [
                "| 代號 | 名稱 | composite | state | pivot | dist% | stop |",
                "|------|------|-----------|-------|-------|-------|------|",
            ]
        )
        for c in cands:
            pivot_s = f"{c.pivot_price:.2f}" if c.pivot_price else "—"
            dist_s = f"{c.distance_from_pivot_pct:.1f}" if c.distance_from_pivot_pct is not None else "—"
            stop_s = f"{c.stop_loss:.2f}" if c.stop_loss else "—"
            lines.append(
                f"| {c.stock_id} | {c.stock_name} | {c.composite_score:.1f} | "
                f"{c.execution_state} | {pivot_s} | {dist_s} | {stop_s} |"
            )
    lines.extend(["", "---", f"模組：`vcp_funnel_specs_daily.py` · backtest variant `{variant}`", ""])
    return "\n".join(lines)


def write_spec_briefs(
    conn: sqlite3.Connection,
    *,
    as_of_date: str | None = None,
    reports_dir: Path = REPORTS_DIR,
    spec_keys: tuple[str, ...] = ("pivot_gate", "coil_close"),
) -> list[Path]:
    ref = as_of_date or date.today().isoformat()
    stamp = ref.replace("-", "")
    reports_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    sections: list[str] = []
    slug_map = {"pivot_gate": "vcp_pivot_gate", "coil_close": "vcp_coil_close"}
    for spec_key in spec_keys:
        md = build_spec_brief_markdown(conn, spec_key=spec_key, as_of_date=ref)
        slug = slug_map.get(spec_key, spec_key)
        dated = reports_dir / f"{stamp}_{slug}_daily_brief.md"
        latest = reports_dir / f"{slug}_daily_brief.md"
        dated.write_text(md, encoding="utf-8")
        latest.write_text(md, encoding="utf-8")
        written.extend([dated, latest])
        sections.append(md)
    combined = "\n\n---\n\n".join(sections)
    combo_dated = reports_dir / f"{stamp}_vcp_funnel_specs_daily_brief.md"
    combo_latest = reports_dir / "vcp_funnel_specs_daily_brief.md"
    combo_dated.write_text(combined, encoding="utf-8")
    combo_latest.write_text(combined, encoding="utf-8")
    written.extend([combo_dated, combo_latest])
    publish_vcp_funnel_specs(combined, ref)
    return written


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="VCP Pivot Gate / Coil Close daily brief")
    parser.add_argument("--as-of", default=None, help="參考日 YYYY-MM-DD（預設今日）")
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db) if args.db else connect()
    try:
        paths = write_spec_briefs(conn, as_of_date=args.as_of)
    finally:
        conn.close()
    for p in paths:
        print(f"Wrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
