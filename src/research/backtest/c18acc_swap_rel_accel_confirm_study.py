"""C18acc · relative accel margin + swap confirm-days (H-BUY-1 / H-BUY-3).

Preregistered topic: c18acc-swap-rel-accel-confirm
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


def _gated_pool(
    ctx: dict[str, Any],
    as_of: str,
    *,
    cfg,
    gate: dict[str, set[str]] | None,
) -> list:
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


def calibrate_rel_accel_deltas(
    ctx: dict[str, Any],
    trade_dates: list[str],
    *,
    gate: dict[str, set[str]] | None,
    is_end: str,
    n_slots: int = 3,
) -> dict[str, Any]:
    """IS-only · Δaccel = best_challenger_accel − worst_held_accel on sell-eligible days.

    Uses champion sell filter (accel<0) and challengers that already pass seg_last+0.05
    vs worst held — so δ is calibrated where the dual gate would bind.
    """
    cfg = replace(champion_score_swap_c_config(), n_slots=n_slots, candidate_pool="fresh")
    is_dates = [d for d in trade_dates if d <= is_end]
    gaps_all: list[float] = []
    gaps_seg_pass: list[float] = []
    for as_of in is_dates:
        pool = _gated_pool(ctx, as_of, cfg=cfg, gate=gate)
        if len(pool) < 2:
            continue
        # synthetic 3-slot held = top-3 by seg_last (proxy for occupied book)
        held_rows = sorted(pool, key=lambda r: (-r.seg_last, r.stock_id))[:n_slots]
        if len(held_rows) < n_slots:
            continue
        acc: dict[str, float] = {}
        for r in pool:
            a = _avg_accel_scalar(
                ctx["rs_ratio"],
                ctx["rs_mom"],
                ctx["full_dates"],
                as_of,
                r.stock_id,
                lb=cfg.accel_lookback,
            )
            if a is not None:
                acc[r.stock_id] = float(a)
        held_acc = [(acc.get(r.stock_id), r) for r in held_rows if r.stock_id in acc]
        if not held_acc:
            continue
        sell_a, sell_row = min(held_acc, key=lambda x: x[0])  # type: ignore[arg-type]
        if sell_a is None or sell_a >= 0:
            continue
        held_ids = {r.stock_id for r in held_rows}
        chall = [r for r in pool if r.stock_id not in held_ids and r.stock_id in acc]
        if not chall:
            continue
        best = max(chall, key=lambda r: (acc[r.stock_id], r.stock_id))
        gap = acc[best.stock_id] - float(sell_a)
        gaps_all.append(gap)
        if float(best.seg_last) > float(sell_row.seg_last) + float(cfg.effective_margin):
            gaps_seg_pass.append(gap)

    def _pack(xs: list[float]) -> dict[str, Any]:
        return {
            "n": len(xs),
            "median": round(statistics.median(xs), 6) if xs else None,
            "p25": round(_pctile(xs, 0.25) or 0.0, 6) if xs else None,
            "p50": round(_pctile(xs, 0.50) or 0.0, 6) if xs else None,
            "p75": round(_pctile(xs, 0.75) or 0.0, 6) if xs else None,
            "mean": round(statistics.mean(xs), 6) if xs else None,
        }

    cal = {
        "is_end": is_end,
        "gaps_sell_eligible": _pack(gaps_all),
        "gaps_seg_margin_pass": _pack(gaps_seg_pass),
    }
    # Prefer δ from seg-pass cohort (dual-gate relevant); fallback sell-eligible.
    src = gaps_seg_pass if len(gaps_seg_pass) >= 8 else gaps_all
    p25 = _pctile(src, 0.25)
    p50 = _pctile(src, 0.50)
    cal["delta_p25"] = round(float(p25), 6) if p25 is not None else 0.01
    cal["delta_p50"] = round(float(p50), 6) if p50 is not None else 0.02
    cal["delta_source"] = "seg_margin_pass" if len(gaps_seg_pass) >= 8 else "sell_eligible"
    # Floor: tiny positive so δ=0 path (already rejected as M3) is not re-tested as "calibrated"
    cal["delta_p25"] = max(1e-4, float(cal["delta_p25"]))
    cal["delta_p50"] = max(float(cal["delta_p25"]), float(cal["delta_p50"]))
    return cal


def build_phase1_variants(cal: dict[str, Any]) -> list[dict[str, Any]]:
    d25 = float(cal["delta_p25"])
    d50 = float(cal["delta_p50"])
    return [
        {
            "id": "B0",
            "label": "champion · seg_last+0.05 · confirm=1",
            "score_margin": 0.05,
            "swap_margin_key": "seg_last",
            "swap_accel_margin": None,
            "swap_confirm_days": 1,
        },
        {
            "id": "R25",
            "label": f"accel gate · δ=IS_p25={d25:.4f}",
            "score_margin": d25,
            "swap_margin_key": "sort_key",
            "swap_accel_margin": None,
            "swap_confirm_days": 1,
        },
        {
            "id": "R50",
            "label": f"accel gate · δ=IS_p50={d50:.4f}",
            "score_margin": d50,
            "swap_margin_key": "sort_key",
            "swap_accel_margin": None,
            "swap_confirm_days": 1,
        },
        {
            "id": "D25",
            "label": f"dual · seg+0.05 ∧ accel≥δ_p25={d25:.4f}",
            "score_margin": 0.05,
            "swap_margin_key": "seg_last",
            "swap_accel_margin": d25,
            "swap_confirm_days": 1,
        },
        {
            "id": "C2",
            "label": "champion + swap_confirm_days=2",
            "score_margin": 0.05,
            "swap_margin_key": "seg_last",
            "swap_accel_margin": None,
            "swap_confirm_days": 2,
        },
        {
            "id": "DC2",
            "label": f"dual δ_p25 + confirm=2",
            "score_margin": 0.05,
            "swap_margin_key": "seg_last",
            "swap_accel_margin": d25,
            "swap_confirm_days": 2,
        },
    ]


def _run_variant(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    spec: dict[str, Any],
    ctx: dict[str, Any],
    gate: dict[str, set[str]] | None,
    n_slots: int,
    confirm_bars: int,
) -> dict[str, Any]:
    vid = str(spec["id"])
    print(f"C18acc rel-accel/confirm · {vid} · {spec['label']} …", flush=True)
    cfg = replace(
        champion_score_swap_c_config(),
        candidate_pool="fresh",
        n_slots=max(1, int(n_slots)),
        score_margin=float(spec["score_margin"]),
        swap_margin_key=str(spec["swap_margin_key"]),  # type: ignore[arg-type]
        swap_accel_margin=(
            None
            if spec.get("swap_accel_margin") is None
            else float(spec["swap_accel_margin"])
        ),
        swap_confirm_days=int(spec.get("swap_confirm_days") or 1),
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
        "score_margin": spec["score_margin"],
        "swap_margin_key": spec["swap_margin_key"],
        "swap_accel_margin": spec.get("swap_accel_margin"),
        "swap_confirm_days": spec.get("swap_confirm_days"),
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
    full_p = _slice_periods(periods, start=date_start, end=date_end)
    is_p = _slice_periods(periods, start=date_start, end=is_end)
    oos_p = _slice_periods(periods, start=oos_start, end=date_end)
    base_sum = {
        "swaps_total": result["stats"].get("swaps"),
        "slot_util_pct": result["stats"].get("slot_util_pct"),
    }
    return {
        "variant_id": result["variant_id"],
        "label": result["label"],
        "score_margin": result["score_margin"],
        "swap_margin_key": result["swap_margin_key"],
        "swap_accel_margin": result.get("swap_accel_margin"),
        "swap_confirm_days": result.get("swap_confirm_days"),
        "full": _period_stats(full_p, base_sum),
        "is": _period_stats(is_p, base_sum),
        "oos": _period_stats(oos_p, base_sum),
    }


def _delta_pp(challenger: float | None, baseline: float | None) -> float | None:
    if challenger is None or baseline is None:
        return None
    return round(float(challenger) - float(baseline), 4)


def evaluate_vs_baseline(
    slices: list[dict[str, Any]],
    *,
    baseline_id: str = "B0",
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
        oos_ex = _num(s["oos"].get("mean_excess_pct"))
        is_ex = _num(s["is"].get("mean_excess_pct"))
        d_oos = _delta_pp(oos_ex, base_oos)
        d_is = _delta_pp(is_ex, base_is)
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
            f"無嚴格勝出；非劣：{', '.join(r['variant_id'] for r in keepers)} · 不採納規則變更"
        )
    else:
        verdict = "KEEP_CHAMPION_SWAP"
        summary = "相對加速／confirm 均未過關 · 維持 seg_last+0.05 · confirm=1"
    return {
        "baseline_id": baseline_id,
        "pass_oos_pp": pass_oos_pp,
        "non_inferior_pp": non_inferior_pp,
        "min_oos_n": min_oos_n,
        "rows": rows,
        "verdict": verdict,
        "summary": summary,
    }


def run_rel_accel_confirm_study(
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
    print("Calibrating IS relative-accel δ …", flush=True)
    cal = calibrate_rel_accel_deltas(
        ctx, trade_dates, gate=gate, is_end=is_end, n_slots=n_slots
    )
    print(
        f"  δ_p25={cal['delta_p25']} δ_p50={cal['delta_p50']} source={cal['delta_source']}",
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
        "topic": "c18acc-swap-rel-accel-confirm",
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


def render_rel_accel_confirm_md(payload: dict[str, Any]) -> str:
    d = payload.get("decision") or {}
    cal = payload.get("calibration") or {}
    lines = [
        f"# C18acc · rel-accel margin + swap confirm · {payload.get('date_start')}…{payload.get('date_end')}",
        "",
        f"- Topic：`{payload.get('topic')}` · H-BUY-1 / H-BUY-3",
        f"- Split：IS ≤ **{payload.get('is_end')}** · OOS ≥ **{payload.get('oos_start')}**",
        f"- IS δ calibration：p25=**{cal.get('delta_p25')}** · p50=**{cal.get('delta_p50')}** · "
        f"source=`{cal.get('delta_source')}`",
        f"- Verdict：**{d.get('verdict')}** — {_fmt(d.get('summary'))}",
        "",
        "## Variants vs B0",
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
    lines.extend(["", "## Gates", "", f"- Pass OOS ≥ {d.get('pass_oos_pp')}pp · IS ≥ {d.get('non_inferior_pp')}pp", ""])
    return "\n".join(lines)
