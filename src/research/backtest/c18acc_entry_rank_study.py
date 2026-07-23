"""C18acc · entry rank diagnostic + slot re-sim sweep (Phase 0–1).

Preregistered topic: c18acc-entry-rank-slot-resim
Plan: reports/research/rrg/20260712_c18acc_entry_rank_research_plan.md
"""

from __future__ import annotations

import sqlite3
import statistics
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

from market_benchmark import load_benchmark_close
from research.backtest.archive.c18acc_pool1_showcase import _ensure_avoid_mixed_gate
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import (
    DEFAULT_C_SWEEP,
    CVariantConfig,
    rank_shortlist_scale,
)
from research.backtest.rrg_mono_score_swap_c import (
    _avg_accel_scalar,
    _poll5m_needs_spread_rs_panel,
    build_daily_spread_rs_panel,
    build_pit_candidate_pool,
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from research.backtest.triple_wma_pullback_sweep import _mv, _rv
from rrg_mono_daily_brief import LENGTH, ScanRow
from rrg_rotation import compute_rrg_panel
from rrg_universe_intraday_panel import (
    WMA_MICRO_LENGTH,
    build_dual_wma_intraday_trajectories,
    intraday_poll_minutes,
)

IS_END_DEFAULT = "2025-12-31"
OOS_START_DEFAULT = "2026-01-02"
PASS_OOS_PP = 0.3
NON_INFERIOR_PP = -0.2
MIN_OOS_N = 8
HOLD_DAYS_CF = 5

PHASE1_VARIANTS: list[dict[str, Any]] = [
    {
        "id": "R0",
        "label": "scaled_seg_last · C3 confirm=2",
        "entry_variant_id": "C3",
        "swap_seg_last": False,
        "rank_mode": "scale",
    },
    {
        "id": "R1",
        "label": "full_rrg @ poll · C4 confirm=2",
        "entry_variant_id": "C4",
        "swap_seg_last": False,
        "rank_mode": "full_rrg",
    },
    {
        "id": "R2",
        "label": "avg_accel_decel entry rank · C3",
        "entry_variant_id": "C3",
        "swap_seg_last": False,
        "rank_mode": "avg_accel",
    },
    {
        "id": "R3",
        "label": "WMA composite entry rank · C3",
        "entry_variant_id": "C3",
        "swap_seg_last": False,
        "rank_mode": "wma_composite",
    },
    {
        "id": "R4",
        "label": "seg_last everywhere (audit control)",
        "entry_variant_id": "C3",
        "swap_seg_last": True,
        "rank_mode": "scale",
    },
]


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    u = a | b
    if not u:
        return 1.0
    return len(a & b) / len(u)


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


def _entry_c_config(variant_id: str, confirm_bars: int = 2) -> CVariantConfig:
    base = next(c for c in DEFAULT_C_SWEEP if c.variant_id == variant_id)
    if int(base.confirm_bars) == int(confirm_bars):
        return base
    return replace(base, confirm_bars=int(confirm_bars))


def _champion_cfg(*, n_slots: int, swap_seg_last: bool):
    cfg = replace(
        champion_score_swap_c_config(),
        candidate_pool="fresh",
        n_slots=max(1, int(n_slots)),
    )
    if swap_seg_last:
        cfg = replace(cfg, sort_key="seg_last", buy_sort_key="seg_last")
    return cfg


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


def _gated_pool_rows(
    ctx: dict[str, Any],
    as_of: str,
    *,
    cfg,
    gate: dict[str, set[str]] | None,
) -> list[ScanRow]:
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


def _pool_size_stats(
    ctx: dict[str, Any],
    trade_dates: list[str],
    *,
    cfg,
    gate: dict[str, set[str]] | None,
) -> dict[str, Any]:
    sizes: list[int] = []
    ge3 = 0
    ge2 = 0
    ge1 = 0
    for d in trade_dates:
        n = len(_gated_pool_rows(ctx, d, cfg=cfg, gate=gate))
        sizes.append(n)
        if n >= 1:
            ge1 += 1
        if n >= 2:
            ge2 += 1
        if n >= 3:
            ge3 += 1
    med = statistics.median(sizes) if sizes else 0
    return {
        "n_trade_dates": len(trade_dates),
        "days_ge1": ge1,
        "days_ge2": ge2,
        "days_ge3": ge3,
        "median_per_day": med,
        "pct_ge3": round(100.0 * ge3 / max(len(trade_dates), 1), 2),
    }


def _avg_accel_map(
    ctx: dict[str, Any], as_of: str, stock_ids: list[str], *, lb: int = 4
) -> dict[str, float]:
    out: dict[str, float] = {}
    for sid in stock_ids:
        v = _avg_accel_scalar(
            ctx["rs_ratio"],
            ctx["rs_mom"],
            ctx["full_dates"],
            as_of,
            sid,
            lb=lb,
        )
        if v is not None:
            out[sid] = float(v)
    return out


def _norm_map(raw: dict[str, float]) -> dict[str, float]:
    if not raw:
        return {}
    vals = list(raw.values())
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return {k: 0.5 for k in raw}
    return {k: (v - lo) / (hi - lo) for k, v in raw.items()}


def _wma_composite_from_point(pt: dict[str, Any]) -> float | None:
    w20_mv = _mv(pt, "w20")
    w3_rv = _rv(pt, "w3")
    w5_rv = _rv(pt, "w5")
    w20_rv = _rv(pt, "w20")
    if w20_mv is None or w3_rv is None or w5_rv is None or w20_rv is None:
        return None
    spread = float(w5_rv) - float(w20_rv)
    return 0.5 * float(w20_mv) + 0.3 * spread + 0.2 * float(w3_rv)


class _WmaRankCache:
    """Lazy per-day WMA composite scores for entry rank (R3)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        ctx: dict[str, Any],
        gate: dict[str, set[str]] | None,
        cfg,
    ) -> None:
        self._conn = conn
        self._ctx = ctx
        self._gate = gate
        self._cfg = cfg
        self._cache: dict[tuple[str, str, str], float] = {}

    def score(self, trade_date: str, minute: str, stock_id: str) -> float | None:
        key = (trade_date, minute, stock_id)
        if key in self._cache:
            return self._cache[key]
        self._ensure_day(trade_date)
        return self._cache.get(key)

    def _ensure_day(self, trade_date: str) -> None:
        if any(k[0] == trade_date for k in self._cache):
            return
        rows = _gated_pool_rows(self._ctx, trade_date, cfg=self._cfg, gate=self._gate)
        if not rows:
            return
        stock_ids = [r.stock_id for r in rows]
        poll_5m = intraday_poll_minutes(interval_min=5, start="09:05", end="13:20")
        frames, trajectories, _ = build_dual_wma_intraday_trajectories(
            self._conn,
            dates=[trade_date],
            stock_ids=stock_ids,
            poll_minutes=poll_5m,
            micro_lengths=(WMA_MICRO_LENGTH,),
            kbar_bar_minutes=5,
        )
        from research.backtest.dual_wma_signal_backtest import _build_stock_frame_maps

        maps = _build_stock_frame_maps(trajectories, frames)
        minute_idx: dict[str, int] = {}
        for i, f in enumerate(frames):
            if f["date"] == trade_date:
                minute_idx[f["minute"]] = i
        for sid in stock_ids:
            pts = maps.get(sid)
            if not pts:
                continue
            for minute in minute_idx:
                idx = minute_idx[minute]
                if idx >= len(pts):
                    continue
                pt = pts[idx]
                if not pt or pt.get("spread_class") == "mixed":
                    continue
                comp = _wma_composite_from_point(pt)
                if comp is not None:
                    self._cache[(trade_date, minute, sid)] = comp


def _build_wma_day_cache(
    conn: sqlite3.Connection,
    *,
    trade_dates: list[str],
    stock_ids: list[str],
) -> dict[tuple[str, str, str], float]:
    """Deprecated bulk builder · kept for tests; prefer _WmaRankCache."""
    del conn, trade_dates, stock_ids
    return {}


def _forward_hold_excess(
    ctx: dict[str, Any],
    *,
    entry_date: str,
    stock_id: str,
    entry_px: float,
    hold_days: int = HOLD_DAYS_CF,
) -> float | None:
    full_dates = ctx["full_dates"]
    close = ctx["close"]
    bench = ctx["bench"]
    if entry_date not in full_dates or stock_id not in close.columns:
        return None
    si = full_dates.index(entry_date)
    if si + hold_days >= len(full_dates):
        return None
    exit_d = full_dates[si + hold_days]
    c = close.at[exit_d, stock_id]
    b0 = bench.at[entry_date]
    b1 = bench.at[exit_d]
    if c != c or entry_px <= 0 or b0 != b0 or b1 != b1 or float(b0) <= 0:
        return None
    ret = float(c) / float(entry_px) - 1.0
    bench_ret = float(b1) / float(b0) - 1.0
    return round(100.0 * (ret - bench_ret), 4)


def run_entry_rank_diagnostic(
    conn: sqlite3.Connection,
    *,
    ctx: dict[str, Any],
    trade_dates: list[str],
    gate: dict[str, set[str]] | None,
    n_slots: int = 3,
) -> dict[str, Any]:
    cfg = _champion_cfg(n_slots=n_slots, swap_seg_last=False)
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}

    multi_days: list[dict[str, Any]] = []
    jaccards: list[float] = []
    rank34_gaps: list[float] = []
    cf_deltas: list[float] = []

    for d in trade_dates:
        rows = _gated_pool_rows(ctx, d, cfg=cfg, gate=gate)
        if len(rows) < 3:
            continue
        r930 = rank_shortlist_scale(
            rows,
            conn=conn,
            close=ctx["close"],
            trade_date=d,
            minute="09:30",
            kbar_cache=kbar_cache,
        )
        r935 = rank_shortlist_scale(
            rows,
            conn=conn,
            close=ctx["close"],
            trade_date=d,
            minute="09:35",
            kbar_cache=kbar_cache,
        )
        top930 = {r.stock_id for r in r930[:n_slots]}
        top935 = {r.stock_id for r in r935[:n_slots]}
        jac = _jaccard(top930, top935)
        jaccards.append(jac)

        gap = None
        if len(r935) >= 4:
            from research.backtest.rrg_mono_intraday_ab import (
                _kbar_px,
                intraday_price_scale,
                scaled_seg_last,
            )

            def _score(row: ScanRow) -> float:
                sid = row.stock_id
                close_px = float(ctx["close"].at[d, sid])
                px = _kbar_px(conn, sid, d, "09:35", kbar_cache, ctx["close"])
                scale = intraday_price_scale(close_px, px)
                return scaled_seg_last(row, scale)

            sc3, sc4 = _score(r935[2]), _score(r935[3])
            gap = round(abs(sc3 - sc4), 6)
            rank34_gaps.append(gap)

            px3 = _kbar_px(conn, r935[2].stock_id, d, "09:35", kbar_cache, ctx["close"])
            px4 = _kbar_px(conn, r935[3].stock_id, d, "09:35", kbar_cache, ctx["close"])
            if px3 and px4:
                ex3 = _forward_hold_excess(ctx, entry_date=d, stock_id=r935[2].stock_id, entry_px=float(px3))
                ex4 = _forward_hold_excess(ctx, entry_date=d, stock_id=r935[3].stock_id, entry_px=float(px4))
                if ex3 is not None and ex4 is not None:
                    cf_deltas.append(round(ex3 - ex4, 4))

        multi_days.append(
            {
                "date": d,
                "n_candidates": len(rows),
                "top3_0930": sorted(top930),
                "top3_0935": sorted(top935),
                "jaccard_0930_0935": round(jac, 4),
                "rank3_rank4_gap": gap,
            }
        )

    pool_stats = _pool_size_stats(ctx, trade_dates, cfg=cfg, gate=gate)
    med_jac = round(statistics.median(jaccards), 4) if jaccards else None
    med_gap = round(statistics.median(rank34_gaps), 6) if rank34_gaps else None
    med_cf = round(statistics.median(cf_deltas), 4) if cf_deltas else None
    mean_cf = round(statistics.mean(cf_deltas), 4) if cf_deltas else None
    max_abs_cf = round(max(abs(x) for x in cf_deltas), 4) if cf_deltas else None
    med_abs_cf = (
        round(statistics.median([abs(x) for x in cf_deltas]), 4) if cf_deltas else None
    )

    h_d0 = {
        "H-D0-1_pct_ge3": pool_stats["pct_ge3"],
        "H-D0-1_pass": pool_stats["pct_ge3"] < 15.0,
        "H-D0-2_median_jaccard": med_jac,
        "H-D0-2_pass": med_jac is not None and med_jac < 0.85,
        "H-D0-3_median_rank34_excess_delta_pp": med_cf,
        "H-D0-3_median_abs_delta_pp": med_abs_cf,
        "H-D0-3_max_abs_delta_pp": max_abs_cf,
        # pass if typical (median |Δ|) impact ≤ 0.5pp · max documented separately
        "H-D0-3_pass": med_abs_cf is not None and med_abs_cf <= 0.5,
    }

    return {
        "pool_stats": pool_stats,
        "multi_candidate_days": len(multi_days),
        "jaccard_median": med_jac,
        "rank34_gap_median": med_gap,
        "counterfactual_rank3_minus_rank4_pp": {
            "median": med_cf,
            "mean": mean_cf,
            "max_abs": max_abs_cf,
            "n": len(cf_deltas),
        },
        "hypothesis_checks": h_d0,
        "sample_days": multi_days[:15],
    }


@contextmanager
def _patched_entry_rank(
    rank_mode: str,
    *,
    ctx: dict[str, Any],
    wma_cache: _WmaRankCache | None = None,
) -> Iterator[None]:
    if rank_mode in ("scale", "full_rrg"):
        yield
        return

    import research.backtest.rrg_mono_intraday_ab as ab_mod

    orig = ab_mod.rank_shortlist

    def _patched(shortlist: list[ScanRow], **kwargs: Any) -> list[ScanRow]:
        config: CVariantConfig = kwargs["config"]
        trade_date: str = kwargs["trade_date"]
        minute: str = kwargs["minute"]
        if rank_mode == "avg_accel":
            acc = _avg_accel_map(ctx, trade_date, [r.stock_id for r in shortlist])
            return sorted(
                shortlist,
                key=lambda r: (-acc.get(r.stock_id, -999.0), r.stock_id),
            )
        if rank_mode == "wma_composite" and wma_cache is not None:
            raw: dict[str, float] = {}
            for r in shortlist:
                v = wma_cache.score(trade_date, minute, r.stock_id)
                if v is not None:
                    raw[r.stock_id] = v
            if not raw:
                return orig(shortlist, **kwargs)
            normed = _norm_map(raw)
            return sorted(
                shortlist,
                key=lambda r: (-normed.get(r.stock_id, -1.0), r.stock_id),
            )
        return orig(shortlist, **kwargs)

    ab_mod.rank_shortlist = _patched  # type: ignore[method-assign]
    try:
        yield
    finally:
        ab_mod.rank_shortlist = orig


def _run_rank_variant(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    spec: dict[str, Any],
    ctx: dict[str, Any],
    gate: dict[str, set[str]] | None,
    confirm_bars: int,
    n_slots: int,
    wma_cache: _WmaRankCache | None,
) -> dict[str, Any]:
    vid = str(spec["id"])
    print(f"C18acc entry-rank sweep · {vid} · {spec['label']} …", flush=True)
    cfg = _champion_cfg(n_slots=n_slots, swap_seg_last=bool(spec.get("swap_seg_last")))
    entry_c = _entry_c_config(str(spec["entry_variant_id"]), confirm_bars)
    if spec.get("rank_mode") == "full_rrg":
        entry_c = replace(entry_c, score_mode="full_rrg")

    with _patched_entry_rank(str(spec.get("rank_mode") or "scale"), ctx=ctx, wma_cache=wma_cache):
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
        "rank_mode": spec.get("rank_mode"),
        "entry_variant_id": spec.get("entry_variant_id"),
        "swap_seg_last": spec.get("swap_seg_last"),
        "stats": stats,
        "periods": periods,
    }


def _variant_slices(
    result: dict[str, Any], *, date_start: str, date_end: str, is_end: str, oos_start: str | None
) -> dict[str, dict[str, Any]]:
    periods = result["periods"]
    out: dict[str, dict[str, Any]] = {
        "full": _period_stats(_slice_periods(periods, start=date_start, end=date_end), result["stats"]),
        "is": _period_stats(_slice_periods(periods, start=date_start, end=is_end), result["stats"]),
    }
    if oos_start:
        out["oos"] = _period_stats(_slice_periods(periods, start=oos_start, end=date_end), result["stats"])
    return out


def _evaluate_vs_baseline(
    baseline: dict[str, Any],
    challenger: dict[str, Any],
    *,
    is_end: str,
) -> dict[str, Any]:
    b = baseline["slices"]
    c = challenger["slices"]
    delta: dict[str, Any] = {}
    for win in ("full", "is", "oos"):
        if win not in b or win not in c:
            continue
        be, ce = b[win].get("mean_excess_pct"), c[win].get("mean_excess_pct")
        if be is not None and ce is not None:
            delta[f"delta_{win}_excess_pp"] = round(float(ce) - float(be), 4)
        delta[f"delta_{win}_n"] = int(c[win].get("n_legs") or 0) - int(b[win].get("n_legs") or 0)
    oos_d = delta.get("delta_oos_excess_pp")
    is_d = delta.get("delta_is_excess_pp")
    oos_n = (c.get("oos") or {}).get("n_legs") if "oos" in c else None
    pass_strict = bool(
        oos_d is not None
        and float(oos_d) >= PASS_OOS_PP
        and oos_n is not None
        and int(oos_n) >= MIN_OOS_N
        and (is_d is None or float(is_d) >= -0.3)
    )
    pass_non_inf = bool(
        oos_d is not None
        and float(oos_d) >= NON_INFERIOR_PP
        and oos_n is not None
        and int(oos_n) >= MIN_OOS_N
    )
    return {
        **delta,
        "pass_strict": pass_strict,
        "pass_non_inferior": pass_non_inf,
        "recommend_adopt": pass_strict,
    }


def run_entry_rank_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-02",
    date_end: str | None = None,
    is_end: str = IS_END_DEFAULT,
    confirm_bars: int = 2,
    n_slots: int = 3,
    gate_cache_path: str | Path | None = None,
    variants: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    if date_end is None:
        date_end = full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    if len(trade_dates) < 20:
        raise ValueError(f"need ≥20 trade dates, got {len(trade_dates)}")

    oos_start = next((d for d in trade_dates if d >= OOS_START_DEFAULT), None)
    gate, gate_meta = _resolve_gate(
        conn, trade_dates, gate_cache_path=gate_cache_path, n_slots=n_slots
    )

    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    from market_breadth_ma import build_breadth_panel

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

    diagnostic = run_entry_rank_diagnostic(
        conn, ctx=ctx, trade_dates=trade_dates, gate=gate, n_slots=n_slots
    )

    cfg = _champion_cfg(n_slots=n_slots, swap_seg_last=False)
    wma_cache = _WmaRankCache(conn, ctx=ctx, gate=gate, cfg=cfg)

    specs = variants or PHASE1_VARIANTS
    results: list[dict[str, Any]] = []
    for spec in specs:
        raw = _run_rank_variant(
            conn,
            trade_dates,
            spec=spec,
            ctx=ctx,
            gate=gate,
            confirm_bars=confirm_bars,
            n_slots=n_slots,
            wma_cache=wma_cache,
        )
        slices = _variant_slices(
            raw,
            date_start=date_start,
            date_end=date_end,
            is_end=is_end,
            oos_start=oos_start,
        )
        item = {
            "variant_id": raw["variant_id"],
            "label": raw["label"],
            "rank_mode": raw["rank_mode"],
            "slices": slices,
        }
        if results:
            item["vs_R0"] = _evaluate_vs_baseline(
                {"slices": results[0]["slices"]}, {"slices": slices}, is_end=is_end
            )
        results.append(item)

    baseline = results[0] if results else None
    winner = None
    for r in results[1:]:
        ev = r.get("vs_R0") or {}
        if ev.get("recommend_adopt"):
            winner = r
            break

    if winner is None:
        verdict = {
            "outcome": "KEEP_BASELINE",
            "summary": (
                f"No rank variant passed OOS ≥{PASS_OOS_PP}pp · n≥{MIN_OOS_N}. "
                f"Recommend freeze entry_sort=c0_scaled_seg_last."
            ),
            "recommend_adopt": False,
        }
    else:
        verdict = {
            "outcome": "GO_RANK_KEY",
            "winner": winner["variant_id"],
            "summary": (
                f"Adopt {winner['variant_id']} ({winner['label']}) · "
                f"OOS Δ={(winner.get('vs_R0') or {}).get('delta_oos_excess_pp')}pp"
            ),
            "recommend_adopt": True,
        }

    return {
        "schema": "c18acc_entry_rank_sweep-v1",
        "date_start": date_start,
        "date_end": date_end,
        "is_end": is_end,
        "oos_start": oos_start,
        "confirm_bars": confirm_bars,
        "n_slots": n_slots,
        "gate_meta": gate_meta,
        "diagnostic": diagnostic,
        "variants": results,
        "verdict": verdict,
    }


def render_entry_rank_study_md(payload: dict[str, Any]) -> str:
    diag = payload.get("diagnostic") or {}
    pool = diag.get("pool_stats") or {}
    hc = diag.get("hypothesis_checks") or {}
    lines = [
        "# C18acc · 進場排序 · Phase 0 診斷 + Phase 1 slot re-sim",
        "",
        f"窗口：**{payload['date_start']}** .. **{payload['date_end']}** · "
        f"**{payload.get('n_slots')} 槽** · confirm={payload.get('confirm_bars')} · "
        f"fresh passthrough · avoid spread_mixed",
        "",
        f"IS ≤ **{payload.get('is_end')}** · OOS ≥ **{payload.get('oos_start')}**",
        "",
        "## Phase 0 · 診斷",
        "",
        "| 指標 | 值 |",
        "|------|-----|",
        f"| 候選 ≥1 天 | {pool.get('days_ge1')} |",
        f"| 候選 ≥2 天 | {pool.get('days_ge2')} |",
        f"| 候選 ≥3 天 | {pool.get('days_ge3')} ({pool.get('pct_ge3')}%) |",
        f"| 中位數/天 | {pool.get('median_per_day')} |",
        f"| top-3 Jaccard(09:30,09:35) median | {diag.get('jaccard_median')} |",
        f"| rank3–rank4 scaled gap median | {diag.get('rank34_gap_median')} |",
    ]
    cf = diag.get("counterfactual_rank3_minus_rank4_pp") or {}
    lines.extend(
        [
            f"| counterfactual excess rank3−rank4 (5d) median | {cf.get('median')}pp · n={cf.get('n')} |",
            "",
            "### 假說檢定（Phase 0）",
            "",
            f"- H-D0-1 episodic (<15% ≥3): **{'pass' if hc.get('H-D0-1_pass') else 'fail'}** "
            f"({hc.get('H-D0-1_pct_ge3')}%)",
            f"- H-D0-2 邊界不穩 (Jaccard median <0.85): **{'pass' if hc.get('H-D0-2_pass') else 'fail'}** "
            f"({hc.get('H-D0-2_median_jaccard')})",
            f"- H-D0-3 counterfactual median|Δ|≤0.5pp: **{'pass' if hc.get('H-D0-3_pass') else 'fail'}** "
            f"(median|Δ| {hc.get('H-D0-3_median_abs_delta_pp')}pp · max {hc.get('H-D0-3_max_abs_delta_pp')}pp)",
            "",
            "## Phase 1 · Rank key slot re-sim",
            "",
            "| variant | window | n | mean_excess% | swaps | vs R0 Δexcess | pass |",
            "|---------|--------|--:|-------------:|------:|--------------:|:----:|",
        ]
    )
    for v in payload.get("variants") or []:
        vid = v.get("variant_id")
        for win in ("full", "is", "oos"):
            sl = (v.get("slices") or {}).get(win) or {}
            vs = v.get("vs_R0") or {}
            dkey = f"delta_{win}_excess_pp"
            delta = "—" if vid == "R0" else vs.get(dkey, "—")
            adopt = "—" if vid == "R0" else ("✓" if vs.get("pass_strict") else "")
            lines.append(
                f"| {vid} | {win.upper()} | {sl.get('n_legs', '—')} | "
                f"{sl.get('mean_excess_pct', '—')} | {sl.get('swaps', '—')} | {delta} | {adopt} |"
            )
    v = payload.get("verdict") or {}
    lines.extend(
        [
            "",
            "## 結論",
            "",
            f"- **Outcome**：`{v.get('outcome')}`",
            f"- {v.get('summary', '')}",
            f"- 採納 rank key：**{'是' if v.get('recommend_adopt') else '否'}**",
            "",
            "模組：`src/research/backtest/c18acc_entry_rank_study.py`",
        ]
    )
    return "\n".join(lines) + "\n"


# ── G3 breadth stratification + adoption draft ──────────────────────────────

BREADTH_BUCKETS: dict[str, tuple[str, ...]] = {
    "risk_on": ("strong", "overbought"),
    "neutral": ("neutral",),
    "risk_off": ("weak", "oversold"),
}

BUCKET_LABELS: dict[str, str] = {
    "risk_on": "Strong/Overbought · 強勢/過熱",
    "neutral": "Neutral · 中性",
    "risk_off": "Weak/Oversold · 弱勢/超賣",
}

G3_MIN_N = 5


def _bucket_for_zone(zone: str | None) -> str:
    z = str(zone or "unknown")
    for bucket, zones in BREADTH_BUCKETS.items():
        if z in zones:
            return bucket
    return "unknown"


def _leg_excess(p: dict[str, Any]) -> float | None:
    if p.get("excess_pct") is not None:
        return float(p["excess_pct"])
    ret, bench = p.get("return_pct"), p.get("bench_return_pct")
    if ret is None:
        return None
    if bench is None:
        return None
    return float(ret) - float(bench)


def stratify_periods_by_breadth(periods: list[dict[str, Any]]) -> dict[str, Any]:
    from market_breadth_ma import BREADTH_ZONE_ZH, BREADTH_ZONES_ORDER

    by_zone: dict[str, list[float]] = {z: [] for z in BREADTH_ZONES_ORDER}
    by_bucket: dict[str, list[float]] = {b: [] for b in (*BREADTH_BUCKETS, "unknown")}
    for p in periods:
        ex = _leg_excess(p)
        if ex is None:
            continue
        zone = str(p.get("breadth_zone_200") or "unknown")
        by_zone.setdefault(zone, []).append(ex)
        by_bucket.setdefault(_bucket_for_zone(zone), []).append(ex)

    def _stats(vals: list[float]) -> dict[str, Any]:
        return {
            "n": len(vals),
            "mean_excess_pct": round(sum(vals) / len(vals), 4) if vals else None,
        }

    return {
        "by_zone": {
            z: {**_stats(v), "label": BREADTH_ZONE_ZH.get(z, z)}  # type: ignore[arg-type]
            for z, v in by_zone.items()
            if v
        },
        "by_bucket": {
            b: {**_stats(v), "label": BUCKET_LABELS.get(b, b)} for b, v in by_bucket.items() if v
        },
        "n_legs_with_excess": sum(len(v) for v in by_bucket.values()),
    }


def _delta_table(
    base: dict[str, Any],
    challenger: dict[str, Any],
    *,
    key: str,
) -> list[dict[str, Any]]:
    bmap = base.get(key) or {}
    cmap = challenger.get(key) or {}
    rows: list[dict[str, Any]] = []
    for name in sorted(set(bmap) | set(cmap)):
        b, c = bmap.get(name) or {}, cmap.get(name) or {}
        mb, mc = b.get("mean_excess_pct"), c.get("mean_excess_pct")
        nb, nc = int(b.get("n") or 0), int(c.get("n") or 0)
        delta = round(float(mc) - float(mb), 4) if mb is not None and mc is not None else None
        rows.append(
            {
                "id": name,
                "label": c.get("label") or b.get("label") or name,
                "n_r0": nb,
                "n_r2": nc,
                "mean_r0": mb,
                "mean_r2": mc,
                "delta_excess_pp": delta,
                "eligible": nb >= G3_MIN_N and nc >= G3_MIN_N and delta is not None,
            }
        )
    return rows


def evaluate_g3_breadth(
    *,
    r0_periods: list[dict[str, Any]],
    r2_periods: list[dict[str, Any]],
) -> dict[str, Any]:
    s0 = stratify_periods_by_breadth(r0_periods)
    s2 = stratify_periods_by_breadth(r2_periods)
    zone_rows = _delta_table(s0, s2, key="by_zone")
    bucket_rows = _delta_table(s0, s2, key="by_bucket")
    eligible = [r for r in bucket_rows if r.get("eligible")]
    negatives = [r for r in eligible if float(r["delta_excess_pp"]) < 0]
    all_negative = bool(eligible) and len(negatives) == len(eligible)
    risk_on = next((r for r in bucket_rows if r["id"] == "risk_on"), None)
    risk_on_ok = True
    if risk_on and risk_on.get("eligible"):
        risk_on_ok = float(risk_on["delta_excess_pp"]) >= -0.5

    g3_pass = bool(eligible) and (not all_negative) and risk_on_ok
    return {
        "r0": s0,
        "r2": s2,
        "by_zone_delta": zone_rows,
        "by_bucket_delta": bucket_rows,
        "eligible_buckets": len(eligible),
        "negative_eligible_buckets": len(negatives),
        "all_eligible_negative": all_negative,
        "risk_on_delta_pp": (risk_on or {}).get("delta_excess_pp"),
        "g3_pass": g3_pass,
        "summary": (
            f"eligible buckets={len(eligible)} · negative={len(negatives)} · "
            f"risk_on Δ={(risk_on or {}).get('delta_excess_pp')}pp · "
            f"{'PASS' if g3_pass else 'FAIL'}"
        ),
    }


def decide_r2_adoption(
    *,
    phase1_vs: dict[str, Any],
    g3: dict[str, Any],
) -> dict[str, Any]:
    """Final GO / NO_GO for R2 after G2 + G3."""
    g2_pass = bool(phase1_vs.get("pass_strict"))
    g3_pass = bool(g3.get("g3_pass"))
    oos = phase1_vs.get("delta_oos_excess_pp")
    is_d = phase1_vs.get("delta_is_excess_pp")
    full = phase1_vs.get("delta_full_excess_pp")

    # Conservative: require G2+G3 · FULL non-inferior · modest effect size note
    full_ok = full is not None and float(full) >= NON_INFERIOR_PP
    adopt = g2_pass and g3_pass and full_ok

    if adopt:
        outcome = "GO_ADOPT_R2"
        summary = (
            f"採納 R2 avg_accel 進場排序 · OOS Δ={oos}pp · IS Δ={is_d}pp · "
            f"FULL Δ={full}pp · G3 {g3.get('summary')}"
        )
    elif g2_pass and not g3_pass:
        outcome = "NO_GO_G3"
        summary = (
            f"G2 通過但 G3 廣度分層未過 · {g3.get('summary')} · "
            f"維持 entry_sort=c0_scaled_seg_last"
        )
    else:
        outcome = "NO_GO"
        summary = f"未達採納線 · G2={g2_pass} · G3={g3_pass}"

    return {
        "outcome": outcome,
        "recommend_adopt": adopt,
        "g2_pass": g2_pass,
        "g3_pass": g3_pass,
        "gates": {
            "G1": "passed",
            "G2": "passed" if g2_pass else "failed",
            "G3": "passed" if g3_pass else "failed",
            "G4": "drafted",
            "G5": "pending" if adopt else "na",
            "G6": "drafted",
        },
        "summary": summary,
    }


def run_entry_rank_g3_adoption(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str = IS_END_DEFAULT,
    confirm_bars: int = 2,
    n_slots: int = 3,
    gate_cache_path: str | Path | None = None,
) -> dict[str, Any]:
    """Re-sim R0 vs R2 · G3 breadth · adoption draft payload."""
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    if date_end is None:
        date_end = full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    oos_start = next((d for d in trade_dates if d >= OOS_START_DEFAULT), None)
    gate, gate_meta = _resolve_gate(
        conn, trade_dates, gate_cache_path=gate_cache_path, n_slots=n_slots
    )
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    from market_breadth_ma import build_breadth_panel

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

    specs = [v for v in PHASE1_VARIANTS if v["id"] in ("R0", "R2")]
    results: dict[str, dict[str, Any]] = {}
    for spec in specs:
        raw = _run_rank_variant(
            conn,
            trade_dates,
            spec=spec,
            ctx=ctx,
            gate=gate,
            confirm_bars=confirm_bars,
            n_slots=n_slots,
            wma_cache=None,
        )
        slices = _variant_slices(
            raw,
            date_start=date_start,
            date_end=date_end,
            is_end=is_end,
            oos_start=oos_start,
        )
        results[str(spec["id"])] = {**raw, "slices": slices}

    r0, r2 = results["R0"], results["R2"]
    vs = _evaluate_vs_baseline(
        {"slices": r0["slices"]}, {"slices": r2["slices"]}, is_end=is_end
    )
    g3 = evaluate_g3_breadth(r0_periods=r0["periods"], r2_periods=r2["periods"])

    # OOS-only G3
    r0_oos = _slice_periods(r0["periods"], start=oos_start or date_start, end=date_end)
    r2_oos = _slice_periods(r2["periods"], start=oos_start or date_start, end=date_end)
    g3_oos = evaluate_g3_breadth(r0_periods=r0_oos, r2_periods=r2_oos)

    decision = decide_r2_adoption(phase1_vs=vs, g3=g3)
    return {
        "schema": "c18acc_entry_rank_g3_adoption-v1",
        "date_start": date_start,
        "date_end": date_end,
        "is_end": is_end,
        "oos_start": oos_start,
        "confirm_bars": confirm_bars,
        "n_slots": n_slots,
        "gate_meta": gate_meta,
        "r0_slices": r0["slices"],
        "r2_slices": r2["slices"],
        "vs_R0": vs,
        "g3_full": g3,
        "g3_oos": g3_oos,
        "decision": decision,
    }


def render_r2_adoption_draft_md(payload: dict[str, Any]) -> str:
    """8-section adoption report draft (lineage §1.2)."""
    d = payload.get("decision") or {}
    vs = payload.get("vs_R0") or {}
    g3 = payload.get("g3_full") or {}
    g3o = payload.get("g3_oos") or {}
    gates = d.get("gates") or {}

    lines = [
        "# C18acc · 進場排序 R2 採納報告草稿",
        "",
        f"**Outcome**：`{d.get('outcome')}` · "
        f"**採納建議**：{'是' if d.get('recommend_adopt') else '否'}",
        "",
        f"{d.get('summary', '')}",
        "",
        f"窗口：**{payload['date_start']}** .. **{payload['date_end']}** · "
        f"IS ≤ {payload.get('is_end')} · OOS ≥ {payload.get('oos_start')} · "
        f"confirm={payload.get('confirm_bars')} · n_slots={payload.get('n_slots')} · "
        f"fresh + avoid_spread_mixed",
        "",
        "Parent：`rrg-mono-swap-accel` · Topic：`c18acc-entry-rank-slot-resim`",
        "",
        "---",
        "",
        "## 1 · 研究問題",
        "",
        "在 live-aligned 3 槽 · passthrough 定池下，09:35 進場排序是否應從 "
        "`c0_scaled_seg_last` 改為 `avg_accel_decel`（與換倉 key 對齊）？",
        "",
        "排序風險定義：≥3 候選日的擠掉／邊界代價（非 passthrough bug）。",
        "",
        "## 2 · 掃描範圍",
        "",
        "| Variant | Rank key | 狀態 |",
        "|---------|----------|------|",
        "| R0 | scaled_seg_last（baseline） | 對照 |",
        "| R1 | full_rrg @ poll | deferred（計算量） |",
        "| R2 | avg_accel_decel | **候選** |",
        "| R3 | WMA composite | rejected（IS 逆勢） |",
        "| R4 | seg_last everywhere | rejected（換倉劣化） |",
        "",
        "固定：`candidate_pool=fresh` · `confirm_bars=2` · `avoid_spread_mixed` · S2 exit。",
        "",
        "## 3 · 統計證明",
        "",
        "| window | R0 mean_excess% | R2 mean_excess% | Δ pp | n |",
        "|--------|----------------:|----------------:|-----:|--:|",
    ]
    for win in ("full", "is", "oos"):
        b = (payload.get("r0_slices") or {}).get(win) or {}
        c = (payload.get("r2_slices") or {}).get(win) or {}
        lines.append(
            f"| {win.upper()} | {b.get('mean_excess_pct')} | {c.get('mean_excess_pct')} | "
            f"{vs.get(f'delta_{win}_excess_pp')} | {c.get('n_legs')} |"
        )
    lines.extend(
        [
            "",
            f"- G2 OOS：Δ **{vs.get('delta_oos_excess_pp')}pp** · "
            f"pass_strict={vs.get('pass_strict')}",
            "",
            "## 4 · 採納決策",
            "",
            f"- **判決**：`{d.get('outcome')}`",
            f"- G1 preregistered：{gates.get('G1')}",
            f"- G2 OOS holdout：{gates.get('G2')}",
            f"- G3 breadth：{gates.get('G3')} · {g3.get('summary')}",
            f"- G4 rejection registry：{gates.get('G4')}",
            f"- G5 frozen spec：{gates.get('G5')}（僅 GO 時寫 strategy.yaml）",
            f"- G6 本報告：{gates.get('G6')}",
            "",
        ]
    )
    if d.get("recommend_adopt"):
        lines.extend(
            [
                "**凍結變更（若正式採納）**：",
                "",
                "- `config/strategy.yaml` · `funnel.score_keys.entry_sort` → "
                "`avg_accel_decel`（進場 C0 外層 rank）",
                "- Order layer：`rank_shortlist` 於 entry poll 使用四日平均加速",
                "- 換倉 key 不變（仍 avg_accel + seg_last margin）",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "**維持現狀**：`entry_sort=c0_scaled_seg_last` · 不改 Order layer。",
                "",
            ]
        )

    lines.extend(
        [
            "## 5 · 拒絕清單",
            "",
            "| 假說 / variant | 理由 |",
            "|----------------|------|",
            "| R3 WMA composite | OOS 看似優但 IS −0.84pp · 逆勢 |",
            "| R4 全程 seg_last | FULL −0.92pp · 換倉仍需 avg_accel |",
            "| R1 full_rrg | 計算量過大 · deferred · 非優先 |",
            "| W3 MV/RV 進場 gate | post-hoc −1.57pp · 不接 Order gate |",
            "| 恢復 top10 crop | 既有 ablation 稀釋品質 |",
            "",
            "## 6 · 分環境證據（G3）",
            "",
            f"**FULL**：{g3.get('summary')}",
            "",
            "| bucket | n R0 | n R2 | mean R0% | mean R2% | Δ pp |",
            "|--------|-----:|-----:|---------:|---------:|-----:|",
        ]
    )
    for r in g3.get("by_bucket_delta") or []:
        lines.append(
            f"| {r.get('label')} | {r.get('n_r0')} | {r.get('n_r2')} | "
            f"{r.get('mean_r0')} | {r.get('mean_r2')} | {r.get('delta_excess_pp')} |"
        )
    lines.extend(
        [
            "",
            "| zone | n R0 | n R2 | mean R0% | mean R2% | Δ pp |",
            "|------|-----:|-----:|---------:|---------:|-----:|",
        ]
    )
    for r in g3.get("by_zone_delta") or []:
        lines.append(
            f"| {r.get('label')} | {r.get('n_r0')} | {r.get('n_r2')} | "
            f"{r.get('mean_r0')} | {r.get('mean_r2')} | {r.get('delta_excess_pp')} |"
        )
    lines.extend(
        [
            "",
            f"**OOS 分層（參考）**：{g3o.get('summary')}",
            "",
            "| bucket (OOS) | n R0 | n R2 | Δ pp |",
            "|--------------|-----:|-----:|-----:|",
        ]
    )
    for r in g3o.get("by_bucket_delta") or []:
        lines.append(
            f"| {r.get('label')} | {r.get('n_r0')} | {r.get('n_r2')} | {r.get('delta_excess_pp')} |"
        )

    lines.extend(
        [
            "",
            "## 7 · 凍結規格（條件式）",
            "",
        ]
    )
    if d.get("recommend_adopt"):
        lines.extend(
            [
                "```yaml",
                "# config/strategy.yaml · rrg-mono-swap-accel · funnel.score_keys",
                "score_keys:",
                "  pool_rank: seg_last",
                "  entry_sort: avg_accel_decel   # was: c0_scaled_seg_last",
                "  swap_sell: avg_accel_decel",
                "  swap_buy: avg_accel_decel",
                "  swap_margin: seg_last",
                "```",
                "",
                "實作備註：C0 fill 仍用 poll_px；**shortlist 排序**改為四日 `avg_accel_decel`"
                "（PIT · 昨收 RRG），不再乘盤中 price scale。",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "無變更。維持：",
                "",
                "```yaml",
                "entry_sort: c0_scaled_seg_last",
                "```",
                "",
            ]
        )

    lines.extend(
        [
            "## 8 · 本機延伸",
            "",
            "- Sweep：`reports/research/rrg/20260712_c18acc_entry_rank_sweep.md`",
            "- Plan：`reports/research/rrg/20260712_c18acc_entry_rank_research_plan.md`",
            "- 模組：`src/research/backtest/c18acc_entry_rank_study.py`",
            "- Runner：`scripts/research/run_c18acc_entry_rank_sweep.py --g3-adoption`",
            "",
        ]
    )
    return "\n".join(lines) + "\n"
