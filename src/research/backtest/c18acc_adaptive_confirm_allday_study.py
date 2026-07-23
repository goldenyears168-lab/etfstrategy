"""C18acc · adaptive confirm + all-day entry · simplified first pass.

Discussion framing (2026-07-14):
- Live locks new entries near close (no_trade_before=13:00 · 2026-07-14 採納).
- Alternative: open ≥09:30 (all-day) with either fixed confirm_bars or a schedule
  that requires more consecutive top-N polls early, fewer late.

Simplified stack (reuse champion sim · not a full live replica):
- candidate_pool=fresh (EOD calendar · same caveat as NTB window studies)
- avoid_spread_mixed gate @09:30 (shared)
- timing_mode=poll_5m · 3 slots · S2 exits unchanged
- Entry open synced: ScoreSwapCConfig.no_trade_before == CVariantConfig.no_swap_before

Variants:
- B0_1320_c1: ≥13:20 · confirm=1（現行語意基線）
- ALLDAY_c1 / c2 / c3: ≥09:30 · fixed confirm
- ADAPT_sch: ≥09:30 · confirm schedule 09:30→3 · 11:00→2 · 13:00→1
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
    _entry_c_config,
    _period_stats,
    _variant_row,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import CVariantConfig
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    _poll5m_needs_spread_rs_panel,
    build_daily_spread_rs_panel,
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from rrg_mono_daily_brief import LENGTH
from rrg_rotation import compute_rrg_panel

# early = stricter confirm · late = looser
ADAPT_SCHEDULE: tuple[tuple[str, int], ...] = (
    ("09:30", 3),
    ("11:00", 2),
    ("13:00", 1),
)

VARIANTS: tuple[tuple[str, str, str, int | None, tuple[tuple[str, int], ...] | None], ...] = (
    ("B0_1320_c1", "現行 · ≥13:20 · confirm=1", "13:20", 1, None),
    ("ALLDAY_c1", "全天 · ≥09:30 · confirm=1", "09:30", 1, None),
    ("ALLDAY_c2", "全天 · ≥09:30 · confirm=2", "09:30", 2, None),
    ("ALLDAY_c3", "全天 · ≥09:30 · confirm=3", "09:30", 3, None),
    (
        "ADAPT_sch",
        "全天 · ≥09:30 · 自適應 confirm（早3/中2/晚1）",
        "09:30",
        3,
        ADAPT_SCHEDULE,
    ),
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


def _make_entry_cfg(
    *,
    ntb: str,
    confirm_bars: int,
    schedule: tuple[tuple[str, int], ...] | None,
) -> CVariantConfig:
    base = _entry_c_config(int(confirm_bars))
    return replace(
        base,
        no_swap_before=str(ntb),
        confirm_schedule=schedule,
        label=(
            f"{base.label} · ntb={ntb}"
            + (f" · adapt={schedule}" if schedule else f" · confirm={confirm_bars}")
        ),
    )


def _run_sim(
    conn: sqlite3.Connection,
    *,
    ctx: dict[str, Any],
    trade_dates: list[str],
    cfg: ScoreSwapCConfig,
    entry_c: CVariantConfig,
    gate: dict[str, set[str]] | None,
    label: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    print(f"  sim {label} · entry_open={entry_c.no_swap_before} …", flush=True)
    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=ctx["full_dates"],
        close=ctx["close"],
        bench=ctx["bench"],
        fresh_by_date=ctx["fresh_by_date"],
        zone_by_date=ctx["zone_by_date"],
        config=cfg,
        mono_by_date=ctx["mono_by_date"],
        kbar_cache=ctx["kbar_cache"],
        rs_mom=ctx["rs_mom"],
        rs_ratio=ctx["rs_ratio"],
        spread_rs_panel=ctx["spread_rs_panel"],
        entry_c_config=entry_c,
        entry_gate_by_date=gate,
        swap_gate_by_date=gate,
    )
    stats = _period_stats(periods, summary)
    print(
        f"    n={stats['n_legs']} excess={stats.get('mean_excess_pct')}% "
        f"win={stats.get('win_rate_gross_pct')}",
        flush=True,
    )
    return periods, summary, stats


def run_adaptive_confirm_allday_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str = "2025-06-30",
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
    for vid, label, ntb, bars, schedule in VARIANTS:
        cfg = replace(
            champ,
            variant_id=vid,
            label=label,
            timing_mode="poll_5m",
            no_trade_before=ntb,
        )
        entry_c = _make_entry_cfg(
            ntb=ntb,
            confirm_bars=int(bars or 1),
            schedule=schedule,
        )
        periods, summary, _ = _run_sim(
            conn,
            ctx=ctx,
            trade_dates=trade_dates,
            cfg=cfg,
            entry_c=entry_c,
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
                "confirm_bars": bars,
                "confirm_schedule": [list(x) for x in schedule] if schedule else None,
                "entry_open": entry_c.no_swap_before,
            },
        )
        if vid == "B0_1320_c1":
            base_full = (row.get("slices") or {}).get("full")
            base_is = (row.get("slices") or {}).get("is")
            base_oos = (row.get("slices") or {}).get("oos")
            row["delta_full_excess_pp"] = 0.0
            row["delta_is_excess_pp"] = 0.0
            row["delta_oos_excess_pp"] = 0.0
        variants.append(row)

    adopt: dict[str, str] = {}
    notes: list[str] = []
    for v in variants:
        vid = str(v["variant_id"])
        if vid == "B0_1320_c1":
            adopt[vid] = "BASELINE"
            continue
        oos = (v.get("slices") or {}).get("oos") or {}
        d_oos = v.get("delta_oos_excess_pp")
        d_is = v.get("delta_is_excess_pp")
        n_oos = int(oos.get("n_legs") or 0)
        flag = _adopt(n_oos=n_oos, d_oos=d_oos, d_is=d_is)
        adopt[vid] = flag
        notes.append(
            f"{vid}: OOS Δexcess={d_oos}pp n={n_oos} · IS Δ={d_is}pp → {flag}"
        )

    best_id = None
    best_d = None
    for v in variants:
        vid = str(v["variant_id"])
        if vid == "B0_1320_c1":
            continue
        d = v.get("delta_oos_excess_pp")
        if d is None:
            continue
        if best_d is None or float(d) > float(best_d):
            best_d = float(d)
            best_id = vid

    any_go = any(v == "GO" for k, v in adopt.items() if k != "B0_1320_c1")
    verdict_summary = (
        f"GO_ADOPT_{best_id}" if any_go and best_id else "KEEP_1320_WINDOW"
    )
    if best_id and not any_go:
        notes.append(
            f"best OOS Δ={best_d}pp on {best_id} but below adopt thresholds → keep 13:20"
        )

    return {
        "schema": "c18acc-adaptive-confirm-allday-v1",
        "date_start": date_start,
        "date_end": end,
        "is_end": is_end,
        "oos_start": oos_start,
        "n_trade_dates": len(trade_dates),
        "n_slots": n_slots,
        "baseline": "B0_1320_c1",
        "adapt_schedule": [list(x) for x in ADAPT_SCHEDULE],
        "gate_meta": gate_meta,
        "caveats": [
            "fresh pool = EOD calendar（與 NTB 窗研究同一簡化；非盤中暫定 RRG）",
            "entry open 已與 no_trade_before 同步（修正舊 NTB 研究 entry 仍從 09:30 起算的偏誤）",
            "簡化：不重算盤中 fresh 名單穩定性 / 不優化相對低點",
        ],
        "variants": variants,
        "verdict": {
            "summary": verdict_summary,
            "adopt": adopt,
            "best_oos_variant": best_id,
            "best_oos_delta_pp": best_d,
            "thresholds": {
                "oos_min_n": ADOPT_OOS_MIN_N,
                "oos_min_delta_pp": ADOPT_OOS_MIN_DELTA_PP,
                "is_min_delta_pp": ADOPT_IS_MIN_DELTA_PP,
            },
            "notes": notes,
        },
    }


def render_adaptive_confirm_allday_md(payload: dict[str, Any]) -> str:
    vs = payload.get("variants") or []
    vdict = payload.get("verdict") or {}
    lines = [
        "# C18acc · 自適應 confirm + 全天可買（簡化第一輪）",
        "",
        f"窗口：**{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"IS ≤ **{payload.get('is_end')}** · OOS ≥ **{payload.get('oos_start')}** · "
        f"n_slots={payload.get('n_slots')}",
        "",
        "## 研究問題",
        "",
        "1. 全日 ≥09:30 開窗（相對現行 ≥13:20）能否提升 OOS 超額？",
        "2. 固定提高 confirm_bars，或「越早越嚴、越晚越鬆」的自適應 schedule，"
        "能否在全天開窗下控噪？",
        "",
        "## 簡化假設",
        "",
    ]
    for c in payload.get("caveats") or []:
        lines.append(f"- {c}")
    lines += [
        "",
        f"自適應 schedule：`{payload.get('adapt_schedule')}`",
        "",
        f"## 結論：{vdict.get('summary')}",
        "",
    ]
    for n in vdict.get("notes") or []:
        lines.append(f"- {n}")
    lines += [
        "",
        "## 結果（Δ vs B0_1320_c1）",
        "",
        "| variant | window | n | mean_excess% | Δ vs B0 | entry_minute_hist |",
        "|---------|--------|--:|-------------:|--------:|-------------------|",
    ]
    for v in vs:
        for slice_name in ("full", "is", "oos"):
            sl = (v.get("slices") or {}).get(slice_name) or {}
            if slice_name == "full":
                dlt = v.get("delta_full_excess_pp")
                hist = _fmt_hist(v.get("entry_minute_hist") or {})
            elif slice_name == "is":
                dlt = v.get("delta_is_excess_pp")
                hist = "—"
            else:
                dlt = v.get("delta_oos_excess_pp")
                hist = "—"
            if v.get("variant_id") == "B0_1320_c1" or dlt is None:
                dlt_s = "—"
            else:
                dlt_s = f"{float(dlt):+.4f}pp"
            lines.append(
                f"| {v.get('variant_id')} | {slice_name.upper()} | {sl.get('n_legs')} | "
                f"{sl.get('mean_excess_pct')} | {dlt_s} | {hist} |"
            )
    thresh = vdict.get("thresholds") or {}
    lines += [
        "",
        f"門檻：OOS n≥{thresh.get('oos_min_n')} · Δ≥{thresh.get('oos_min_delta_pp')}pp · "
        f"IS Δ≥{thresh.get('is_min_delta_pp')}pp（相對 B0_1320_c1）",
        "",
        "模組：`src/research/backtest/c18acc_adaptive_confirm_allday_study.py`",
        "",
    ]
    return "\n".join(lines)


def _fmt_hist(hist: dict[str, int]) -> str:
    if not hist:
        return "—"
    items = sorted(hist.items(), key=lambda x: (-x[1], x[0]))[:6]
    return ", ".join(f"{k}:{v}" for k, v in items)
