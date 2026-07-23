"""C18acc champion · fresh-only vs POOL1 (fresh_union_accel) · Strategy adoption study."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
from research.backtest.archive.c18acc_pool1_showcase import _ensure_avoid_mixed_gate
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP
from research.backtest.rrg_mono_score_swap_c import (
    _fresh_union_accel_pool,
    _poll5m_needs_spread_rs_panel,
    build_daily_spread_rs_panel,
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from rrg_mono_daily_brief import LENGTH
from rrg_rotation import compute_rrg_panel

CandidatePool = Literal["fresh", "fresh_union_accel"]

ADOPT_OOS_MIN_N = 8
ADOPT_OOS_MIN_DELTA_PP = -0.2
ADOPT_IS_MIN_DELTA_PP = -0.5
ADOPT_MAX_LEG_DELTA_PCT = 5.0


def _slice_periods(
    periods: list[dict[str, Any]], *, start: str, end: str
) -> list[dict[str, Any]]:
    return [
        p
        for p in periods
        if start <= str(p.get("entry_date") or p.get("signal_date") or "") <= end
    ]


def _period_stats(periods: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    s = summarize_periods(periods)
    s["n_legs"] = len(periods)
    mr, mb = s.get("mean_return_pct"), s.get("mean_bench_pct")
    if mr is not None and mb is not None:
        s["mean_excess_pct"] = round(float(mr) - float(mb), 4)
    else:
        s["mean_excess_pct"] = summary.get("mean_excess_pct")
    s["swaps"] = summary.get("swaps_total")
    return s


def _entry_set(periods: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(str(p["stock_id"]), str(p["entry_date"])) for p in periods}


def _pool_composition_stats(
    conn: sqlite3.Connection,
    trade_dates: list[str],
) -> dict[str, Any]:
    fresh = build_fresh_mono_calendar(conn, trade_dates)
    mono = build_mono_tier2_calendar(conn, trade_dates)
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()

    fresh_sizes: list[int] = []
    pool_sizes: list[int] = []
    accel_only_days = 0
    accel_only_events = 0
    for d in trade_dates:
        fresh_rows = fresh.get(d, [])
        pool = _fresh_union_accel_pool(
            fresh_rows, mono.get(d, []), rs_ratio, rs_mom, full_dates, d
        )
        fresh_ids = {r.stock_id for r in fresh_rows}
        n_accel = sum(1 for r in pool if r.stock_id not in fresh_ids)
        fresh_sizes.append(len(fresh_rows))
        pool_sizes.append(len(pool))
        if n_accel:
            accel_only_days += 1
            accel_only_events += n_accel

    def _med(xs: list[int]) -> float:
        if not xs:
            return 0.0
        ys = sorted(xs)
        m = len(ys) // 2
        return float(ys[m]) if len(ys) % 2 else (ys[m - 1] + ys[m]) / 2.0

    return {
        "n_trade_dates": len(trade_dates),
        "fresh_median_per_day": _med(fresh_sizes),
        "pool1_median_per_day": _med(pool_sizes),
        "days_pool1_gt_fresh": accel_only_days,
        "accel_only_stock_days": accel_only_events,
        "days_fresh_zero": sum(1 for x in fresh_sizes if x == 0),
        "days_pool1_zero": sum(1 for x in pool_sizes if x == 0),
    }


def _resolve_gate(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    gate_cache_path: str | Path | None,
    live_aligned: bool,
    n_slots: int,
) -> tuple[dict[str, set[str]] | None, dict[str, Any]]:
    if not live_aligned:
        return None, {}
    gate, meta = _ensure_avoid_mixed_gate(
        conn,
        trade_dates,
        gate_cache_path=gate_cache_path,
        live_aligned=True,
        n_slots=n_slots,
    )
    return gate, meta


def _entry_c_config(confirm_bars: int):
    base = next(c for c in DEFAULT_C_SWEEP if c.variant_id == ("C0" if confirm_bars <= 1 else "C3"))
    if int(base.confirm_bars) == int(confirm_bars):
        return base
    return replace(base, confirm_bars=int(confirm_bars))


def _run_variant(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    candidate_pool: CandidatePool,
    gate: dict[str, set[str]] | None,
    confirm_bars: int,
    n_slots: int,
    ctx: dict[str, Any],
) -> dict[str, Any]:
    label = "POOL1" if candidate_pool == "fresh_union_accel" else "fresh-only"
    print(f"C18acc fresh-pool study · {label} · n_slots={n_slots} ...", flush=True)
    cfg = replace(
        champion_score_swap_c_config(),
        candidate_pool=candidate_pool,
        n_slots=max(1, int(n_slots)),
    )
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
        entry_c_config=_entry_c_config(confirm_bars),
        entry_gate_by_date=gate,
        swap_gate_by_date=gate,
    )
    stats = _period_stats(periods, summary)
    stats["candidate_pool"] = candidate_pool
    stats["variant_label"] = label
    print(
        f"  done {label}: n={stats['n_legs']} mean_excess={stats.get('mean_excess_pct')}% "
        f"swaps={stats.get('swaps')}",
        flush=True,
    )
    return {"periods": periods, "summary": summary, "stats": stats}


def _verdict(
    *,
    baseline: dict[str, Any],
    challenger: dict[str, Any],
    slices: dict[str, dict[str, dict[str, Any]]],
    pool_stats: dict[str, Any],
) -> dict[str, Any]:
    b_full = baseline["stats"]
    c_full = challenger["stats"]
    b_oos = slices["oos"]["baseline"]
    c_oos = slices["oos"]["challenger"]
    b_is = slices["is"]["baseline"]
    c_is = slices["is"]["challenger"]

    delta_full_pp = None
    if b_full.get("mean_excess_pct") is not None and c_full.get("mean_excess_pct") is not None:
        delta_full_pp = round(float(c_full["mean_excess_pct"]) - float(b_full["mean_excess_pct"]), 4)

    delta_oos_pp = None
    if b_oos.get("mean_excess_pct") is not None and c_oos.get("mean_excess_pct") is not None:
        delta_oos_pp = round(float(c_oos["mean_excess_pct"]) - float(b_oos["mean_excess_pct"]), 4)

    delta_is_pp = None
    if b_is.get("mean_excess_pct") is not None and c_is.get("mean_excess_pct") is not None:
        delta_is_pp = round(float(c_is["mean_excess_pct"]) - float(b_is["mean_excess_pct"]), 4)

    leg_delta = int(c_full.get("n_legs") or 0) - int(b_full.get("n_legs") or 0)
    leg_delta_pct = (
        round(100.0 * leg_delta / max(int(b_full.get("n_legs") or 0), 1), 2)
        if b_full.get("n_legs")
        else None
    )

    oos_n = int(c_oos.get("n_legs") or 0)
    oos_ok = (
        oos_n >= ADOPT_OOS_MIN_N
        and delta_oos_pp is not None
        and delta_oos_pp >= ADOPT_OOS_MIN_DELTA_PP
    )
    is_ok = delta_is_pp is None or delta_is_pp >= ADOPT_IS_MIN_DELTA_PP
    leg_ok = leg_delta_pct is None or abs(leg_delta_pct) <= ADOPT_MAX_LEG_DELTA_PCT
    adopt = bool(oos_ok and is_ok and leg_ok)

    b_entries = _entry_set(baseline["periods"])
    c_entries = _entry_set(challenger["periods"])

    return {
        "adopt_fresh_only": adopt,
        "recommended_candidate_pool": "fresh" if adopt else "fresh_union_accel",
        "delta_full_excess_pp": delta_full_pp,
        "delta_oos_excess_pp": delta_oos_pp,
        "delta_is_excess_pp": delta_is_pp,
        "delta_legs": leg_delta,
        "delta_legs_pct": leg_delta_pct,
        "shared_entry_events": len(b_entries & c_entries),
        "only_pool1_entries": sorted(b_entries - c_entries),
        "only_fresh_entries": sorted(c_entries - b_entries),
        "gates": {
            "oos_n_min": ADOPT_OOS_MIN_N,
            "oos_delta_min_pp": ADOPT_OOS_MIN_DELTA_PP,
            "is_delta_min_pp": ADOPT_IS_MIN_DELTA_PP,
            "max_leg_delta_pct": ADOPT_MAX_LEG_DELTA_PCT,
            "oos_pass": oos_ok,
            "is_pass": is_ok,
            "leg_count_pass": leg_ok,
        },
        "pool_stats": pool_stats,
        "summary": (
            f"{'採納 fresh-only' if adopt else '保留 POOL1'} · "
            f"FULL Δexcess={delta_full_pp}pp · OOS Δ={delta_oos_pp}pp (n={oos_n}) · "
            f"IS Δ={delta_is_pp}pp · Δlegs={leg_delta} ({leg_delta_pct}%) · "
            f"shared={len(b_entries & c_entries)}"
        ),
    }


def run_fresh_pool_adoption_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str = "2025-12-31",
    confirm_bars: int = 2,
    n_slots: int = 3,
    live_aligned: bool = True,
    gate_cache_path: str | Path | None = None,
) -> dict[str, Any]:
    """Live-aligned champion · POOL1 baseline vs fresh-only · slot re-sim."""
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    if date_end is None:
        date_end = full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    if len(trade_dates) < 20:
        raise ValueError(f"need ≥20 trade dates, got {len(trade_dates)}")

    oos_start = next((d for d in trade_dates if d > is_end), None)
    gate, gate_meta = _resolve_gate(
        conn,
        trade_dates,
        gate_cache_path=gate_cache_path,
        live_aligned=live_aligned,
        n_slots=n_slots,
    )
    pool_stats = _pool_composition_stats(conn, trade_dates)

    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=trade_dates[0], date_end=trade_dates[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    champ = champion_score_swap_c_config()
    spread_rs_panel = (
        build_daily_spread_rs_panel(close, bench)
        if _poll5m_needs_spread_rs_panel(champ)
        else None
    )
    ctx: dict[str, Any] = {
        "close": close,
        "bench": bench,
        "full_dates": full_dates,
        "fresh_by_date": fresh_by_date,
        "mono_by_date": mono_by_date,
        "zone_by_date": zone_by_date,
        "rs_ratio": rs_ratio,
        "rs_mom": rs_mom,
        "spread_rs_panel": spread_rs_panel,
        "kbar_cache": {},
    }

    baseline = _run_variant(
        conn,
        trade_dates,
        candidate_pool="fresh_union_accel",
        gate=gate,
        confirm_bars=confirm_bars,
        n_slots=n_slots,
        ctx=ctx,
    )
    challenger = _run_variant(
        conn,
        trade_dates,
        candidate_pool="fresh",
        gate=gate,
        confirm_bars=confirm_bars,
        n_slots=n_slots,
        ctx=ctx,
    )

    def _window_stats(periods: list[dict[str, Any]], summary: dict[str, Any], start: str, end: str) -> dict[str, Any]:
        return _period_stats(_slice_periods(periods, start=start, end=end), summary)

    slices: dict[str, dict[str, dict[str, Any]]] = {
        "full": {
            "baseline": _window_stats(
                baseline["periods"], baseline["summary"], date_start, date_end
            ),
            "challenger": _window_stats(
                challenger["periods"], challenger["summary"], date_start, date_end
            ),
        },
        "is": {
            "baseline": _window_stats(
                baseline["periods"], baseline["summary"], date_start, is_end
            ),
            "challenger": _window_stats(
                challenger["periods"], challenger["summary"], date_start, is_end
            ),
        },
        "oos": {
            "baseline": _window_stats(
                baseline["periods"],
                baseline["summary"],
                oos_start or date_end,
                date_end,
            )
            if oos_start
            else {"n_legs": 0, "mean_excess_pct": None},
            "challenger": _window_stats(
                challenger["periods"],
                challenger["summary"],
                oos_start or date_end,
                date_end,
            )
            if oos_start
            else {"n_legs": 0, "mean_excess_pct": None},
        },
    }

    verdict = _verdict(
        baseline=baseline,
        challenger=challenger,
        slices=slices,
        pool_stats=pool_stats,
    )

    return {
        "schema": "c18acc_fresh_pool_adoption-v1",
        "date_start": date_start,
        "date_end": date_end,
        "is_end": is_end,
        "oos_start": oos_start,
        "live_aligned": live_aligned,
        "confirm_bars": confirm_bars,
        "n_slots": n_slots,
        "gate_cache_path": str(gate_cache_path) if gate_cache_path else None,
        "gate_meta": gate_meta,
        "pool_composition": pool_stats,
        "baseline": {
            "candidate_pool": "fresh_union_accel",
            "stats": baseline["stats"],
            "slices": {k: v["baseline"] for k, v in slices.items()},
        },
        "challenger": {
            "candidate_pool": "fresh",
            "stats": challenger["stats"],
            "slices": {k: v["challenger"] for k, v in slices.items()},
        },
        "verdict": verdict,
    }


def render_fresh_pool_adoption_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    pool = payload.get("pool_composition") or {}
    lines = [
        "# C18acc champion · fresh-only vs POOL1 adoption study",
        "",
        f"窗口：**{payload['date_start']}** .. **{payload['date_end']}** · "
        f"live-aligned · confirm_bars={payload.get('confirm_bars')} · "
        f"n_slots={payload.get('n_slots')} · avoid spread_mixed @ 09:30",
        "",
        f"OOS 起點：{payload.get('oos_start') or '—'}（IS 截止 {payload.get('is_end')}）",
        "",
        "## 定池差異（理論）",
        "",
        f"- POOL1 中位數/天：{pool.get('pool1_median_per_day')} · fresh 中位數/天：{pool.get('fresh_median_per_day')}",
        f"- POOL1 > fresh 天數：{pool.get('days_pool1_gt_fresh')} / {pool.get('n_trade_dates')}",
        f"- accel-only 檔×日：{pool.get('accel_only_stock_days')}",
        "",
        "## 全窗口 · slot re-sim",
        "",
        "| pool | n legs | mean_excess% | swaps |",
        "|------|-------:|-------------:|------:|",
    ]
    for key, label in (("baseline", "POOL1 · fresh∪accel"), ("challenger", "fresh-only")):
        st = (payload.get(key) or {}).get("stats") or {}
        lines.append(
            f"| {label} | {st.get('n_legs')} | {st.get('mean_excess_pct')} | {st.get('swaps')} |"
        )
    lines += [
        "",
        f"- **Δ mean_excess（fresh − POOL1）**：{v.get('delta_full_excess_pp')}pp",
        f"- **Δ legs**：{v.get('delta_legs')} ({v.get('delta_legs_pct')}%)",
        f"- **共同 entry 事件**：{v.get('shared_entry_events')}",
        "",
        "## 分段",
        "",
        "| window | POOL1 n | POOL1 excess% | fresh n | fresh excess% | Δ excess pp |",
        "|--------|--------:|--------------:|--------:|--------------:|------------:|",
    ]
    for win in ("is", "oos", "full"):
        b = (payload.get("baseline") or {}).get("slices", {}).get(win) or {}
        c = (payload.get("challenger") or {}).get("slices", {}).get(win) or {}
        d = None
        if b.get("mean_excess_pct") is not None and c.get("mean_excess_pct") is not None:
            d = round(float(c["mean_excess_pct"]) - float(b["mean_excess_pct"]), 4)
        lines.append(
            f"| {win.upper()} | {b.get('n_legs')} | {b.get('mean_excess_pct')} "
            f"| {c.get('n_legs')} | {c.get('mean_excess_pct')} | {d} |"
        )
    gates = v.get("gates") or {}
    lines += [
        "",
        "## 採納建議",
        "",
        f"- **建議 candidate_pool**：`{v.get('recommended_candidate_pool')}`",
        f"- **採納 fresh-only**：{'是' if v.get('adopt_fresh_only') else '否'}",
        f"- OOS gate：n≥{gates.get('oos_n_min')} · Δ≥{gates.get('oos_delta_min_pp')}pp → "
        f"{'pass' if gates.get('oos_pass') else 'fail'}",
        f"- IS gate：Δ≥{gates.get('is_delta_min_pp')}pp → "
        f"{'pass' if gates.get('is_pass') else 'fail'}",
        f"- leg count：|Δ|≤{gates.get('max_leg_delta_pct')}% → "
        f"{'pass' if gates.get('leg_count_pass') else 'fail'}",
        f"- {v.get('summary', '')}",
        "",
        "模組：`src/research/backtest/c18acc_fresh_pool_adoption_study.py`",
    ]
    return "\n".join(lines) + "\n"
