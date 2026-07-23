"""C18acc · no_trade_before window: 12:40→13:30 vs 13:20→13:30 (pre-E1300 baseline).

Fair pair (same selection set, only open-window length differs):
- E_1320: same-day EOD fresh pool · ≥13:20（現行 ≈10 分窗到 13:30）
- E_1240: same-day EOD fresh pool · ≥12:40（約 50 分窗到 13:30）

Shared avoid_mixed gate (live @09:30 cache) so Δ isolates earlier entry/swap polls,
not a re-anchored afternoon spread snapshot. Live would also re-anchor mixed at NTB.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

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

WINDOWS = (
    ("E_1320", "13:20", "當日 EOD 池 · ≥13:20→13:30（現行）"),
    ("E_1300", "13:00", "當日 EOD 池 · ≥13:00→13:30"),
    ("E_1240", "12:40", "當日 EOD 池 · ≥12:40→13:30"),
    ("E_1200", "12:00", "當日 EOD 池 · ≥12:00→13:30"),
)


def _adopt(*, n_oos: int, d_oos: float | None, d_is: float | None) -> str:
    ok = (
        n_oos >= ADOPT_OOS_MIN_N
        and d_oos is not None
        and float(d_oos) >= ADOPT_OOS_MIN_DELTA_PP
        and d_is is not None
        and float(d_is) >= ADOPT_IS_MIN_DELTA_PP
    )
    return "GO" if ok else "NO_GO"


def run_ntb_window_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str = "2025-06-30",
    confirm_bars: int = 1,
    n_slots: int = 3,
    gate_cache_path: str | Path | None = DEFAULT_GATE_CACHE,
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
    for vid, ntb, label in WINDOWS:
        cfg = replace(
            champ,
            variant_id=vid,
            label=label,
            timing_mode="poll_5m",
            no_trade_before=ntb,
        )
        periods, summary, _ = _run_sim(
            conn,
            ctx=ctx,
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
                "no_trade_before": ntb,
                "pit_note": "same-day EOD pool proxy · shared 09:30 avoid_mixed gate",
            },
        )
        if vid == "E_1320":
            base_full = row["slices"]["full"]
            base_is = row["slices"]["is"]
            base_oos = row["slices"]["oos"]
            # re-emit base row without deltas
            row["delta_full_excess_pp"] = None
            row["delta_is_excess_pp"] = None
            row["delta_oos_excess_pp"] = None
        variants.append(row)

    by_id = {v["variant_id"]: v for v in variants}
    adopt_by: dict[str, str] = {}
    bits: list[str] = []
    any_go = False
    best_vid = "E_1320"
    best_d = 0.0
    for vid, ntb, _label in WINDOWS:
        if vid == "E_1320":
            continue
        row = by_id[vid]
        n_oos = int((row["slices"]["oos"] or {}).get("n_legs") or 0)
        adopt = _adopt(
            n_oos=n_oos,
            d_oos=row.get("delta_oos_excess_pp"),
            d_is=row.get("delta_is_excess_pp"),
        )
        adopt_by[vid] = adopt
        d_oos = row.get("delta_oos_excess_pp")
        bits.append(f"{vid}(≥{ntb}) OOS Δ={d_oos}pp → {adopt}")
        if adopt == "GO":
            any_go = True
        if d_oos is not None and float(d_oos) > best_d:
            best_d = float(d_oos)
            best_vid = vid
    verdict = {
        "baseline": "E_1320",
        "adopt_by_variant": adopt_by,
        "best_challenger": best_vid,
        "recommendation": "KEEP_1320" if not any_go else f"CONSIDER_{best_vid}",
        "summary": " · ".join(bits) + (" · 皆未過門檻" if not any_go else ""),
        "thresholds": {
            "oos_n_min": ADOPT_OOS_MIN_N,
            "oos_delta_pp_min": ADOPT_OOS_MIN_DELTA_PP,
            "is_delta_pp_min": ADOPT_IS_MIN_DELTA_PP,
        },
    }
    return {
        "study_id": "c18acc_ntb_1240_vs_1320",
        "date_start": date_start,
        "date_end": end,
        "is_end": is_end,
        "oos_start": oos_start,
        "confirm_bars": confirm_bars,
        "n_slots": n_slots,
        "gate_meta": gate_meta,
        "variants": variants,
        "verdict": verdict,
        "why_1320": (
            "現行 13:20 來自 LIVE_MATCHED_1320：訊號日近收盤買優於隔日 09:30；"
            "同日名單在 ≥13:20 才接近 EOD。提早到 12:40 等於用更暫定的盤中狀態提早下單。"
        ),
    }


def render_ntb_window_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    lines = [
        "# C18acc · 開窗長度：12:40 vs 13:20",
        "",
        f"窗口：**{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"IS ≤ **{payload.get('is_end')}** · OOS ≥ **{payload.get('oos_start')}** · "
        f"confirm_bars={payload.get('confirm_bars')}",
        "",
        "## 為什麼現在是 13:20",
        "",
        str(payload.get("why_1320") or ""),
        "",
        "## 對照語意",
        "",
        "| id | 池 | 開窗 |",
        "|----|----|------|",
        "| E_1320 | 當日 EOD fresh | ≥13:20→13:30（現行） |",
        "| E_1300 | 同上 | ≥13:00→13:30 |",
        "| E_1240 | 同上 | ≥12:40→13:30 |",
        "| E_1200 | 同上 | ≥12:00→13:30 |",
        "",
        "共用 avoid_mixed gate（09:30 cache）→ Δ 主要反映「更早可進場／換倉」的 5m poll。",
        "",
        f"## 結論：{v.get('recommendation', '—')}",
        "",
        f"- {v.get('summary', '')}",
        "",
        "## 結果（Δ vs E_1320）",
        "",
        "| variant | window | n | mean_excess% | Δ vs E_1320 | entry_minute_hist |",
        "|---------|--------|--:|-------------:|------------:|-------------------|",
    ]
    for row in payload.get("variants") or []:
        for slice_name in ("full", "is", "oos"):
            s = (row.get("slices") or {}).get(slice_name) or {}
            delta = {
                "full": row.get("delta_full_excess_pp"),
                "is": row.get("delta_is_excess_pp"),
                "oos": row.get("delta_oos_excess_pp"),
            }[slice_name]
            hist = row.get("entry_minute_hist") or {}
            hist_s = ", ".join(f"{k}:{v}" for k, v in sorted(hist.items())[:8]) if hist else "—"
            if slice_name != "full":
                hist_s = "—"
            d_s = "—" if delta is None else f"{delta:+.4f}pp"
            lines.append(
                f"| {row['variant_id']} | {slice_name.upper()} | {s.get('n_legs', 0)} | "
                f"{s.get('mean_excess_pct')} | {d_s} | {hist_s} |"
            )
    lines.extend(
        [
            "",
            f"門檻：OOS n≥{ADOPT_OOS_MIN_N} · Δ≥+{ADOPT_OOS_MIN_DELTA_PP}pp · "
            f"IS Δ≥{ADOPT_IS_MIN_DELTA_PP}pp（相對 E_1320）",
            "",
            "模組：`src/research/backtest/c18acc_ntb_window_study.py`",
            "",
        ]
    )
    return "\n".join(lines)
