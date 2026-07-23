"""C18acc · true provisional RRG (SNAP) open-window: 12:00 vs 13:20.

Intent: enter while the afternoon list is still forming — before near-close crowd.

Fair pair (PIT-clean · live-executable):
- SNAP_1320: provisional RRG @13:20 · fill ≥13:20（現行近收盤）
- SNAP_1200: provisional RRG @12:00 · fill ≥12:00（提早入）

Optional mid points SNAP_1240 / SNAP_1300 for gradient.

Shared avoid_mixed gate (09:30 live cache) — Δ = selection+fill at that clock,
not a re-anchored afternoon mixed snapshot.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
from research.backtest.archive.c18acc_avoid_mixed_slot_resim import load_gate_cache
from research.backtest.archive.c18acc_pool1_showcase import _ensure_avoid_mixed_gate
from research.backtest.c18acc_open_timing_study import (
    ADOPT_IS_MIN_DELTA_PP,
    ADOPT_OOS_MIN_DELTA_PP,
    ADOPT_OOS_MIN_N,
    DEFAULT_GATE_CACHE,
    _run_sim,
    _variant_row,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_score_swap_c import (
    _poll5m_needs_spread_rs_panel,
    build_daily_spread_rs_panel,
    champion_score_swap_c_config,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from rrg_mono_daily_brief import LENGTH
from rrg_rotation import compute_rrg_panel

# baseline first · challengers after
SNAP_WINDOWS = (
    ("SNAP_1320", "13:20", "暫定 RRG@13:20 · ≥13:20 成交（現行近收）"),
    ("SNAP_1300", "13:00", "暫定 RRG@13:00 · ≥13:00 成交"),
    ("SNAP_1240", "12:40", "暫定 RRG@12:40 · ≥12:40 成交"),
    ("SNAP_1200", "12:00", "暫定 RRG@12:00 · ≥12:00 成交（提早入）"),
)


@contextmanager
def _snapshot_minute(minute: str) -> Iterator[None]:
    import research.backtest.c18acc_snapshot_1300 as snap

    old_m = snap.SNAPSHOT_1300_MINUTE
    old_sql = snap.SNAPSHOT_1300_MINUTE_SQL
    snap.SNAPSHOT_1300_MINUTE = minute
    snap.SNAPSHOT_1300_MINUTE_SQL = f"{minute}:00" if len(minute) == 5 else minute
    snap.clear_snapshot_panel_cache()
    try:
        yield
    finally:
        snap.SNAPSHOT_1300_MINUTE = old_m
        snap.SNAPSHOT_1300_MINUTE_SQL = old_sql
        snap.clear_snapshot_panel_cache()


def _adopt(*, n_oos: int, d_oos: float | None, d_is: float | None) -> str:
    ok = (
        n_oos >= ADOPT_OOS_MIN_N
        and d_oos is not None
        and float(d_oos) >= ADOPT_OOS_MIN_DELTA_PP
        and d_is is not None
        and float(d_is) >= ADOPT_IS_MIN_DELTA_PP
    )
    return "GO" if ok else "NO_GO"


def run_snap_window_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str = "2025-06-30",
    confirm_bars: int = 1,
    n_slots: int = 3,
    gate_cache_path: str | Path | None = DEFAULT_GATE_CACHE,
    minutes: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]

    gate_path = Path(gate_cache_path) if gate_cache_path else None
    if gate_path and gate_path.is_file() and date_end is None:
        _, _, c0, c1 = load_gate_cache(str(gate_path))
        if c0 == trade_dates[0] and c1 < trade_dates[-1]:
            end = c1
            trade_dates = [d for d in full_dates if date_start <= d <= end]
            print(f"truncated trade window to gate cache end {end}", flush=True)

    want = set(minutes) if minutes else {m for _, m, _ in SNAP_WINDOWS}
    windows = tuple(w for w in SNAP_WINDOWS if w[1] in want)
    if not any(w[0] == "SNAP_1320" for w in windows):
        windows = (SNAP_WINDOWS[0],) + windows

    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=trade_dates[0], date_end=trade_dates[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    champ = replace(
        champion_score_swap_c_config(),
        candidate_pool="fresh",
        n_slots=max(1, int(n_slots)),
    )
    spread_rs_panel = (
        build_daily_spread_rs_panel(close, bench) if _poll5m_needs_spread_rs_panel(champ) else None
    )
    ctx: dict[str, Any] = {
        "close": close,
        "bench": bench,
        "rs_ratio": rs_ratio,
        "rs_mom": rs_mom,
        "full_dates": full_dates,
        "fresh_by_date": fresh_by_date,
        "mono_by_date": mono_by_date,
        "zone_by_date": zone_by_date,
        "kbar_cache": {},
        "spread_rs_panel": spread_rs_panel,
    }

    oos_start = next((d for d in trade_dates if d > is_end), None)
    if not oos_start:
        raise ValueError(f"no OOS after {is_end}")

    gate, gate_meta = _ensure_avoid_mixed_gate(
        conn,
        trade_dates,
        gate_cache_path=gate_path,
        live_aligned=True,
        n_slots=n_slots,
    )
    if gate is None:
        raise ValueError("avoid_mixed gate required")

    variants: list[dict[str, Any]] = []
    base_full = base_is = base_oos = None
    for vid, minute, label in windows:
        print(f"  sim {vid} (provisional RRG @{minute}) …", flush=True)
        cfg = replace(
            champ,
            variant_id=vid,
            label=label,
            timing_mode="snapshot_1300",
            no_trade_before=minute,
            snapshot_strict_kbar=False,
        )
        with _snapshot_minute(minute):
            periods, summary, _ = _run_sim(
                conn,
                ctx={**ctx, "kbar_cache": {}},
                trade_dates=trade_dates,
                cfg=cfg,
                confirm_bars=confirm_bars,
                gate=gate,
                label=vid,
            )
        row = _variant_row(
            variant_id=vid,
            label=label,
            periods=periods,
            summary=summary,
            date_start=date_start,
            is_end=is_end,
            oos_start=oos_start,
            end=end,
            base_full=base_full,
            base_is=base_is,
            base_oos=base_oos,
            extra={
                "snapshot_minute": minute,
                "no_trade_before": minute,
                "pit_clean": True,
                "live_executable": True,
                "snap_meta": {
                    "snapshot_days_skipped": (summary or {}).get("snapshot_days_skipped"),
                    "n_legs": len(periods),
                },
            },
        )
        if vid == "SNAP_1320":
            base_full = row["slices"]["full"]
            base_is = row["slices"]["is"]
            base_oos = row["slices"]["oos"]
            row["delta_full_excess_pp"] = None
            row["delta_is_excess_pp"] = None
            row["delta_oos_excess_pp"] = None
        variants.append(row)

    adopt_by: dict[str, str] = {}
    bits: list[str] = []
    any_go = False
    best_vid = "SNAP_1320"
    best_d = float("-inf")
    for row in variants:
        vid = row["variant_id"]
        if vid == "SNAP_1320":
            continue
        n_oos = int((row["slices"]["oos"] or {}).get("n_legs") or 0)
        adopt = _adopt(
            n_oos=n_oos,
            d_oos=row.get("delta_oos_excess_pp"),
            d_is=row.get("delta_is_excess_pp"),
        )
        adopt_by[vid] = adopt
        d = row.get("delta_oos_excess_pp")
        bits.append(f"{vid} OOS Δ={d}pp → {adopt}")
        if adopt == "GO":
            any_go = True
        if d is not None and float(d) > best_d:
            best_d = float(d)
            best_vid = vid

    return {
        "study_id": "c18acc_snap_window",
        "schema": "c18acc_snap_window-v1",
        "date_start": trade_dates[0],
        "date_end": trade_dates[-1],
        "is_end": is_end,
        "oos_start": oos_start,
        "confirm_bars": confirm_bars,
        "n_slots": n_slots,
        "gate_meta": gate_meta,
        "intent": "early provisional entry before near-close crowd",
        "variants": variants,
        "verdict": {
            "baseline": "SNAP_1320",
            "adopt_by_variant": adopt_by,
            "best_challenger": best_vid,
            "recommendation": "KEEP_SNAP_1320" if not any_go else f"CONSIDER_{best_vid}",
            "summary": " · ".join(bits) + (" · 皆未過門檻" if not any_go else ""),
            "thresholds": {
                "oos_n_min": ADOPT_OOS_MIN_N,
                "oos_delta_pp_min": ADOPT_OOS_MIN_DELTA_PP,
                "is_delta_pp_min": ADOPT_IS_MIN_DELTA_PP,
            },
            "caveat": (
                "SNAP 重建當日該分鐘暫定 RRG（PIT）。"
                "共用 09:30 avoid_mixed gate；真 live 會在開窗錨點重算 mixed。"
            ),
        },
    }


def render_snap_window_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    lines = [
        "# C18acc · 真·暫定 RRG（SNAP）開窗：提早入 vs 近收盤",
        "",
        f"窗口：**{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"IS ≤ **{payload.get('is_end')}** · OOS ≥ **{payload.get('oos_start')}** · "
        f"confirm_bars={payload.get('confirm_bars')}",
        "",
        f"意圖：{payload.get('intent')}",
        "",
        "## 對照語意（PIT · live-executable）",
        "",
        "| id | 訊號 | 成交 |",
        "|----|------|------|",
    ]
    for row in payload.get("variants") or []:
        m = row.get("snapshot_minute") or "—"
        lines.append(f"| {row['variant_id']} | 暫定 RRG@{m} | ≥{m} |")
    lines.extend(
        [
            "",
            str(v.get("caveat") or ""),
            "",
            f"## 結論：{v.get('recommendation', '—')}",
            "",
            f"- {v.get('summary', '')}",
            "",
            "## 結果（Δ vs SNAP_1320）",
            "",
            "| variant | window | n | mean_excess% | Δ vs SNAP_1320 |",
            "|---------|--------|--:|-------------:|---------------:|",
        ]
    )
    for row in payload.get("variants") or []:
        for slice_name in ("full", "is", "oos"):
            s = (row.get("slices") or {}).get(slice_name) or {}
            delta = {
                "full": row.get("delta_full_excess_pp"),
                "is": row.get("delta_is_excess_pp"),
                "oos": row.get("delta_oos_excess_pp"),
            }[slice_name]
            d_s = "—" if delta is None else f"{float(delta):+.4f}pp"
            lines.append(
                f"| {row['variant_id']} | {slice_name.upper()} | {s.get('n_legs', 0)} | "
                f"{s.get('mean_excess_pct')} | {d_s} |"
            )
    lines.extend(
        [
            "",
            f"門檻：OOS n≥{ADOPT_OOS_MIN_N} · Δ≥+{ADOPT_OOS_MIN_DELTA_PP}pp · "
            f"IS Δ≥{ADOPT_IS_MIN_DELTA_PP}pp（相對 SNAP_1320）",
            "",
            "模組：`src/research/backtest/c18acc_snap_window_study.py`",
            "",
        ]
    )
    return "\n".join(lines)
