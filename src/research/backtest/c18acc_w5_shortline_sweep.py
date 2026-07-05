"""C18acc short-line research · Phase 0–3 slot sweeps.

Phase 1–2: WMA(5) structural pool vs WMA(20) baseline.
Phase 3: WMA(20) POOL1 pool + shortened hold / W5 spread overlay (dual-WMA).
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import replace
from datetime import datetime
from typing import Any, Literal

import pandas as pd

from market_breadth_ma import build_breadth_panel
from market_benchmark import load_benchmark_close
from research.backtest.c18acc_drs_extension_overlay_sweep import (
    IS_END_DEFAULT,
    OOS_H1_END_DEFAULT,
    OOS_START_DEFAULT,
    build_daily_spread_rs_panel,
    _run_variant_window,
)
from research.backtest.c18acc_rotation_selective_exit_sweep import (
    rotation_selective_exit_sweep_configs,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    champion_score_swap_c_config,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from rrg_mono_daily_brief import LENGTH as WMA20_LENGTH
from rrg_rotation import compute_rrg_panel
from rrg_universe_intraday_panel import WMA_SHORT_LENGTH

WMA5_LENGTH = WMA_SHORT_LENGTH
SweepPhase = Literal[1, 2, 3, 4]

# Phase 1 (legacy · includes invalid accel=2 cells)
HOLD_GRID_P1: tuple[tuple[int, int], ...] = ((2, 5), (1, 3))
ACCEL_GRID_P1: tuple[int, ...] = (2, 3)
LOSS_GRID_P1: tuple[float, ...] = (-0.03, -0.04, -0.05)

# Phase 2 · valid lookback ≥3 · wider hold · margin · S2 recalibration
HOLD_GRID_P2: tuple[tuple[int, int], ...] = ((2, 5), (3, 7), (4, 8))
ACCEL_GRID_P2: tuple[int, ...] = (3, 4)
MARGIN_GRID_P2: tuple[float, ...] = (0.06, 0.08)
S2_GRID_P2: tuple[tuple[int, float, str], ...] = (
    (2, -0.05, "2d5"),
    (2, -0.03, "2d3"),
    (1, -0.05, "1d5"),
)
# spread pause overlay on W20-aligned S2 · mh3-7 / mh4-8 · accel 3/4
SPREAD_PAUSE_GRID_P2: tuple[tuple[int, int, int, float], ...] = (
    (3, 7, 3, 0.06),
    (3, 7, 4, 0.06),
    (4, 8, 3, 0.08),
    (4, 8, 4, 0.08),
)

# Phase 3 · W20 pool fixed · shorten hold and/or W5 spread overlay
HOLD_GRID_P3: tuple[tuple[int, int], ...] = ((2, 5), (3, 7), (4, 8))
HOLD_OVERLAY_GRID_P3: tuple[tuple[int, int], ...] = ((3, 7), (4, 8))


def _w20_pool1_s2(**overrides: Any) -> ScoreSwapCConfig:
    """W20 POOL1 · poll_5m · S2 · spread panel ready."""
    return _pool1_s2_config(rrg_length=WMA20_LENGTH, **overrides)


def _pool1_s2_config(**overrides: Any) -> ScoreSwapCConfig:
    """POOL1 · poll_5m · S2 weakening exit · champion accel fields."""
    s2 = next(c for c in rotation_selective_exit_sweep_configs() if c.variant_id == "S2")
    champ = champion_score_swap_c_config()
    base = replace(
        s2,
        timing_mode=champ.timing_mode,
        candidate_pool="fresh_union_accel",
        sort_key=champ.sort_key,
        buy_sort_key=champ.buy_sort_key,
        score_margin=champ.score_margin,
        accel_sell_negative_only=champ.accel_sell_negative_only,
        accel_lookback=champ.accel_lookback,
        candidate_lookback=champ.candidate_lookback,
        min_hold_days=champ.min_hold_days,
        max_hold_days=champ.max_hold_days,
    )
    fields = base.to_dict()
    fields.update(overrides)
    return ScoreSwapCConfig(**fields)


def _w20_ref_config() -> ScoreSwapCConfig:
    return _pool1_s2_config(
        variant_id="W20-REF",
        label="WMA(20) · mh5 mx10 · accel4 · S2 2d loss5%",
        rrg_length=WMA20_LENGTH,
    )


def w5_shortline_sweep_configs(*, phase: SweepPhase = 1) -> list[ScoreSwapCConfig]:
    """Preregistered sweep configs · phase 1, 2, or 3."""
    if phase == 1:
        return _phase1_configs()
    if phase == 2:
        return _phase2_configs()
    if phase == 3:
        return _phase3_configs()
    return _phase4_configs()


def _phase1_configs() -> list[ScoreSwapCConfig]:
    configs: list[ScoreSwapCConfig] = [_w20_ref_config()]
    for min_h, max_h in HOLD_GRID_P1:
        for accel_lb in ACCEL_GRID_P1:
            for loss_pct in LOSS_GRID_P1:
                loss_tag = int(abs(loss_pct) * 100)
                vid = f"W5-h{min_h}-{max_h}-a{accel_lb}-l{loss_tag}"
                configs.append(
                    _pool1_s2_config(
                        variant_id=vid,
                        label=(
                            f"WMA(5) · mh{min_h} mx{max_h} · accel{accel_lb} · "
                            f"S2 1d loss{loss_tag}%"
                        ),
                        min_hold_days=min_h,
                        max_hold_days=max_h,
                        accel_lookback=accel_lb,
                        candidate_lookback=accel_lb,
                        quad_force_exit_min_days=1,
                        quad_force_exit_loss_pct=loss_pct,
                        rrg_length=WMA5_LENGTH,
                    )
                )
    return configs


def _phase2_configs() -> list[ScoreSwapCConfig]:
    configs: list[ScoreSwapCConfig] = [_w20_ref_config()]
    for min_h, max_h in HOLD_GRID_P2:
        for accel_lb in ACCEL_GRID_P2:
            for margin in MARGIN_GRID_P2:
                m_tag = int(round(margin * 100))
                for force_days, loss_pct, s2_tag in S2_GRID_P2:
                    loss_tag = int(abs(loss_pct) * 100)
                    vid = f"W5P2-h{min_h}-{max_h}-a{accel_lb}-m{m_tag}-s{s2_tag}"
                    configs.append(
                        _pool1_s2_config(
                            variant_id=vid,
                            label=(
                                f"WMA(5) · mh{min_h} mx{max_h} · accel{accel_lb} · "
                                f"m{margin:.2f} · S2 {force_days}d loss{loss_tag}%"
                            ),
                            min_hold_days=min_h,
                            max_hold_days=max_h,
                            accel_lookback=accel_lb,
                            candidate_lookback=accel_lb,
                            score_margin=margin,
                            quad_force_exit_min_days=force_days,
                            quad_force_exit_loss_pct=loss_pct,
                            rrg_length=WMA5_LENGTH,
                        )
                    )
    for min_h, max_h, accel_lb, margin in SPREAD_PAUSE_GRID_P2:
        m_tag = int(round(margin * 100))
        vid = f"W5P2-h{min_h}-{max_h}-a{accel_lb}-m{m_tag}-s2d5-P"
        configs.append(
            _pool1_s2_config(
                variant_id=vid,
                label=(
                    f"WMA(5) · mh{min_h} mx{max_h} · accel{accel_lb} · "
                    f"m{margin:.2f} · S2 2d5 + spread pause"
                ),
                min_hold_days=min_h,
                max_hold_days=max_h,
                accel_lookback=accel_lb,
                candidate_lookback=accel_lb,
                score_margin=margin,
                quad_force_exit_min_days=2,
                quad_force_exit_loss_pct=-0.05,
                quad_force_exit_pause_spread_rs_positive=True,
                rrg_length=WMA5_LENGTH,
            )
        )
    return configs


def _phase3_configs() -> list[ScoreSwapCConfig]:
    """W20 POOL1 · dual-WMA short-line · shorten hold + W5 spread overlay."""
    configs: list[ScoreSwapCConfig] = [_w20_ref_config()]

    # Track A · shorten hold · S2 unchanged
    for min_h, max_h in HOLD_GRID_P3:
        configs.append(
            _pool1_s2_config(
                variant_id=f"W3P3-h{min_h}-{max_h}",
                label=f"W20 pool · mh{min_h} mx{max_h} · S2 2d5",
                min_hold_days=min_h,
                max_hold_days=max_h,
                rrg_length=WMA20_LENGTH,
            )
        )

    # Track B · baseline hold · W5 overlay on S2
    configs.extend(
        [
            _pool1_s2_config(
                variant_id="W3P3-S2sp",
                label="W20 pool · S2 2d5 + W5 spread pause",
                quad_force_exit_pause_spread_rs_positive=True,
                rrg_length=WMA20_LENGTH,
            ),
            _pool1_s2_config(
                variant_id="W3P3-DR3",
                label="W20 pool · S2 + spread_lost 1d loss3% stack",
                struct_force_exit_mode="spread_lost",
                struct_force_exit_min_days=1,
                struct_force_exit_loss_pct=-0.03,
                rrg_length=WMA20_LENGTH,
            ),
            _pool1_s2_config(
                variant_id="W3P3-DR4",
                label="W20 pool · S2 + drs_neg 2d loss5% stack",
                struct_force_exit_mode="drs_neg",
                struct_force_exit_min_days=2,
                struct_force_exit_loss_pct=-0.05,
                rrg_length=WMA20_LENGTH,
            ),
            _pool1_s2_config(
                variant_id="W3P3-bypass",
                label="W20 pool · S2 + accel sell bypass on spread_lost",
                accel_sell_bypass_on_spread_lost=True,
                rrg_length=WMA20_LENGTH,
            ),
        ]
    )

    # Track C · shorten hold + W5 overlay
    for min_h, max_h in HOLD_OVERLAY_GRID_P3:
        configs.extend(
            [
                _pool1_s2_config(
                    variant_id=f"W3P3-h{min_h}-{max_h}-DR3",
                    label=f"W20 · mh{min_h} mx{max_h} · S2 + spread_lost stack",
                    min_hold_days=min_h,
                    max_hold_days=max_h,
                    struct_force_exit_mode="spread_lost",
                    struct_force_exit_min_days=1,
                    struct_force_exit_loss_pct=-0.03,
                    rrg_length=WMA20_LENGTH,
                ),
                _pool1_s2_config(
                    variant_id=f"W3P3-h{min_h}-{max_h}-S2sp",
                    label=f"W20 · mh{min_h} mx{max_h} · S2 + spread pause",
                    min_hold_days=min_h,
                    max_hold_days=max_h,
                    quad_force_exit_pause_spread_rs_positive=True,
                    rrg_length=WMA20_LENGTH,
                ),
            ]
        )
    return configs


def _phase4_configs() -> list[ScoreSwapCConfig]:
    """W20 pool · conditional acceleration · spread-weak swap + slot-empty fill."""
    return [
        _w20_ref_config(),
        _w20_pool1_s2(
            variant_id="W4P4-bypass",
            label="spread≤0 → bypass accel sell · W20 pool",
            accel_sell_bypass_on_spread_lost=True,
        ),
        _w20_pool1_s2(
            variant_id="W4P4-bypass-S2sp",
            label="bypass + S2 spread pause (W5>W20 不 quad force)",
            accel_sell_bypass_on_spread_lost=True,
            quad_force_exit_pause_spread_rs_positive=True,
        ),
        _w20_pool1_s2(
            variant_id="W4P4-bypass-sw2",
            label="bypass + max_swaps=2/日",
            accel_sell_bypass_on_spread_lost=True,
            max_swaps_per_day=2,
        ),
        _w20_pool1_s2(
            variant_id="W4P4-fillrepeat",
            label="槽空 · batch_topk fill_repeat 加碼補滿",
            fill_mode="batch_topk",
            fill_repeat=True,
        ),
        _w20_pool1_s2(
            variant_id="W4P4-bypass-fill",
            label="bypass + fill_repeat 加碼補滿",
            accel_sell_bypass_on_spread_lost=True,
            fill_mode="batch_topk",
            fill_repeat=True,
        ),
        _w20_pool1_s2(
            variant_id="W4P4-bypass-mr",
            label="bypass + swap margin relax 負加速2日→0.03",
            accel_sell_bypass_on_spread_lost=True,
            swap_margin_relax_neg_accel_days=2,
            swap_margin_relax_to=0.03,
        ),
        _w20_pool1_s2(
            variant_id="W4P4-dual",
            label="bypass + S2sp + sw2 + fill_repeat",
            accel_sell_bypass_on_spread_lost=True,
            quad_force_exit_pause_spread_rs_positive=True,
            max_swaps_per_day=2,
            fill_mode="batch_topk",
            fill_repeat=True,
        ),
    ]


def _needs_spread_panel(cfg: ScoreSwapCConfig) -> bool:
    return bool(
        cfg.quad_force_exit_pause_spread_rs_positive
        or cfg.accel_sell_bypass_on_spread_lost
        or cfg.struct_force_exit_mode != "none"
    )


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    idx = (len(xs) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(xs) - 1)
    frac = idx - lo
    return round(xs[lo] * (1 - frac) + xs[hi] * frac, 4)


def run_w5_calibration_phase0(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
) -> dict[str, Any]:
    """Compare WMA(20) vs WMA(5) pool geometry · fresh/mono counts · disp distribution."""
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    lookbacks = (2, 3, 4)
    rows: list[dict[str, Any]] = []

    for rrg_length in (WMA20_LENGTH, WMA5_LENGTH):
        for lb in lookbacks:
            fresh_by = build_fresh_mono_calendar(
                conn, trade_dates, lookback=lb, rrg_length=rrg_length
            )
            mono_by = build_mono_tier2_calendar(
                conn,
                trade_dates,
                close=close,
                bench=load_benchmark_close(conn).reindex(close.index),
                rrg_length=rrg_length,
                lookback=lb,
            )
            fresh_counts = [len(v) for v in fresh_by.values()]
            mono_counts = [len(v) for v in mono_by.values()]
            disps: list[float] = []
            for pool in mono_by.values():
                for row in pool:
                    disps.append(float(row.disp))
            rows.append(
                {
                    "rrg_length": rrg_length,
                    "lookback": lb,
                    "n_trade_dates": len(trade_dates),
                    "fresh_days_nonempty": sum(1 for c in fresh_counts if c > 0),
                    "fresh_mean_per_day": round(statistics.mean(fresh_counts), 2)
                    if fresh_counts
                    else None,
                    "fresh_p90_per_day": _percentile([float(x) for x in fresh_counts], 0.9),
                    "mono_mean_per_day": round(statistics.mean(mono_counts), 2)
                    if mono_counts
                    else None,
                    "mono_p90_per_day": _percentile([float(x) for x in mono_counts], 0.9),
                    "disp_p50": _percentile(disps, 0.5),
                    "disp_p90": _percentile(disps, 0.9),
                    "disp_max": round(max(disps), 4) if disps else None,
                }
            )

    return {
        "phase": "calibration",
        "date_start": date_start,
        "date_end": date_end,
        "rows": rows,
    }


def _calendar_cache_key(rrg_length: int, lookback: int) -> tuple[int, int]:
    return (rrg_length, lookback)


def _build_comparisons(
    configs: list[ScoreSwapCConfig],
    by_variant: dict[str, dict[str, Any]],
    *,
    ref_full: dict[str, Any],
    ref_oos: dict[str, Any],
) -> list[dict[str, Any]]:
    ref_excess_full = float(ref_full.get("mean_excess_pct") or 0)
    ref_excess_oos = float(ref_oos.get("mean_excess_pct") or 0)
    ref_swaps = int(ref_full.get("swaps_total") or 0)
    ref_n = int(ref_full.get("n_periods") or 0)
    ref_win = ref_full.get("win_rate_vs_bench_pct")

    comparisons: list[dict[str, Any]] = []
    for cfg in configs:
        vid = cfg.variant_id
        if vid == "W20-REF":
            continue
        full = by_variant.get(vid, {}).get("FULL", {})
        oos = by_variant.get(vid, {}).get("OOS_H1", {})
        if full.get("error"):
            continue
        n_periods = int(full.get("n_periods") or 0)
        if n_periods <= 0:
            continue
        swaps = int(full.get("swaps_total") or 0)
        comparisons.append(
            {
                "variant_id": vid,
                "label": cfg.label,
                "rrg_length": cfg.rrg_length,
                "min_hold_days": cfg.min_hold_days,
                "max_hold_days": cfg.max_hold_days,
                "accel_lookback": cfg.accel_lookback,
                "score_margin": cfg.effective_margin,
                "quad_force_exit_min_days": cfg.quad_force_exit_min_days,
                "quad_force_exit_loss_pct": cfg.quad_force_exit_loss_pct,
                "spread_pause": cfg.quad_force_exit_pause_spread_rs_positive,
                "delta_excess_vs_w20_full_pp": round(
                    float(full.get("mean_excess_pct") or 0) - ref_excess_full, 4
                ),
                "delta_excess_vs_w20_oos_pp": round(
                    float(oos.get("mean_excess_pct") or 0) - ref_excess_oos, 4
                ),
                "delta_swaps_vs_w20": swaps - ref_swaps,
                "delta_n_periods_vs_w20": n_periods - ref_n,
                "turnover_ratio_vs_w20": round(n_periods / ref_n, 2) if ref_n else None,
                "mean_hold_days": full.get("mean_hold_days"),
                "swaps_total": swaps,
                "n_periods": n_periods,
                "force_exits": full.get("force_exits"),
                "win_rate_vs_bench_pct": full.get("win_rate_vs_bench_pct"),
                "delta_win_rate_pp": round(
                    float(full.get("win_rate_vs_bench_pct") or 0) - float(ref_win or 0), 2
                )
                if full.get("win_rate_vs_bench_pct") is not None and ref_win is not None
                else None,
                "mean_excess_pct": full.get("mean_excess_pct"),
                "oos_mean_excess_pct": oos.get("mean_excess_pct"),
                "full": full,
                "oos_h1": oos,
            }
        )
    return comparisons


def _phase2_verdict(
    comparisons: list[dict[str, Any]],
    *,
    ref_full: dict[str, Any],
) -> dict[str, Any]:
    ref_n = int(ref_full.get("n_periods") or 1)
    ref_excess = float(ref_full.get("mean_excess_pct") or 0)

    # Primary rank: turnover (n_periods) then mean excess
    ranked = sorted(
        comparisons,
        key=lambda x: (
            int(x.get("n_periods") or 0),
            float(x.get("mean_excess_pct") or -999),
        ),
        reverse=True,
    )
    best_turnover = ranked[0] if ranked else None

    # Best excess among high-turnover (≥1.5× W20 legs)
    high_turn = [c for c in comparisons if int(c.get("n_periods") or 0) >= int(ref_n * 1.5)]
    best_excess_ht = max(
        high_turn,
        key=lambda x: float(x.get("mean_excess_pct") or -999),
        default=None,
    )

    # Pareto candidates: turnover up + excess within 1pp of W20
    pareto = [
        c
        for c in comparisons
        if int(c.get("n_periods") or 0) >= ref_n
        and float(c.get("mean_excess_pct") or 0) >= ref_excess - 1.0
    ]
    pareto.sort(key=lambda x: (-int(x.get("n_periods") or 0), -float(x.get("mean_excess_pct") or 0)))

    h_turn = best_turnover is not None and int(best_turnover.get("n_periods") or 0) >= int(ref_n * 1.5)
    h_excess_ok = best_excess_ht is not None and float(best_excess_ht.get("mean_excess_pct") or 0) >= ref_excess - 1.0
    h_oos_ok = (
        best_excess_ht is not None
        and float(best_excess_ht.get("delta_excess_vs_w20_oos_pp") or -99) >= -0.5
    )

    return {
        "phase": 2,
        "h_turnover_1p5x": h_turn,
        "h_excess_within_1pp": h_excess_ok,
        "h_oos_within_0p5pp": h_oos_ok,
        "recommend_adopt": h_turn and h_excess_ok and h_oos_ok,
        "best_turnover_variant_id": best_turnover.get("variant_id") if best_turnover else None,
        "best_excess_high_turnover_id": best_excess_ht.get("variant_id") if best_excess_ht else None,
        "pareto_candidates": pareto[:5],
        "summary": (
            f"Best turnover {best_turnover.get('variant_id')} "
            f"n={best_turnover.get('n_periods')} ({best_turnover.get('turnover_ratio_vs_w20')}×) "
            f"excess={best_turnover.get('mean_excess_pct')}% · "
            f"Best HT-excess {best_excess_ht.get('variant_id') if best_excess_ht else '—'} "
            f"excess={best_excess_ht.get('mean_excess_pct') if best_excess_ht else '—'}%"
            if best_turnover
            else "No valid W5 Phase 2 variants"
        ),
    }


def _phase3_verdict(
    comparisons: list[dict[str, Any]],
    *,
    ref_full: dict[str, Any],
) -> dict[str, Any]:
    ref_n = int(ref_full.get("n_periods") or 1)
    ref_excess = float(ref_full.get("mean_excess_pct") or 0)

    ranked_excess = sorted(
        comparisons,
        key=lambda x: float(x.get("mean_excess_pct") or -999),
        reverse=True,
    )
    best_excess = ranked_excess[0] if ranked_excess else None

    pareto = [
        c
        for c in comparisons
        if int(c.get("n_periods") or 0) >= int(ref_n * 1.1)
        and float(c.get("mean_excess_pct") or 0) >= ref_excess - 0.5
    ]
    pareto.sort(key=lambda x: (-float(x.get("mean_excess_pct") or 0), -int(x.get("n_periods") or 0)))

    best_pareto = pareto[0] if pareto else None
    h_pareto = best_pareto is not None
    h_oos = (
        best_pareto is not None
        and float(best_pareto.get("delta_excess_vs_w20_oos_pp") or -99) >= -0.3
    )
    h_turn = (
        best_pareto is not None
        and int(best_pareto.get("n_periods") or 0) >= int(ref_n * 1.1)
    )

    return {
        "phase": 3,
        "h_dual_pareto": h_pareto,
        "h_turnover_1p1x": h_turn,
        "h_excess_within_0p5pp": h_pareto,
        "h_oos_within_0p3pp": h_oos,
        "recommend_adopt": h_pareto and h_oos,
        "best_excess_variant_id": best_excess.get("variant_id") if best_excess else None,
        "best_pareto_variant_id": best_pareto.get("variant_id") if best_pareto else None,
        "pareto_candidates": pareto[:5],
        "summary": (
            f"Best excess {best_excess.get('variant_id')} "
            f"excess={best_excess.get('mean_excess_pct')}% n={best_excess.get('n_periods')} · "
            f"Pareto {best_pareto.get('variant_id') if best_pareto else '—'} "
            f"(≥1.1× n & excess≥W20−0.5pp)"
            if best_excess
            else "No valid Phase 3 variants"
        ),
    }


def _portfolio_total_return_pct(row: dict[str, Any]) -> float:
    port = (row.get("full") or {}).get("portfolio") or {}
    val = port.get("total_return_pct")
    return float(val) if val is not None else -999.0


def _phase4_verdict(
    comparisons: list[dict[str, Any]],
    *,
    ref_full: dict[str, Any],
) -> dict[str, Any]:
    ref_excess = float(ref_full.get("mean_excess_pct") or 0)
    ref_port = (ref_full.get("portfolio") or {})
    ref_total = float(ref_port.get("total_return_pct") or 0)
    ref_n = int(ref_full.get("n_periods") or 1)

    ranked_total = sorted(
        comparisons,
        key=lambda x: (_portfolio_total_return_pct(x), float(x.get("mean_excess_pct") or -999)),
        reverse=True,
    )
    best_total = ranked_total[0] if ranked_total else None

    beat_w20 = [
        c
        for c in comparisons
        if float(c.get("delta_excess_vs_w20_full_pp") or -99) >= 0
        and float(c.get("delta_excess_vs_w20_oos_pp") or -99) >= -0.3
    ]
    beat_w20.sort(
        key=lambda x: (_portfolio_total_return_pct(x), float(x.get("mean_excess_pct") or -999)),
        reverse=True,
    )
    best_beat = beat_w20[0] if beat_w20 else None

    h_conditional = best_beat is not None
    h_oos = best_beat is not None and float(best_beat.get("delta_excess_vs_w20_oos_pp") or -99) >= -0.3
    h_total = best_beat is not None and _portfolio_total_return_pct(best_beat) >= ref_total

    return {
        "phase": 4,
        "h_conditional_beats_w20": h_conditional,
        "h_oos_within_0p3pp": h_oos,
        "h_total_return_ge_w20": h_total,
        "recommend_adopt": h_conditional and h_oos and h_total,
        "best_total_return_variant_id": best_total.get("variant_id") if best_total else None,
        "best_beat_w20_variant_id": best_beat.get("variant_id") if best_beat else None,
        "beat_w20_candidates": beat_w20[:5],
        "w20_ref_total_return_pct": ref_total,
        "summary": (
            f"Best total {best_total.get('variant_id')} "
            f"ret={(best_total.get('full') or {}).get('portfolio', {}).get('total_return_pct')}% "
            f"excess={best_total.get('mean_excess_pct')}% · "
            f"Beat W20 {best_beat.get('variant_id') if best_beat else '—'} "
            f"Δex={best_beat.get('delta_excess_vs_w20_full_pp') if best_beat else '—'}pp "
            f"OOS Δ={best_beat.get('delta_excess_vs_w20_oos_pp') if best_beat else '—'}pp"
            if best_total
            else "No valid Phase 4 variants"
        ),
    }


def _phase1_verdict(comparisons: list[dict[str, Any]]) -> dict[str, Any]:
    comparisons.sort(
        key=lambda x: (
            float(x.get("delta_excess_vs_w20_oos_pp") or -999),
            float(x.get("mean_excess_pct") or -999),
        ),
        reverse=True,
    )
    best = comparisons[0] if comparisons else None
    beat_w20_oos = [
        c
        for c in comparisons
        if float(c.get("delta_excess_vs_w20_oos_pp") or 0) >= -0.3
        and not (c.get("oos_h1") or {}).get("error")
    ]
    h1_pass = best is not None and float(best.get("delta_excess_vs_w20_full_pp") or -99) >= -0.5
    h2_pass = best is not None and int(best.get("delta_swaps_vs_w20") or 0) >= 4
    h3_pass = len(beat_w20_oos) > 0
    return {
        "phase": 1,
        "h1_maintains_excess": h1_pass,
        "h2_higher_turnover": h2_pass,
        "h3_oos_not_collapsed": h3_pass,
        "recommend_adopt": h1_pass and h3_pass and best is not None,
        "best_variant_id": best.get("variant_id") if best else None,
        "beat_w20_oos_candidates": beat_w20_oos[:5],
        "summary": (
            f"Best W5={best.get('variant_id')} FULL Δ={best.get('delta_excess_vs_w20_full_pp')}pp "
            f"OOS Δ={best.get('delta_excess_vs_w20_oos_pp')}pp "
            f"swaps Δ={best.get('delta_swaps_vs_w20')} hold={best.get('mean_hold_days')}d"
            if best
            else "No valid W5 variants"
        ),
    }


def run_w5_shortline_sweep(
    conn: sqlite3.Connection,
    *,
    phase: SweepPhase = 1,
    date_start: str = "2024-01-01",
    date_end: str | None = None,
    is_end: str = IS_END_DEFAULT,
    oos_h1_start: str = OOS_START_DEFAULT,
    oos_h1_end: str = OOS_H1_END_DEFAULT,
) -> dict[str, Any]:
    """Phase 0 + Phase 1/2/3 · W20-REF vs grid · IS / OOS H1 / FULL."""
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    full_dates = close.index.astype(str).tolist()
    if date_end is None:
        date_end = full_dates[-1]
    if date_end > full_dates[-1]:
        date_end = full_dates[-1]

    calibration = (
        None
        if phase == 3
        else run_w5_calibration_phase0(conn, date_start=date_start, date_end=date_end)
    )
    configs = w5_shortline_sweep_configs(phase=phase)
    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    spread_rs_panel = build_daily_spread_rs_panel(close, bench)
    kbar_cache: dict = {}

    windows = [
        ("IS", date_start, is_end),
        ("OOS_H1", oos_h1_start, min(oos_h1_end, date_end)),
        ("FULL", date_start, date_end),
    ]

    fresh_cache: dict[tuple[int, int], dict] = {}
    mono_cache: dict[tuple[int, int], dict] = {}
    rs_cache: dict[int, tuple[pd.DataFrame, pd.DataFrame]] = {}

    def _ensure_calendars(rrg_length: int, lookback: int) -> None:
        key = _calendar_cache_key(rrg_length, lookback)
        if key not in fresh_cache:
            trade_dates = [d for d in full_dates if date_start <= d <= date_end]
            fresh_cache[key] = build_fresh_mono_calendar(
                conn, trade_dates, lookback=lookback, rrg_length=rrg_length
            )
            mono_cache[key] = build_mono_tier2_calendar(
                conn,
                trade_dates,
                close=close,
                bench=bench,
                rrg_length=rrg_length,
                lookback=lookback,
            )
        if rrg_length not in rs_cache:
            rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=rrg_length)
            rs_cache[rrg_length] = (rs_ratio, rs_mom)

    rows: list[dict[str, Any]] = []
    for cfg in configs:
        rrg_length = int(cfg.rrg_length)
        lookback = int(cfg.candidate_lookback)
        _ensure_calendars(rrg_length, lookback)
        key = _calendar_cache_key(rrg_length, lookback)
        rs_ratio, rs_mom = rs_cache[rrg_length]
        spread = spread_rs_panel if _needs_spread_panel(cfg) else pd.DataFrame()
        for win_label, w_start, w_end in windows:
            if w_start > w_end:
                continue
            row = _run_variant_window(
                conn,
                cfg=cfg,
                date_start=w_start,
                date_end=w_end,
                label=win_label,
                close=close,
                bench=bench,
                rs_ratio=rs_ratio,
                rs_mom=rs_mom,
                spread_rs_panel=spread,
                full_dates=full_dates,
                fresh_by_date=fresh_cache[key],
                mono_by_date=mono_cache[key],
                zone_by_date=zone_by_date,
                c0_cfg=c0_cfg,
                kbar_cache=kbar_cache,
            )
            row["rrg_length"] = rrg_length
            row["min_hold_days"] = cfg.min_hold_days
            row["max_hold_days"] = cfg.max_hold_days
            row["accel_lookback"] = cfg.accel_lookback
            row["score_margin"] = cfg.effective_margin
            row["quad_force_exit_min_days"] = cfg.quad_force_exit_min_days
            row["quad_force_exit_loss_pct"] = cfg.quad_force_exit_loss_pct
            row["spread_pause"] = cfg.quad_force_exit_pause_spread_rs_positive
            row["struct_force_exit_mode"] = cfg.struct_force_exit_mode
            row["accel_sell_bypass_on_spread_lost"] = cfg.accel_sell_bypass_on_spread_lost
            rows.append(row)

    by_variant: dict[str, dict[str, Any]] = {}
    for r in rows:
        vid = str(r.get("variant_id", ""))
        by_variant.setdefault(vid, {})[str(r.get("label", ""))] = r

    ref_full = by_variant.get("W20-REF", {}).get("FULL", {})
    ref_oos = by_variant.get("W20-REF", {}).get("OOS_H1", {})
    comparisons = _build_comparisons(
        configs, by_variant, ref_full=ref_full, ref_oos=ref_oos
    )

    if phase == 2:
        comparisons.sort(
            key=lambda x: (int(x.get("n_periods") or 0), float(x.get("mean_excess_pct") or -999)),
            reverse=True,
        )
        verdict = _phase2_verdict(comparisons, ref_full=ref_full)
    elif phase == 3:
        comparisons.sort(
            key=lambda x: (float(x.get("mean_excess_pct") or -999), int(x.get("n_periods") or 0)),
            reverse=True,
        )
        verdict = _phase3_verdict(comparisons, ref_full=ref_full)
    elif phase == 4:
        comparisons.sort(
            key=lambda x: (_portfolio_total_return_pct(x), float(x.get("mean_excess_pct") or -999)),
            reverse=True,
        )
        verdict = _phase4_verdict(comparisons, ref_full=ref_full)
    else:
        verdict = _phase1_verdict(comparisons)

    schema_map = {
        1: "c18acc_w5_shortline_sweep-v1",
        2: "c18acc_w5_shortline_sweep-v2",
        3: "c18acc_w5_dual_shortline_sweep-v1",
        4: "c18acc_w20_conditional_accel_sweep-v1",
    }
    schema = schema_map[phase]

    return {
        "schema": schema,
        "topic": (
            "c18acc-w20-w5-dual-shortline"
            if phase == 3
            else "c18acc-w20-conditional-accel"
            if phase == 4
            else "c18acc-w5-shortline"
        ),
        "phase": phase,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": date_end,
        "is_end": is_end,
        "oos_h1_start": oos_h1_start,
        "oos_h1_end": oos_h1_end,
        "n_configs": len(configs),
        "baseline_w20": {
            "variant_id": "W20-REF",
            "full": ref_full,
            "oos_h1": ref_oos,
            "mean_hold_days": ref_full.get("mean_hold_days"),
            "swaps_total": ref_full.get("swaps_total"),
            "n_periods": ref_full.get("n_periods"),
            "win_rate_vs_bench_pct": ref_full.get("win_rate_vs_bench_pct"),
        },
        "phase0_calibration": calibration,
        "variants": by_variant,
        "comparisons": comparisons,
        "verdict": verdict,
    }


def render_w5_shortline_md(payload: dict[str, Any]) -> str:
    """Markdown report for W5 short-line sweep."""
    phase = payload.get("phase", 1)
    cal = payload.get("phase0_calibration") or {}
    cal_rows = cal.get("rows") or []
    verdict = payload.get("verdict") or {}
    baseline = payload.get("baseline_w20") or {}
    b_full = baseline.get("full") or {}

    lines = [
        f"# C18acc short-line research · Phase {phase}",
        "",
        f"> {payload.get('date_start')} → {payload.get('date_end')} · "
        f"{payload.get('n_configs')} configs · topic `{payload.get('topic')}`",
        "",
        "## Baseline W20-REF",
        "",
        f"- n_periods **{b_full.get('n_periods')}** · mean_excess **{b_full.get('mean_excess_pct')}%** · "
        f"win **{b_full.get('win_rate_vs_bench_pct')}%** · "
        f"swaps **{b_full.get('swaps_total')}** · hold **{b_full.get('mean_hold_days')}** d",
        "",
    ]

    if phase == 3:
        lines.extend(
            [
                "## Phase 3 · W20 pool + shorten hold / W5 overlay (FULL)",
                "",
                "| variant | n | ×W20 | excess | Δex | win | hold | swaps | quad | struct | OOS Δ |",
                "|---------|---|------|--------|-----|-----|------|-------|------|--------|-------|",
            ]
        )
        for c in (payload.get("comparisons") or []):
            full = c.get("full") or {}
            lines.append(
                f"| {c.get('variant_id')} | {c.get('n_periods')} | "
                f"{c.get('turnover_ratio_vs_w20')} | {c.get('mean_excess_pct')} | "
                f"{c.get('delta_excess_vs_w20_full_pp')} | {c.get('win_rate_vs_bench_pct')} | "
                f"{c.get('mean_hold_days')} | {c.get('swaps_total')} | "
                f"{full.get('quad_force_exits')} | {full.get('struct_force_exits')} | "
                f"{c.get('delta_excess_vs_w20_oos_pp')} |"
            )
        pareto = verdict.get("pareto_candidates") or []
        if pareto:
            lines.extend(["", "## Pareto · n≥1.1× W20 & excess ≥ W20−0.5pp", ""])
            for c in pareto:
                lines.append(
                    f"- **{c.get('variant_id')}** · n={c.get('n_periods')} · "
                    f"excess={c.get('mean_excess_pct')}% · OOS Δ={c.get('delta_excess_vs_w20_oos_pp')}pp"
                )
    elif phase == 4:
        lines.extend(
            [
                "## Phase 4 · W20 pool · conditional accel (FULL)",
                "",
                "| variant | n | excess | Δex | total ret | fill_rep | swaps | slot_util | OOS Δ |",
                "|---------|---|--------|-----|-----------|----------|-------|-----------|-------|",
            ]
        )
        for c in (payload.get("comparisons") or []):
            full = c.get("full") or {}
            port = full.get("portfolio") or {}
            lines.append(
                f"| {c.get('variant_id')} | {c.get('n_periods')} | "
                f"{c.get('mean_excess_pct')} | {c.get('delta_excess_vs_w20_full_pp')} | "
                f"{port.get('total_return_pct')} | {full.get('fill_repeat_days')} | "
                f"{c.get('swaps_total')} | {full.get('slot_util_pct')} | "
                f"{c.get('delta_excess_vs_w20_oos_pp')} |"
            )
        beat = verdict.get("beat_w20_candidates") or []
        if beat:
            lines.extend(["", "## Beat W20 · excess ≥ W20 & OOS ≥ −0.3pp", ""])
            for c in beat:
                port = (c.get("full") or {}).get("portfolio") or {}
                lines.append(
                    f"- **{c.get('variant_id')}** · excess={c.get('mean_excess_pct')}% · "
                    f"total={port.get('total_return_pct')}% · OOS Δ={c.get('delta_excess_vs_w20_oos_pp')}pp"
                )
    elif phase == 2:
        lines.extend(
            [
                "## Phase 2 · top by turnover (FULL)",
                "",
                "| variant | n | ×W20 | excess | win | Δwin | swaps | force | OOS Δ |",
                "|---------|---|------|--------|-----|------|-------|-------|-------|",
            ]
        )
        for c in (payload.get("comparisons") or [])[:12]:
            lines.append(
                f"| {c.get('variant_id')} | {c.get('n_periods')} | "
                f"{c.get('turnover_ratio_vs_w20')} | {c.get('mean_excess_pct')} | "
                f"{c.get('win_rate_vs_bench_pct')} | {c.get('delta_win_rate_pp')} | "
                f"{c.get('swaps_total')} | {c.get('force_exits')} | "
                f"{c.get('delta_excess_vs_w20_oos_pp')} |"
            )
        pareto = verdict.get("pareto_candidates") or []
        if pareto:
            lines.extend(["", "## Pareto · turnover↑ + excess ≥ W20−1pp", ""])
            for c in pareto:
                lines.append(
                    f"- **{c.get('variant_id')}** · n={c.get('n_periods')} · "
                    f"excess={c.get('mean_excess_pct')}% · win={c.get('win_rate_vs_bench_pct')}%"
                )
    else:
        lines.extend(
            [
                "## Phase 0 · pool geometry",
                "",
                "| length | lb | fresh/day | mono/day | disp p50 |",
                "|--------|-----|-----------|----------|----------|",
            ]
        )
        for r in cal_rows:
            lines.append(
                f"| {r.get('rrg_length')} | {r.get('lookback')} | "
                f"{r.get('fresh_mean_per_day')} | {r.get('mono_mean_per_day')} | "
                f"{r.get('disp_p50')} |"
            )
        lines.extend(
            [
                "",
                "## Phase 1 · top W5 vs W20",
                "",
                "| variant | Δ excess | swaps Δ | hold | force | OOS Δ |",
                "|---------|----------|---------|------|-------|-------|",
            ]
        )
        for c in (payload.get("comparisons") or [])[:8]:
            lines.append(
                f"| {c.get('variant_id')} | {c.get('delta_excess_vs_w20_full_pp')} | "
                f"{c.get('delta_swaps_vs_w20')} | {c.get('mean_hold_days')} | "
                f"{c.get('force_exits')} | {c.get('delta_excess_vs_w20_oos_pp')} |"
            )

    lines.extend(
        [
            "",
            "## Verdict",
            "",
            f"- {verdict.get('summary')}",
            f"- recommend_adopt: **{verdict.get('recommend_adopt')}**",
            "",
        ]
    )
    return "\n".join(lines)
