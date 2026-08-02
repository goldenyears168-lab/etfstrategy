"""RRG Improving lifecycle watch · daily close scan · buy_observation integration."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from project_config import DEFAULT_ETF_CODES
from price_panels import load_price_panels
from research.backtest.rrg_improving_lifecycle_backtest import ImprovingDayRow, scan_improving_signal_day
from research.backtest.rrg_improving_s3rv_executable import (
    attach_sid_stats_to_rows,
    build_sid_history_cache,
    conviction_from_improving_row,
    is_s3_executable,
)
from rrg_mono_daily_brief import ScanRow
from rrg_mono_swap_accel_screen import _signal_date_for_session
from stock_db import PROJECT_ROOT

ImprovingTrack = Literal[
    "watch_setup",
    "s3_rv9899",
    "s3_burst",
    "s3_exec",
    "s2_adjacent",
    "rule_b",
    "arch_a",
    "arch_b",
    "arch_c",
]

REPORT_DIR = PROJECT_ROOT / "reports" / "daily" / "rrg-improving"

ARCHETYPE_LABELS: dict[str, str] = {
    "A_coiled_spring": "A 蓄力簧",
    "A_coiled_variant": "A' 蓄力變體",
    "B_leading_edge": "B 貼 Leading",
    "C_pre_chase_spark": "C 預追價火花",
    "D_idiosyncratic": "D 其餘",
}

RULE_B_HIST_BURST_PCT = 4.76


@dataclass(frozen=True)
class ImprovingWatchHit:
    track: ImprovingTrack
    row: ImprovingDayRow
    score: float
    archetype: str | None = None


def is_rule_b(row: ImprovingDayRow) -> bool:
    """S2 → D+1 burst 歷史 4.76% · p≈8.6e-05（§8.6 case study）。"""
    return bool(
        row.s2_setup_core
        and -0.5 <= row.sig_ret <= 0.5
        and row.disp_contract
        and row.rv < 99.0
        and row.improve_streak >= 6
    )


def classify_s2_archetype(row: ImprovingDayRow) -> str | None:
    """S2 Setup core 三原型 + 變體（case study SSOT）。"""
    if not row.s2_setup_core:
        return None
    if 0.5 <= row.sig_ret < 1.0:
        return "C_pre_chase_spark"
    if row.rv >= 99.5:
        return "B_leading_edge"
    if -0.5 <= row.sig_ret <= 0.5 and row.disp_contract and row.rv < 99.0:
        return "A_coiled_spring"
    if row.disp_contract:
        return "A_coiled_variant"
    return "D_idiosyncratic"


def _watch_setup_score(row: ImprovingDayRow) -> float:
    flat_bonus = max(0.0, 0.5 - abs(row.sig_ret)) * 20.0
    rv_bonus = max(0.0, row.rv - 97.0) if row.rv >= 98.0 else 0.0
    return row.improve_streak * 10.0 + flat_bonus + rv_bonus


def _rule_b_score(row: ImprovingDayRow) -> float:
    flat_bonus = max(0.0, 0.5 - abs(row.sig_ret)) * 30.0
    disp_bonus = max(0.0, -row.disp_d) * 10.0 if not np.isnan(row.disp_d) else 0.0
    rv_bonus = max(0.0, 98.5 - row.rv) * 5.0 if row.rv < 99.0 else 0.0
    return row.improve_streak * 15.0 + flat_bonus + disp_bonus + rv_bonus + 100.0


def _arch_score(row: ImprovingDayRow, archetype: str) -> float:
    base = _watch_setup_score(row)
    if archetype == "B_leading_edge":
        return base + max(0.0, row.rv - 99.0) * 20.0
    if archetype == "C_pre_chase_spark":
        return base + row.sig_ret * 10.0
    if archetype in ("A_coiled_spring", "A_coiled_variant"):
        return base + (10.0 if row.disp_contract else 0.0)
    return base


def _s3_conviction_score(row: ImprovingDayRow) -> float:
    """0–100 · PIT signal-day factors · see compute_s3_rv_conviction_score."""
    return conviction_from_improving_row(row).score


def filter_improving_track(
    rows: list[ImprovingDayRow],
    track: ImprovingTrack,
) -> list[ImprovingWatchHit]:
    hits: list[ImprovingWatchHit] = []
    for row in rows:
        arch = classify_s2_archetype(row)
        if track == "watch_setup" and row.watch_setup:
            hits.append(ImprovingWatchHit(track, row, _watch_setup_score(row)))
        elif track == "s3_rv9899" and row.s3_rv_98_99:
            hits.append(ImprovingWatchHit(track, row, _s3_conviction_score(row)))
        elif track == "s3_exec" and is_s3_executable(
            s3_rv_98_99=row.s3_rv_98_99,
            bench_today_ret=row.bench_today_ret,
            sid_s3rv_hist_n=row.sid_s3rv_hist_n,
            sid_s3rv_hist_ok=row.sid_s3rv_hist_ok,
            require_sid_hist=True,
        ):
            hits.append(ImprovingWatchHit(track, row, _s3_conviction_score(row)))
        elif track == "s3_burst" and row.s3_burst:
            hits.append(ImprovingWatchHit(track, row, _s3_conviction_score(row)))
        elif track == "s2_adjacent" and row.s2_setup_core and row.session_gap_days <= 4:
            hits.append(ImprovingWatchHit(track, row, _watch_setup_score(row), arch))
        elif track == "rule_b" and is_rule_b(row):
            hits.append(ImprovingWatchHit(track, row, _rule_b_score(row), arch or "A_coiled_spring"))
        elif track == "arch_a" and arch in ("A_coiled_spring", "A_coiled_variant"):
            hits.append(ImprovingWatchHit(track, row, _arch_score(row, arch), arch))
        elif track == "arch_b" and arch == "B_leading_edge":
            hits.append(ImprovingWatchHit(track, row, _arch_score(row, arch), arch))
        elif track == "arch_c" and arch == "C_pre_chase_spark":
            hits.append(ImprovingWatchHit(track, row, _arch_score(row, arch), arch))
    hits.sort(key=lambda h: -h.score)
    return hits


def collect_s2_archetype_hits(rows: list[ImprovingDayRow]) -> dict[str, list[ImprovingWatchHit]]:
    """All S2 archetype buckets for daily brief (Rule B listed separately)."""
    rule_b = filter_improving_track(rows, "rule_b")
    rule_b_ids = {h.row.stock_id for h in rule_b}
    out: dict[str, list[ImprovingWatchHit]] = {"rule_b": rule_b}
    for track, arch_keys in [
        ("arch_a", ("A_coiled_spring", "A_coiled_variant")),
        ("arch_b", ("B_leading_edge",)),
        ("arch_c", ("C_pre_chase_spark",)),
    ]:
        bucket: list[ImprovingWatchHit] = []
        for row in rows:
            if not row.s2_setup_core:
                continue
            arch = classify_s2_archetype(row)
            if arch not in arch_keys:
                continue
            if track == "arch_a" and row.stock_id in rule_b_ids:
                continue
            bucket.append(
                ImprovingWatchHit(
                    track=track,  # type: ignore[arg-type]
                    row=row,
                    score=_arch_score(row, arch or ""),
                    archetype=arch,
                )
            )
        bucket.sort(key=lambda h: -h.score)
        out[track] = bucket
    return out


def improving_hits_to_scan_rows(hits: list[ImprovingWatchHit]) -> list[ScanRow]:
    out: list[ScanRow] = []
    for hit in hits:
        row = hit.row
        out.append(
            ScanRow(
                stock_id=row.stock_id,
                stock_name=row.stock_name,
                fresh=False,
                mono=False,
                seg_last=float(hit.score),
                disp=row.disp,
                segs=[],
                quadrants=list(row.quadrants),
                rs_ratio=row.rv,
                rs_momentum=row.rs_momentum,
                daily_pct=row.sig_ret,
                composite_score=float(hit.score),
            )
        )
    return out


def build_improving_observation_pool(
    conn: sqlite3.Connection,
    source: str,
    *,
    session: str,
    close: pd.DataFrame,
    bench: pd.Series,
) -> tuple[list[ScanRow], str]:
    full_dates = close.index.astype(str).tolist()
    signal_as_of = _signal_date_for_session(full_dates, session)
    if not signal_as_of:
        return [], ""

    track_map: dict[str, ImprovingTrack] = {
        "rrg_improving_watch_setup": "watch_setup",
        "rrg_improving_s3_rv": "s3_rv9899",
        "rrg_improving_s3_exec": "s3_exec",
        "rrg_improving_s3_burst": "s3_burst",
        "rrg_improving_s2_adjacent": "s2_adjacent",
        "rrg_improving_rule_b": "rule_b",
        "rrg_improving_arch_a": "arch_a",
        "rrg_improving_arch_b": "arch_b",
        "rrg_improving_arch_c": "arch_c",
    }
    track = track_map.get(source)
    if track is None:
        return [], signal_as_of

    rows, pit = scan_improving_signal_day(
        conn, signal_as_of, close=close, bench=bench, etf_codes=DEFAULT_ETF_CODES
    )
    try:
        sid_cache = build_sid_history_cache(conn)
        rows = attach_sid_stats_to_rows(rows, sid_cache)
    except Exception:
        pass
    hits = filter_improving_track(rows, track)
    return improving_hits_to_scan_rows(hits), pit


def format_improving_watch_note(hit: ImprovingWatchHit) -> str:
    row = hit.row
    parts: list[str] = []
    if hit.archetype:
        parts.append(ARCHETYPE_LABELS.get(hit.archetype, hit.archetype))
    parts.extend(
        [
            f"RV={row.rv:.1f}",
            f"streak={row.improve_streak}",
            f"sig={row.sig_ret:+.2f}%",
        ]
    )
    if row.disp_contract:
        parts.append("disp↓")
    if row.s3_burst or row.s3_rv_98_99 or row.base_setup:
        conv = conviction_from_improving_row(row)
        if conv.score > 0:
            parts.append(f"conv={conv.score:.0f}·{conv.tier_label}·{conv.position_weight:.2f}x")
    if row.session_gap_days > 4:
        parts.append(f"gap={row.session_gap_days}d")
    return " · ".join(parts)


def _render_hit_table(hits: list[ImprovingWatchHit], *, show_arch: bool = False) -> list[str]:
    if not hits:
        return ["_（無）_"]
    header = "| # | 代號 | 名稱 | RV | streak | sig% | dispΔ | excess% |"
    if show_arch:
        header += " 原型 |"
    lines = [header, "|---|------|------|-----|--------|------|-------|---------|" + ("------|" if show_arch else "")]
    for i, hit in enumerate(hits[:12], 1):
        r = hit.row
        disp_d = f"{r.disp_d:+.2f}" if not np.isnan(r.disp_d) else "—"
        line = (
            f"| {i} | {r.stock_id} | {r.stock_name} | {r.rv:.1f} | {r.improve_streak} | "
            f"{r.sig_ret:+.2f} | {disp_d} | {r.excess:+.2f} |"
        )
        if show_arch:
            label = ARCHETYPE_LABELS.get(hit.archetype or "", "—")
            line = f"{line} {label} |"
        lines.append(line)
    return lines


def render_improving_daily_markdown(
    *,
    signal_date: str,
    watch_setup: list[ImprovingWatchHit],
    s2_archetypes: dict[str, list[ImprovingWatchHit]],
    s3_burst: list[ImprovingWatchHit],
    s3_rv: list[ImprovingWatchHit],
    s3_exec: list[ImprovingWatchHit],
    s2_adjacent: list[ImprovingWatchHit],
    bench_today_ret: float | None,
) -> str:
    rule_b = s2_archetypes.get("rule_b", [])
    arch_a = s2_archetypes.get("arch_a", [])
    arch_b = s2_archetypes.get("arch_b", [])
    arch_c = s2_archetypes.get("arch_c", [])

    lines = [
        "# RRG Improving watch · daily brief",
        "",
        f"- Signal day (PIT): **{signal_date}**",
        f"- Benchmark day return: **{bench_today_ret:+.2f}%**" if bench_today_ret is not None else "",
        "",
        "## Rule B · S2→隔日 burst 強化 watch",
        "",
        f"> 歷史 Setup core 隔日 ≥5%：**{RULE_B_HIST_BURST_PCT:.2f}%**（n=126 · p≈8.6e-05）· "
        "條件：S2 + sig∈[−0.5,0.5] + disp↓ + RV<99 + streak≥6",
        "",
    ]
    lines.extend(_render_hit_table(rule_b))

    lines.extend(
        [
            "",
            "## 三原型 · S2 Setup core 分軌（隔日 burst 路徑 · §8.6）",
            "",
            "### A 蓄力簧 / A' 變體（disp↓ · 非 Rule B 者另列）",
            "",
        ]
    )
    lines.extend(_render_hit_table(arch_a, show_arch=True))

    lines.extend(["", "### B 貼 Leading（RV≥99.5 · 隔日釋放）", ""])
    lines.extend(_render_hit_table(arch_b, show_arch=True))

    lines.extend(["", "### C 預追價火花（sig∈[0.5%, 1%) · S2 邊界小陽）", ""])
    lines.extend(_render_hit_table(arch_c, show_arch=True))

    lines.extend(
        [
            "",
            "## 蓄力 watch_setup（base + streak≥4 + sig∈[−1%, +0.5%]）",
            "",
        ]
    )
    lines.extend(_render_hit_table(watch_setup))

    lines.extend(["", "## 釋放 S3 Improving ≥5%（全體 · 隔日追蹤）", ""])
    if s3_burst:
        lines.append("| # | 代號 | 名稱 | RV | sig% | streak | RV98–99 |")
        lines.append("|---|------|------|-----|------|--------|---------|")
        for i, hit in enumerate(s3_burst[:15], 1):
            r = hit.row
            rv_band = "✓" if r.s3_rv_98_99 else "—"
            lines.append(
                f"| {i} | {r.stock_id} | {r.stock_name} | {r.rv:.1f} | {r.sig_ret:+.2f} | "
                f"{r.improve_streak} | {rv_band} |"
            )
    else:
        lines.append("_（無）_")

    lines.extend(["", "## 釋放 S3 + RV 98–99（統計顯著子集 · 隔日 burst 優先）", ""])
    if s3_rv:
        lines.append("| # | 代號 | 名稱 | RV | sig% | streak | 信號日台指 |")
        lines.append("|---|------|------|-----|------|--------|-----------|")
        for i, hit in enumerate(s3_rv[:10], 1):
            r = hit.row
            lines.append(
                f"| {i} | {r.stock_id} | {r.stock_name} | {r.rv:.1f} | {r.sig_ret:+.2f} | "
                f"{r.improve_streak} | {r.bench_today_ret:+.2f}% |"
            )
    else:
        lines.append("_（無）_")

    lines.extend(
        [
            "",
            "## 可執行 · S3 RV + 信號日台指≥0 + 個股 S3 後史>0（PIT · research）",
            "",
            "> D 收：鎖定名單（close→close 研究路徑）· D+1 09:00：台指 gap 作風控參考 · 見 `s3rv_executable_proof_*.md`",
            "",
        ]
    )
    if s3_exec:
        lines.append("| # | 代號 | 名稱 | RV | sig% | 信號日台指 | sid n | sid mean |")
        lines.append("|---|------|------|-----|------|-----------|-------|----------|")
        for i, hit in enumerate(s3_exec[:10], 1):
            r = hit.row
            sm = f"{r.sid_s3rv_hist_mean:+.2f}%" if not np.isnan(r.sid_s3rv_hist_mean) else "—"
            lines.append(
                f"| {i} | {r.stock_id} | {r.stock_name} | {r.rv:.1f} | {r.sig_ret:+.2f} | "
                f"{r.bench_today_ret:+.2f}% | {r.sid_s3rv_hist_n} | {sm} |"
            )
    else:
        lines.append("_（無）_")

    if s2_adjacent:
        lines.extend(["", "## S2 adjacent（excess 有效 · session gap ≤4）", ""])
        lines.extend(_render_hit_table(s2_adjacent))

    lines.extend(
        [
            "",
            "> Research only · 非採納策略 · 不觸發 C0 進場 · 見 `docs/rrg-improving-lifecycle-research.md`",
            "",
        ]
    )
    return "\n".join(line for line in lines if line is not None) + "\n"


def run_improving_watch_daily(
    conn: sqlite3.Connection,
    *,
    session: str | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    dates = sorted(close.index.astype(str).tolist())
    signal_date = session or dates[-1]
    rows, pit = scan_improving_signal_day(conn, signal_date, close=close)
    try:
        sid_cache = build_sid_history_cache(conn)
        rows = attach_sid_stats_to_rows(rows, sid_cache)
    except Exception:
        pass
    watch_setup = filter_improving_track(rows, "watch_setup")
    s2_archetypes = collect_s2_archetype_hits(rows)
    s3_burst = filter_improving_track(rows, "s3_burst")
    s3_rv = filter_improving_track(rows, "s3_rv9899")
    s3_exec = filter_improving_track(rows, "s3_exec")
    s2_adjacent = filter_improving_track(rows, "s2_adjacent")
    bench_today = rows[0].bench_today_ret if rows else None

    md = render_improving_daily_markdown(
        signal_date=pit,
        watch_setup=watch_setup,
        s2_archetypes=s2_archetypes,
        s3_burst=s3_burst,
        s3_rv=s3_rv,
        s3_exec=s3_exec,
        s2_adjacent=s2_adjacent,
        bench_today_ret=bench_today,
    )
    out = out_dir or REPORT_DIR
    out.mkdir(parents=True, exist_ok=True)
    stamp = pit.replace("-", "")
    dated = out / f"daily_brief_{stamp}.md"
    latest = out / "daily_brief.md"
    dated.write_text(md, encoding="utf-8")
    latest.write_text(md, encoding="utf-8")

    return {
        "signal_date": pit,
        "rule_b_n": len(s2_archetypes.get("rule_b", [])),
        "arch_a_n": len(s2_archetypes.get("arch_a", [])),
        "arch_b_n": len(s2_archetypes.get("arch_b", [])),
        "arch_c_n": len(s2_archetypes.get("arch_c", [])),
        "watch_setup_n": len(watch_setup),
        "s3_burst_n": len(s3_burst),
        "s3_rv_n": len(s3_rv),
        "s3_exec_n": len(s3_exec),
        "s2_adjacent_n": len(s2_adjacent),
        "report_dated": str(dated),
        "report_latest": str(latest),
        "markdown": md,
    }
