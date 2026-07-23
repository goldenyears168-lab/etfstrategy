"""C18acc · sell-side gates · H-SELL-1 (jerk) / H-SELL-3 (relative weakness).

Preregistered topic: c18acc-swap-sell-gates
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import replace
from pathlib import Path
from typing import Any

from market_benchmark import load_benchmark_close
from research.backtest.archive.c18acc_pool1_showcase import _ensure_avoid_mixed_gate
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import champion_entry_c_config
from research.backtest.rrg_mono_score_swap_c import (
    _avg_accel_scalar,
    _poll5m_needs_spread_rs_panel,
    build_daily_spread_rs_panel,
    build_pit_candidate_pool,
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from rrg_rotation import compute_rrg_panel

IS_END_DEFAULT = "2025-12-31"
OOS_START_DEFAULT = "2026-01-02"
PASS_OOS_PP = 0.3
NON_INFERIOR_PP = -0.2
MIN_OOS_N = 8


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "Y" if v else "N"
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return str(v)


def _pctile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    if len(ys) == 1:
        return ys[0]
    k = (len(ys) - 1) * max(0.0, min(1.0, p))
    lo = int(k)
    hi = min(lo + 1, len(ys) - 1)
    w = k - lo
    return ys[lo] * (1.0 - w) + ys[hi] * w


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
    s["mean_excess_pct"] = (
        round(float(mr) - float(mb), 4) if mr is not None and mb is not None else None
    )
    s["swaps"] = summary.get("swaps_total")
    s["slot_util_pct"] = summary.get("slot_util_pct")
    return s


def _resolve_gate(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    gate_cache_path: str | Path | None,
    n_slots: int,
) -> tuple[dict[str, set[str]] | None, dict[str, Any]]:
    path = Path(gate_cache_path) if gate_cache_path else None
    if path and path.is_file():
        from research.backtest.archive.c18acc_avoid_mixed_slot_resim import load_gate_cache

        gate, meta, c0, c1 = load_gate_cache(str(path))
        if c0 == trade_dates[0] and c1 == trade_dates[-1]:
            return gate, meta
        print(
            f"gate cache window {c0}..{c1} != study {trade_dates[0]}..{trade_dates[-1]} · rebuilding …",
            flush=True,
        )
    gate, meta = _ensure_avoid_mixed_gate(
        conn,
        trade_dates,
        gate_cache_path=None,
        live_aligned=True,
        n_slots=n_slots,
    )
    return gate, meta


def _build_ctx(conn: sqlite3.Connection, trade_dates: list[str]) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    full_dates = [str(d) for d in close.index.astype(str).tolist()]
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=20)
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    from market_breadth_ma import build_breadth_panel

    panel = build_breadth_panel(conn, date_start=trade_dates[0], date_end=trade_dates[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    cfg = champion_score_swap_c_config()
    spread_rs_panel = (
        build_daily_spread_rs_panel(close, bench)
        if _poll5m_needs_spread_rs_panel(cfg)
        else None
    )
    return {
        "close": close,
        "bench": bench,
        "full_dates": full_dates,
        "rs_ratio": rs_ratio,
        "rs_mom": rs_mom,
        "fresh_by_date": fresh_by_date,
        "zone_by_date": zone_by_date,
        "mono_by_date": mono_by_date,
        "spread_rs_panel": spread_rs_panel,
        "kbar_cache": {},
    }


def _gated_pool(ctx: dict[str, Any], as_of: str, *, cfg, gate) -> list:
    pool = build_pit_candidate_pool(
        fresh_mono=ctx["fresh_by_date"].get(as_of, []),
        mono_rows=ctx["mono_by_date"].get(as_of, []),
        rs_ratio=ctx["rs_ratio"],
        rs_mom=ctx["rs_mom"],
        full_dates=ctx["full_dates"],
        as_of=as_of,
        config=cfg,
    )
    if gate is None:
        return pool
    allowed = gate.get(as_of)
    if allowed is None:
        return pool
    return [r for r in pool if r.stock_id in allowed]


def calibrate_rel_weak_gap(
    ctx: dict[str, Any],
    trade_dates: list[str],
    *,
    gate,
    is_end: str,
    n_slots: int = 3,
) -> dict[str, Any]:
    """IS · among synthetic full books with ≥2 neg-accel held, gap = 2nd_worst − worst."""
    cfg = replace(champion_score_swap_c_config(), n_slots=n_slots, candidate_pool="fresh")
    gaps: list[float] = []
    for as_of in trade_dates:
        if as_of > is_end:
            continue
        pool = _gated_pool(ctx, as_of, cfg=cfg, gate=gate)
        held_rows = sorted(pool, key=lambda r: (-r.seg_last, r.stock_id))[:n_slots]
        if len(held_rows) < 2:
            continue
        acc_vals: list[float] = []
        for r in held_rows:
            a = _avg_accel_scalar(
                ctx["rs_ratio"],
                ctx["rs_mom"],
                ctx["full_dates"],
                as_of,
                r.stock_id,
                lb=cfg.accel_lookback,
            )
            if a is not None and float(a) < 0:
                acc_vals.append(float(a))
        if len(acc_vals) < 2:
            continue
        acc_vals.sort()
        gaps.append(acc_vals[1] - acc_vals[0])

    p25 = _pctile(gaps, 0.25)
    p50 = _pctile(gaps, 0.50)
    return {
        "is_end": is_end,
        "n_gaps": len(gaps),
        "median": round(statistics.median(gaps), 6) if gaps else None,
        "gap_p25": round(max(1e-4, float(p25)), 6) if p25 is not None else 0.05,
        "gap_p50": round(max(1e-4, float(p50)), 6) if p50 is not None else 0.1,
    }


def build_phase1_variants(cal: dict[str, Any]) -> list[dict[str, Any]]:
    g25 = float(cal["gap_p25"])
    g50 = float(cal["gap_p50"])
    return [
        {
            "id": "S0",
            "label": "champion · accel<0 sell",
            "sell_accel_jerk_days": None,
            "sell_rel_weak_gap": None,
            "accel_sell_negative_only": True,
        },
        {
            "id": "J2",
            "label": "jerk≥2d + accel<0",
            "sell_accel_jerk_days": 2,
            "sell_rel_weak_gap": None,
            "accel_sell_negative_only": True,
        },
        {
            "id": "J2o",
            "label": "jerk≥2d only（取代 accel<0）",
            "sell_accel_jerk_days": 2,
            "sell_rel_weak_gap": None,
            "accel_sell_negative_only": False,
        },
        {
            "id": "W25",
            "label": f"rel-weak gap≥IS_p25={g25:.4f}",
            "sell_accel_jerk_days": None,
            "sell_rel_weak_gap": g25,
            "accel_sell_negative_only": True,
        },
        {
            "id": "W50",
            "label": f"rel-weak gap≥IS_p50={g50:.4f}",
            "sell_accel_jerk_days": None,
            "sell_rel_weak_gap": g50,
            "accel_sell_negative_only": True,
        },
        {
            "id": "JW",
            "label": f"jerk≥2 + gap≥p25={g25:.4f}",
            "sell_accel_jerk_days": 2,
            "sell_rel_weak_gap": g25,
            "accel_sell_negative_only": True,
        },
    ]


def _run_variant(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    spec: dict[str, Any],
    ctx: dict[str, Any],
    gate,
    n_slots: int,
    confirm_bars: int,
) -> dict[str, Any]:
    vid = str(spec["id"])
    print(f"C18acc sell-gates · {vid} · {spec['label']} …", flush=True)
    cfg = replace(
        champion_score_swap_c_config(),
        candidate_pool="fresh",
        n_slots=max(1, int(n_slots)),
        accel_sell_negative_only=bool(spec["accel_sell_negative_only"]),
        sell_accel_jerk_days=spec.get("sell_accel_jerk_days"),
        sell_rel_weak_gap=(
            None
            if spec.get("sell_rel_weak_gap") is None
            else float(spec["sell_rel_weak_gap"])
        ),
    )
    entry_c = champion_entry_c_config(confirm_bars=confirm_bars)
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
        f"  done {vid}: n={stats['n_legs']} mean_excess={stats.get('mean_excess_pct')}% "
        f"swaps={stats.get('swaps')}",
        flush=True,
    )
    return {
        "variant_id": vid,
        "label": spec["label"],
        "sell_accel_jerk_days": spec.get("sell_accel_jerk_days"),
        "sell_rel_weak_gap": spec.get("sell_rel_weak_gap"),
        "accel_sell_negative_only": spec.get("accel_sell_negative_only"),
        "stats": stats,
        "periods": periods,
    }


def _variant_slices(
    result: dict[str, Any],
    *,
    date_start: str,
    date_end: str,
    is_end: str,
    oos_start: str,
) -> dict[str, Any]:
    periods = result["periods"]
    base_sum = {
        "swaps_total": result["stats"].get("swaps"),
        "slot_util_pct": result["stats"].get("slot_util_pct"),
    }
    return {
        "variant_id": result["variant_id"],
        "label": result["label"],
        "sell_accel_jerk_days": result.get("sell_accel_jerk_days"),
        "sell_rel_weak_gap": result.get("sell_rel_weak_gap"),
        "accel_sell_negative_only": result.get("accel_sell_negative_only"),
        "full": _period_stats(_slice_periods(periods, start=date_start, end=date_end), base_sum),
        "is": _period_stats(_slice_periods(periods, start=date_start, end=is_end), base_sum),
        "oos": _period_stats(_slice_periods(periods, start=oos_start, end=date_end), base_sum),
    }


def _delta_pp(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return round(float(a) - float(b), 4)


def evaluate_vs_baseline(
    slices: list[dict[str, Any]],
    *,
    baseline_id: str = "S0",
    pass_oos_pp: float = PASS_OOS_PP,
    non_inferior_pp: float = NON_INFERIOR_PP,
    min_oos_n: int = MIN_OOS_N,
) -> dict[str, Any]:
    by_id = {s["variant_id"]: s for s in slices}
    base = by_id[baseline_id]
    base_oos = _num(base["oos"].get("mean_excess_pct"))
    base_is = _num(base["is"].get("mean_excess_pct"))
    rows: list[dict[str, Any]] = []
    for s in slices:
        vid = s["variant_id"]
        d_oos = _delta_pp(_num(s["oos"].get("mean_excess_pct")), base_oos)
        d_is = _delta_pp(_num(s["is"].get("mean_excess_pct")), base_is)
        n_oos = int(s["oos"].get("n_legs") or 0)
        pass_strict = (
            d_oos is not None
            and d_oos >= pass_oos_pp
            and n_oos >= min_oos_n
            and d_is is not None
            and d_is >= non_inferior_pp
        )
        non_inf = (
            d_oos is not None
            and d_oos >= non_inferior_pp
            and n_oos >= min_oos_n
            and d_is is not None
            and d_is >= non_inferior_pp
        )
        rows.append(
            {
                "variant_id": vid,
                "label": s["label"],
                "delta_oos_excess_pp": d_oos,
                "delta_is_excess_pp": d_is,
                "delta_full_excess_pp": _delta_pp(
                    _num(s["full"].get("mean_excess_pct")),
                    _num(base["full"].get("mean_excess_pct")),
                ),
                "oos_n": n_oos,
                "full_swaps": s["full"].get("swaps"),
                "pass_strict": pass_strict if vid != baseline_id else None,
                "non_inferior": non_inf if vid != baseline_id else None,
            }
        )
    winners = [r for r in rows if r.get("pass_strict")]
    keepers = [r for r in rows if r.get("non_inferior")]
    if winners:
        verdict = "GO_ADOPT"
        summary = f"OOS pass: {', '.join(r['variant_id'] for r in winners)}"
    elif keepers:
        verdict = "NON_INFERIOR_ONLY"
        summary = (
            f"無嚴格勝出；非劣：{', '.join(r['variant_id'] for r in keepers)} · 不採納"
        )
    else:
        verdict = "KEEP_CHAMPION_SELL"
        summary = "賣方 jerk / 相對弱度均未過關 · 維持 accel<0 可賣"
    return {
        "baseline_id": baseline_id,
        "pass_oos_pp": pass_oos_pp,
        "non_inferior_pp": non_inferior_pp,
        "min_oos_n": min_oos_n,
        "rows": rows,
        "verdict": verdict,
        "summary": summary,
    }


def run_sell_gates_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str = IS_END_DEFAULT,
    oos_start: str = OOS_START_DEFAULT,
    n_slots: int = 3,
    confirm_bars: int = 2,
    gate_cache_path: str | Path | None = None,
    variants: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    all_dates = [str(d) for d in close.index.astype(str).tolist()]
    end = date_end or all_dates[-1]
    trade_dates = [d for d in all_dates if date_start <= d <= end]
    if not trade_dates:
        raise ValueError(f"empty trade window {date_start}..{end}")

    gate, gate_meta = _resolve_gate(
        conn, trade_dates, gate_cache_path=gate_cache_path, n_slots=n_slots
    )
    ctx = _build_ctx(conn, trade_dates)
    print("Calibrating IS relative-weak gap …", flush=True)
    cal = calibrate_rel_weak_gap(
        ctx, trade_dates, gate=gate, is_end=is_end, n_slots=n_slots
    )
    print(
        f"  gap_p25={cal['gap_p25']} gap_p50={cal['gap_p50']} n={cal['n_gaps']}",
        flush=True,
    )
    specs = variants or build_phase1_variants(cal)
    raw = [
        _run_variant(
            conn,
            trade_dates,
            spec=spec,
            ctx=ctx,
            gate=gate,
            n_slots=n_slots,
            confirm_bars=confirm_bars,
        )
        for spec in specs
    ]
    slices = [
        _variant_slices(
            r,
            date_start=date_start,
            date_end=end,
            is_end=is_end,
            oos_start=oos_start,
        )
        for r in raw
    ]
    decision = evaluate_vs_baseline(slices)
    return {
        "topic": "c18acc-swap-sell-gates",
        "date_start": date_start,
        "date_end": end,
        "is_end": is_end,
        "oos_start": oos_start,
        "n_slots": n_slots,
        "confirm_bars": confirm_bars,
        "calibration": cal,
        "gate_meta": gate_meta,
        "variants": slices,
        "decision": decision,
    }


def render_sell_gates_md(payload: dict[str, Any]) -> str:
    d = payload.get("decision") or {}
    cal = payload.get("calibration") or {}
    lines = [
        f"# C18acc · sell gates (jerk / rel-weak) · {payload.get('date_start')}…{payload.get('date_end')}",
        "",
        f"- Topic：`{payload.get('topic')}` · H-SELL-1 / H-SELL-3",
        f"- Split：IS ≤ **{payload.get('is_end')}** · OOS ≥ **{payload.get('oos_start')}**",
        f"- IS gap calibration：p25=**{cal.get('gap_p25')}** · p50=**{cal.get('gap_p50')}** · n={cal.get('n_gaps')}",
        f"- Verdict：**{d.get('verdict')}** — {_fmt(d.get('summary'))}",
        "",
        "## Variants vs S0",
        "",
        "| id | label | ΔOOS | ΔIS | ΔFULL | OOS n | swaps | pass | non-inf |",
        "|----|-------|------|-----|-------|-------|-------|------|---------|",
    ]
    for r in d.get("rows") or []:
        lines.append(
            f"| {r['variant_id']} | {r['label']} | {_fmt(r.get('delta_oos_excess_pp'))} | "
            f"{_fmt(r.get('delta_is_excess_pp'))} | {_fmt(r.get('delta_full_excess_pp'))} | "
            f"{_fmt(r.get('oos_n'))} | {_fmt(r.get('full_swaps'))} | "
            f"{_fmt(r.get('pass_strict'))} | {_fmt(r.get('non_inferior'))} |"
        )
    lines.extend(
        [
            "",
            "## Absolute excess",
            "",
            "| id | FULL % | IS % | OOS % | n |",
            "|----|--------|------|-------|---|",
        ]
    )
    for s in payload.get("variants") or []:
        lines.append(
            f"| {s['variant_id']} | {_fmt(s['full'].get('mean_excess_pct'))} | "
            f"{_fmt(s['is'].get('mean_excess_pct'))} | {_fmt(s['oos'].get('mean_excess_pct'))} | "
            f"{_fmt(s['full'].get('n_legs'))} |"
        )
    lines.extend(
        [
            "",
            "## Gates",
            "",
            f"- Pass OOS ≥ {d.get('pass_oos_pp')}pp · IS ≥ {d.get('non_inferior_pp')}pp",
            "",
        ]
    )
    return "\n".join(lines)
