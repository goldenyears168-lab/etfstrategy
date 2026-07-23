"""C18acc · swap buy-margin gate ablation (hold-out).

Preregistered topic: c18acc-swap-margin-ablation
Question: is seg_last still required as the swap buy threshold, or can a simpler
avg_accel / no-gate rule match or beat champion on OOS?
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

from market_benchmark import load_benchmark_close
from research.backtest.archive.c18acc_pool1_showcase import _ensure_avoid_mixed_gate
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import champion_entry_c_config
from research.backtest.rrg_mono_score_swap_c import (
    _poll5m_needs_spread_rs_panel,
    build_daily_spread_rs_panel,
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

MARGIN_VARIANTS: list[dict[str, Any]] = [
    {
        "id": "M0",
        "label": "champion · seg_last gate · margin=0.05",
        "score_margin": 0.05,
        "swap_margin_key": "seg_last",
    },
    {
        "id": "M1",
        "label": "seg_last gate · margin=0",
        "score_margin": 0.0,
        "swap_margin_key": "seg_last",
    },
    {
        "id": "M2",
        "label": "avg_accel gate · margin=0.05",
        "score_margin": 0.05,
        "swap_margin_key": "sort_key",
    },
    {
        "id": "M3",
        "label": "avg_accel gate · margin=0（須 > held accel）",
        "score_margin": 0.0,
        "swap_margin_key": "sort_key",
    },
    {
        "id": "M4",
        "label": "no margin gate · pick max avg_accel",
        "score_margin": 0.0,
        "swap_margin_key": "none",
    },
    {
        "id": "MD",
        "label": "Δ=Mom−Ratio gate · margin=0.05",
        "score_margin": 0.05,
        "swap_margin_key": "mom_minus_ratio",
    },
]


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
    print(f"C18acc margin ablation · {vid} · {spec['label']} …", flush=True)
    cfg = replace(
        champion_score_swap_c_config(),
        candidate_pool="fresh",
        n_slots=max(1, int(n_slots)),
        score_margin=float(spec["score_margin"]),
        swap_margin_key=str(spec["swap_margin_key"]),  # type: ignore[arg-type]
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
    baseline_id: str = "M0",
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
                "oos_swaps": s["oos"].get("swaps"),
                "full_swaps": s["full"].get("swaps"),
                "pass_strict": pass_strict if vid != baseline_id else None,
                "non_inferior": non_inf if vid != baseline_id else None,
            }
        )
    winners = [r for r in rows if r.get("pass_strict")]
    keepers = [r for r in rows if r.get("non_inferior")]
    if winners:
        verdict = "GO_SIMPLER"
        summary = (
            f"OOS pass (≥{pass_oos_pp}pp): {', '.join(r['variant_id'] for r in winners)}"
        )
    elif keepers:
        verdict = "NON_INFERIOR_ONLY"
        summary = (
            f"無嚴格勝出；非劣：{', '.join(r['variant_id'] for r in keepers)} · 可敘事簡化但不必採納"
        )
    else:
        verdict = "KEEP_SEG_LAST_MARGIN"
        summary = "簡化門檻均未通過 OOS · 維持 champion seg_last+0.05"
    return {
        "baseline_id": baseline_id,
        "pass_oos_pp": pass_oos_pp,
        "non_inferior_pp": non_inferior_pp,
        "min_oos_n": min_oos_n,
        "rows": rows,
        "verdict": verdict,
        "summary": summary,
    }


def run_swap_margin_ablation(
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
    specs = variants or list(MARGIN_VARIANTS)
    raw: list[dict[str, Any]] = []
    for spec in specs:
        raw.append(
            _run_variant(
                conn,
                trade_dates,
                spec=spec,
                ctx=ctx,
                gate=gate,
                n_slots=n_slots,
                confirm_bars=confirm_bars,
            )
        )
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
        "topic": "c18acc-swap-margin-ablation",
        "date_start": date_start,
        "date_end": end,
        "is_end": is_end,
        "oos_start": oos_start,
        "n_slots": n_slots,
        "confirm_bars": confirm_bars,
        "candidate_pool": "fresh",
        "entry_sort": "avg_accel_decel",
        "gate_meta": gate_meta,
        "variants": [
            {
                **{k: v for k, v in s.items()},
            }
            for s in slices
        ],
        "decision": decision,
    }


def render_swap_margin_ablation_md(payload: dict[str, Any]) -> str:
    d = payload.get("decision") or {}
    lines = [
        f"# C18acc · swap margin ablation · {payload.get('date_start')}…{payload.get('date_end')}",
        "",
        f"- Topic：`{payload.get('topic')}` · entry=`avg_accel` · pool=`fresh` · confirm={payload.get('confirm_bars')}",
        f"- Split：IS ≤ **{payload.get('is_end')}** · OOS ≥ **{payload.get('oos_start')}**",
        f"- Verdict：**{d.get('verdict')}** — {_fmt(d.get('summary'))}",
        "",
        "## Variants vs M0 (champion seg_last+0.05)",
        "",
        "| id | label | ΔOOS pp | ΔIS pp | ΔFULL pp | OOS n | FULL swaps | pass | non-inf |",
        "|----|-------|---------|--------|----------|-------|------------|------|---------|",
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
            "| id | FULL % | IS % | OOS % | FULL n |",
            "|----|--------|------|-------|--------|",
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
            f"- Pass OOS ≥ **{d.get('pass_oos_pp')}** pp · IS non-inferior ≥ **{d.get('non_inferior_pp')}** pp · "
            f"min OOS n **{d.get('min_oos_n')}**",
            "- M2 的 0.05 與 seg_last 同數字、不同量綱（exploratory）",
            "",
        ]
    )
    return "\n".join(lines)
