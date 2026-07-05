"""C18acc · POOL1 kinematic gate research · three tracks vs S2 / POOL1 baseline.

Phase 1: hard Ω′ gate / hybrid top-K / single-day spread pause / intraday hard confirm
Phase 2: soft rank bonus / spread streak pause / intraday tiebreak
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime
from typing import Any, Literal

import pandas as pd

from market_breadth_ma import build_breadth_panel
from market_benchmark import load_benchmark_close
from research.backtest.c18acc_drs_extension_overlay_sweep import (
    build_daily_spread_rs_panel,
)
from research.backtest.c18acc_rotation_force_exit_sweep import (
    _cohort_stats,
    _rotation_cohort_periods,
)
from research.backtest.c18acc_rotation_selective_exit_sweep import (
    rotation_selective_exit_sweep_configs,
)
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_drs5d_score_backtest import build_drs5d_panel
from research.backtest.rrg_hybrid_drs5d_ret5d_backtest import (
    INTRADAY_SAMPLE_FROM,
    attach_daily_dual_wma_spread,
    attach_intraday_extension_flags,
    compute_hybrid_structural_score,
)
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, LENGTH
from research.backtest.rrg_mono_score_swap_c import (
    ChallengerRankBonus,
    EntryGateMode,
    ScoreSwapCConfig,
    close_champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from research.backtest.slot_portfolio_metrics import portfolio_metrics_for_periods
from rrg_rotation import compute_rrg_panel

IS_END_DEFAULT = "2025-12-31"
OOS_START_DEFAULT = "2026-01-01"
HYBRID_TOP_K_DEFAULT = 10
RANK_BONUS_WEIGHT_DEFAULT = 0.05


def build_omega_kinematic_gate_lookup(panel: pd.DataFrame) -> dict[str, set[str]]:
    """Per-day set of sids passing in_pool_omega_kinematic."""
    if panel.empty or "in_pool_omega_kinematic" not in panel.columns:
        return {}
    sub = panel[panel["in_pool_omega_kinematic"]]
    out: dict[str, set[str]] = {}
    for d, grp in sub.groupby("date"):
        out[str(d)] = set(grp["sid"].astype(str))
    return out


def build_hybrid_top_gate_lookup(
    panel: pd.DataFrame,
    *,
    mode: Literal["hybrid_top_decile", "hybrid_top_k"],
    top_k: int = HYBRID_TOP_K_DEFAULT,
) -> dict[str, set[str]]:
    """Per-day top hybrid score within Ω′ · decile or fixed K."""
    if panel.empty or "in_pool_omega_kinematic" not in panel.columns:
        return {}
    kin = panel[panel["in_pool_omega_kinematic"]].copy()
    if "score_hybrid" not in kin.columns:
        kin["score_hybrid"] = kin.apply(compute_hybrid_structural_score, axis=1)
    out: dict[str, set[str]] = {}
    for d, grp in kin.groupby("date"):
        g = grp.dropna(subset=["score_hybrid"]).sort_values("score_hybrid", ascending=False)
        if g.empty:
            out[str(d)] = set()
            continue
        if mode == "hybrid_top_decile":
            n = max(1, len(g) // 10)
        else:
            n = min(max(1, top_k), len(g))
        out[str(d)] = set(g.head(n)["sid"].astype(str))
    return out


def build_intraday_extension_lookup(panel: pd.DataFrame) -> dict[str, set[str]]:
    """Per-day sids with 13:30 imp_extension strict."""
    if panel.empty or "intraday_extension" not in panel.columns:
        return {}
    sub = panel[panel["intraday_extension"] == True]  # noqa: E712
    out: dict[str, set[str]] = {}
    for d, grp in sub.groupby("date"):
        out[str(d)] = set(grp["sid"].astype(str))
    return out


def intraday_gate_coverage(
    gate_lookup: dict[str, set[str]],
    trade_dates: list[str],
) -> dict[str, Any]:
    """Report intraday gate coverage · dates ≥ sample_from only."""
    eligible = [d for d in trade_dates if d >= INTRADAY_SAMPLE_FROM]
    if not eligible:
        return {
            "sample_from": INTRADAY_SAMPLE_FROM,
            "n_trade_dates": len(trade_dates),
            "n_intraday_dates": 0,
            "n_dates_with_gate": 0,
            "mean_gate_size": 0.0,
        }
    sizes = [len(gate_lookup.get(d, set())) for d in eligible]
    n_with = sum(1 for s in sizes if s > 0)
    return {
        "sample_from": INTRADAY_SAMPLE_FROM,
        "n_trade_dates": len(trade_dates),
        "n_intraday_dates": len(eligible),
        "n_dates_with_gate": n_with,
        "mean_gate_size": round(sum(sizes) / len(sizes), 2) if sizes else 0.0,
    }


def build_rank_bonus_lookup(
    panel: pd.DataFrame,
    *,
    score_col: str,
) -> dict[str, dict[str, float]]:
    """Per-day normalized score map for soft challenger rank bonus."""
    if panel.empty or score_col not in panel.columns:
        return {}
    out: dict[str, dict[str, float]] = {}
    for d, grp in panel.groupby("date"):
        scores: dict[str, float] = {}
        for _, row in grp.iterrows():
            v = row.get(score_col)
            if pd.notna(v):
                scores[str(row["sid"])] = float(v)
        out[str(d)] = scores
    return out


def _pool1_base(champ: ScoreSwapCConfig) -> ScoreSwapCConfig:
    return replace(
        champ,
        variant_id="POOL1",
        label="fresh∪accel pool · S2 exit",
        candidate_pool="fresh_union_accel",
        quad_force_exit_mode="weakening",
        quad_force_exit_min_days=2,
        quad_force_exit_loss_pct=-0.05,
    )


def pool1_kinematic_gate_configs() -> list[ScoreSwapCConfig]:
    """Preregistered variants · S2 / POOL1 baselines + three research tracks."""
    s2 = next(c for c in rotation_selective_exit_sweep_configs() if c.variant_id == "S2")
    champ = close_champion_score_swap_c_config()
    pool1 = _pool1_base(champ)

    return [
        replace(s2, variant_id="S2", label="baseline · S2 quad weakening 2d + loss≥5%"),
        pool1,
        replace(
            pool1,
            variant_id="P1-OMEGA",
            label="POOL1 + Ω′ kinematic gate",
            entry_gate_mode="omega_kinematic",
        ),
        replace(
            pool1,
            variant_id="P1-HYB10",
            label="POOL1 + hybrid top decile gate",
            entry_gate_mode="hybrid_top_decile",
        ),
        replace(
            pool1,
            variant_id="P1-HYBK",
            label=f"POOL1 + hybrid top-{HYBRID_TOP_K_DEFAULT} gate",
            entry_gate_mode="hybrid_top_k",
            hybrid_top_k=HYBRID_TOP_K_DEFAULT,
        ),
        replace(
            s2,
            variant_id="S2-SP",
            label="S2 + spread_rs>0 pause quad force-exit",
            quad_force_exit_pause_spread_rs_positive=True,
        ),
        replace(
            pool1,
            variant_id="P1-SP",
            label="POOL1 + spread_rs>0 pause quad force-exit",
            quad_force_exit_pause_spread_rs_positive=True,
        ),
        replace(
            pool1,
            variant_id="P1-IC",
            label="POOL1 + 13:30 intraday extension swap confirm",
            intraday_swap_confirm=True,
        ),
        replace(
            s2,
            variant_id="S2-IC",
            label="S2 + 13:30 intraday extension swap confirm",
            intraday_swap_confirm=True,
        ),
    ]


def pool1_kinematic_gate_phase2_configs() -> list[ScoreSwapCConfig]:
    """Phase 2 · soft rank · spread streak pause · intraday tiebreak."""
    s2 = next(c for c in rotation_selective_exit_sweep_configs() if c.variant_id == "S2")
    pool1 = _pool1_base(close_champion_score_swap_c_config())
    w = RANK_BONUS_WEIGHT_DEFAULT

    return [
        replace(s2, variant_id="S2", label="baseline · S2 quad weakening 2d + loss≥5%"),
        pool1,
        replace(
            pool1,
            variant_id="P1-KIN",
            label=f"POOL1 + kin rank bonus w={w}",
            challenger_rank_bonus="kin",
            challenger_rank_bonus_weight=w,
        ),
        replace(
            pool1,
            variant_id="P1-HYB-B",
            label=f"POOL1 + hybrid rank bonus w={w}",
            challenger_rank_bonus="hybrid",
            challenger_rank_bonus_weight=w,
        ),
        replace(
            pool1,
            variant_id="P1-KIN-S",
            label=f"POOL1 + kin bonus margin-only w={w}",
            challenger_rank_bonus="kin",
            challenger_rank_bonus_weight=w,
            challenger_rank_bonus_margin_only=True,
        ),
        replace(
            pool1,
            variant_id="P1-PAUSE-N2",
            label="POOL1 + spread_rs>0 streak≥2 pause quad force-exit",
            quad_force_exit_pause_spread_rs_streak_min=2,
        ),
        replace(
            pool1,
            variant_id="P1-PAUSE-N3",
            label="POOL1 + spread_rs>0 streak≥3 pause quad force-exit",
            quad_force_exit_pause_spread_rs_streak_min=3,
        ),
        replace(
            s2,
            variant_id="S2-PAUSE-N2",
            label="S2 + spread_rs>0 streak≥2 pause quad force-exit",
            quad_force_exit_pause_spread_rs_streak_min=2,
        ),
        replace(
            pool1,
            variant_id="P1-TB",
            label="POOL1 + 13:30 intraday extension tiebreak",
            intraday_swap_tiebreak=True,
        ),
        replace(
            s2,
            variant_id="S2-TB",
            label="S2 + 13:30 intraday extension tiebreak",
            intraday_swap_tiebreak=True,
        ),
    ]


def _gate_context_for_variant(
    cfg: ScoreSwapCConfig,
    *,
    omega_lookup: dict[str, set[str]],
    hybrid_decile_lookup: dict[str, set[str]],
    hybrid_topk_lookup: dict[str, set[str]],
    intraday_lookup: dict[str, set[str]],
) -> tuple[dict[str, set[str]] | None, dict[str, set[str]] | None, dict[str, set[str]] | None]:
    entry_gate: dict[str, set[str]] | None = None
    swap_gate: dict[str, set[str]] | None = None
    intraday: dict[str, set[str]] | None = None

    mode: EntryGateMode = cfg.entry_gate_mode
    if mode == "omega_kinematic":
        entry_gate = swap_gate = omega_lookup
    elif mode == "hybrid_top_decile":
        entry_gate = swap_gate = hybrid_decile_lookup
    elif mode == "hybrid_top_k":
        entry_gate = swap_gate = hybrid_topk_lookup

    if cfg.intraday_swap_confirm or cfg.intraday_swap_tiebreak:
        intraday = intraday_lookup

    return entry_gate, swap_gate, intraday


def _rank_bonus_context_for_variant(
    cfg: ScoreSwapCConfig,
    *,
    kin_lookup: dict[str, dict[str, float]],
    hybrid_lookup: dict[str, dict[str, float]],
) -> dict[str, dict[str, float]] | None:
    mode: ChallengerRankBonus = cfg.challenger_rank_bonus
    if mode == "kin":
        return kin_lookup
    if mode == "hybrid":
        return hybrid_lookup
    return None


def _calendar_years(date_start: str, date_end: str) -> float:
    d0 = datetime.strptime(date_start, "%Y-%m-%d")
    d1 = datetime.strptime(date_end, "%Y-%m-%d")
    return max((d1 - d0).days / 365.25, 1e-6)


def _run_variant_window(
    conn: sqlite3.Connection,
    *,
    cfg: ScoreSwapCConfig,
    date_start: str,
    date_end: str,
    label: str,
    close: pd.DataFrame,
    bench: pd.Series,
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    spread_rs_panel: pd.DataFrame,
    full_dates: list[str],
    fresh_by_date: dict[str, list],
    mono_by_date: dict[str, list],
    zone_by_date: dict[str, str],
    c0_cfg: ScoreSwapCConfig,
    kbar_cache: dict,
    entry_gate_by_date: dict[str, set[str]] | None,
    swap_gate_by_date: dict[str, set[str]] | None,
    intraday_extension_by_date: dict[str, set[str]] | None,
    challenger_rank_bonus_by_date: dict[str, dict[str, float]] | None,
) -> dict[str, Any]:
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    if len(trade_dates) < 5:
        return {"label": label, "error": "insufficient_trade_dates", "variant_id": cfg.variant_id}

    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=cfg,
        mono_by_date=mono_by_date,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        spread_rs_panel=spread_rs_panel,
        kbar_cache=kbar_cache,
        entry_c_config=c0_cfg,
        entry_gate_by_date=entry_gate_by_date,
        swap_gate_by_date=swap_gate_by_date,
        intraday_extension_by_date=intraday_extension_by_date,
        challenger_rank_bonus_by_date=challenger_rank_bonus_by_date,
    )
    leg_sum = summarize_periods(periods)
    port = portfolio_metrics_for_periods(
        conn,
        periods,
        trade_dates,
        total_capital=30_000.0,
        n_slots=cfg.n_slots,
        close=close,
    )
    rot = _cohort_stats(_rotation_cohort_periods(periods, rs_ratio, rs_mom, full_dates))
    struct_exits = sum(1 for p in periods if p.get("exit_reason") == "struct_force_exit")
    quad_exits = sum(1 for p in periods if p.get("exit_reason") == "quad_force_exit")

    return {
        "label": label,
        "variant_id": cfg.variant_id,
        "config_label": cfg.label,
        "date_start": date_start,
        "date_end": date_end,
        "n_trade_dates": len(trade_dates),
        "calendar_years": round(_calendar_years(date_start, date_end), 3),
        "n_periods": summary.get("n_periods"),
        "mean_excess_pct": summary.get("mean_excess_pct"),
        "mean_return_pct": leg_sum.get("mean_return_pct"),
        "win_rate_vs_bench_pct": leg_sum.get("win_rate_vs_bench_pct"),
        "swaps_total": summary.get("swaps_total"),
        "force_exits": summary.get("force_exits"),
        "quad_force_exits": quad_exits,
        "quad_force_exit_paused": summary.get("quad_force_exit_paused"),
        "intraday_swap_blocked": summary.get("intraday_swap_blocked"),
        "struct_force_exits": struct_exits,
        "rotation_cohort": rot,
        "portfolio": {
            "total_return_pct": port.get("total_return_pct"),
            "cagr_pct": port.get("cagr_pct"),
            "sharpe_ratio": port.get("sharpe_ratio"),
            "max_drawdown_pct": port.get("max_drawdown_pct"),
        },
    }


def run_pool1_kinematic_gate_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-30",
    is_end: str = IS_END_DEFAULT,
    oos_start: str = OOS_START_DEFAULT,
    phase: Literal[1, 2] = 1,
) -> dict[str, Any]:
    configs = (
        pool1_kinematic_gate_phase2_configs() if phase == 2 else pool1_kinematic_gate_configs()
    )
    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    spread_rs_panel = build_daily_spread_rs_panel(close, bench)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    kbar_cache: dict = {}

    drs_panel, drs_meta = build_drs5d_panel(conn)
    drs_panel = attach_daily_dual_wma_spread(drs_panel, conn)
    drs_panel["score_hybrid"] = drs_panel.apply(compute_hybrid_structural_score, axis=1)
    kin_rank_lookup = build_rank_bonus_lookup(drs_panel, score_col="drs5d_score_kin")
    hybrid_rank_lookup = build_rank_bonus_lookup(drs_panel, score_col="score_hybrid")
    omega_lookup = build_omega_kinematic_gate_lookup(drs_panel)
    hybrid_decile_lookup = build_hybrid_top_gate_lookup(
        drs_panel, mode="hybrid_top_decile"
    )
    hybrid_topk_lookup = build_hybrid_top_gate_lookup(
        drs_panel, mode="hybrid_top_k", top_k=HYBRID_TOP_K_DEFAULT
    )
    drs_intraday = attach_intraday_extension_flags(drs_panel, conn)
    intraday_lookup = build_intraday_extension_lookup(drs_intraday)
    intraday_meta = dict(drs_intraday.attrs.get("intraday_meta", {}))
    intraday_coverage = intraday_gate_coverage(intraday_lookup, trade_dates)

    windows = [
        ("IS", date_start, is_end),
        ("OOS", oos_start, date_end),
        ("FULL", date_start, date_end),
    ]

    rows: list[dict[str, Any]] = []
    for cfg in configs:
        entry_gate, swap_gate, intraday_gate = _gate_context_for_variant(
            cfg,
            omega_lookup=omega_lookup,
            hybrid_decile_lookup=hybrid_decile_lookup,
            hybrid_topk_lookup=hybrid_topk_lookup,
            intraday_lookup=intraday_lookup,
        )
        rank_bonus = _rank_bonus_context_for_variant(
            cfg,
            kin_lookup=kin_rank_lookup,
            hybrid_lookup=hybrid_rank_lookup,
        )
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
                spread_rs_panel=spread_rs_panel,
                full_dates=full_dates,
                fresh_by_date=fresh_by_date,
                mono_by_date=mono_by_date,
                zone_by_date=zone_by_date,
                c0_cfg=c0_cfg,
                kbar_cache=kbar_cache,
                entry_gate_by_date=entry_gate,
                swap_gate_by_date=swap_gate,
                intraday_extension_by_date=intraday_gate,
                challenger_rank_bonus_by_date=rank_bonus,
            )
            rows.append(row)

    by_variant: dict[str, dict[str, Any]] = {}
    for r in rows:
        vid = str(r.get("variant_id", ""))
        by_variant.setdefault(vid, {})[str(r.get("label", ""))] = r

    s2_full = by_variant.get("S2", {}).get("FULL", {})
    s2_oos = by_variant.get("S2", {}).get("OOS", {})
    pool1_full = by_variant.get("POOL1", {}).get("FULL", {})
    pool1_oos = by_variant.get("POOL1", {}).get("OOS", {})

    comparisons: list[dict[str, Any]] = []
    for cfg in configs:
        vid = cfg.variant_id
        if vid in ("S2", "POOL1"):
            continue
        full = by_variant.get(vid, {}).get("FULL", {})
        oos = by_variant.get(vid, {}).get("OOS", {})
        comparisons.append(
            {
                "variant_id": vid,
                "label": cfg.label,
                "track": _variant_track(vid, phase=phase),
                "delta_excess_vs_s2_full_pp": round(
                    float(full.get("mean_excess_pct") or 0)
                    - float(s2_full.get("mean_excess_pct") or 0),
                    3,
                ),
                "delta_excess_vs_s2_oos_pp": round(
                    float(oos.get("mean_excess_pct") or 0) - float(s2_oos.get("mean_excess_pct") or 0),
                    3,
                ),
                "delta_excess_vs_pool1_full_pp": round(
                    float(full.get("mean_excess_pct") or 0)
                    - float(pool1_full.get("mean_excess_pct") or 0),
                    3,
                ),
                "delta_excess_vs_pool1_oos_pp": round(
                    float(oos.get("mean_excess_pct") or 0)
                    - float(pool1_oos.get("mean_excess_pct") or 0),
                    3,
                ),
                "delta_swaps_vs_s2_full": int(full.get("swaps_total") or 0)
                - int(s2_full.get("swaps_total") or 0),
                "delta_swaps_vs_pool1_full": int(full.get("swaps_total") or 0)
                - int(pool1_full.get("swaps_total") or 0),
                "delta_cagr_vs_s2_full_pp": round(
                    float((full.get("portfolio") or {}).get("cagr_pct") or 0)
                    - float((s2_full.get("portfolio") or {}).get("cagr_pct") or 0),
                    2,
                ),
                "full": full,
                "oos": oos,
            }
        )

    comparisons.sort(
        key=lambda x: (
            float(x.get("delta_excess_vs_pool1_oos_pp") or 0),
            float(x.get("delta_swaps_vs_pool1_full") or 0),
        ),
        reverse=True,
    )

    beat_pool1_oos = [
        c
        for c in comparisons
        if float(c.get("delta_excess_vs_pool1_oos_pp") or 0) > 0
        and not (c.get("oos") or {}).get("error")
    ]
    beat_s2_oos = [
        c
        for c in comparisons
        if float(c.get("delta_excess_vs_s2_oos_pp") or 0) > 0
        and not (c.get("oos") or {}).get("error")
    ]

    schema = "c18acc_pool1_kinematic_gate-v2" if phase == 2 else "c18acc_pool1_kinematic_gate-v1"
    hypotheses = (
        {
            "H-RANK-1": "kin rank bonus raises swaps without OOS excess collapse vs POOL1",
            "H-RANK-2": "hybrid rank bonus improves challenger quality vs POOL1 on OOS",
            "H-RANK-3": "margin-only kin bonus is a safe tiebreak vs full kin bonus",
            "H-PAUSE-2": "spread_rs streak≥2 pause reduces premature quad force-exits · OOS ≥ S2",
            "H-PAUSE-3": "spread_rs streak≥3 pause is stricter · fewer false pauses",
            "H-TB-1": "13:30 intraday tiebreak improves swap quality vs POOL1 (no hard block)",
        }
        if phase == 2
        else {
            "H-GATE-1": "Ω′ kinematic gate raises swaps without OOS excess collapse vs POOL1",
            "H-GATE-2": "hybrid top-K gate improves challenger quality vs POOL1 on OOS",
            "H-PAUSE-1": "spread_rs>0 pause reduces premature quad force-exits · OOS ≥ S2",
            "H-IC-1": "13:30 intraday extension confirm improves swap quality vs POOL1",
        }
    )

    return {
        "schema": schema,
        "topic": "c18acc-pool1-kinematic-gate",
        "phase": phase,
        "baseline_variants": ["S2", "POOL1"],
        "date_start": date_start,
        "date_end": date_end,
        "is_end": is_end,
        "oos_start": oos_start,
        "gate_meta": {
            "omega_kinematic_n": drs_meta.get("omega_kinematic_n"),
            "hybrid_top_k": HYBRID_TOP_K_DEFAULT,
            "rank_bonus_weight": RANK_BONUS_WEIGHT_DEFAULT,
            "intraday": intraday_meta,
            "intraday_coverage": intraday_coverage,
        },
        "hypotheses": hypotheses,
        "variants": by_variant,
        "comparisons": comparisons,
        "verdict": {
            "best_oos_vs_pool1": comparisons[0].get("variant_id") if comparisons else None,
            "n_beat_pool1_oos": len(beat_pool1_oos),
            "beat_pool1_oos_ids": [c["variant_id"] for c in beat_pool1_oos],
            "n_beat_s2_oos": len(beat_s2_oos),
            "beat_s2_oos_ids": [c["variant_id"] for c in beat_s2_oos],
            "s2_full_excess": s2_full.get("mean_excess_pct"),
            "s2_oos_excess": s2_oos.get("mean_excess_pct"),
            "pool1_full_excess": pool1_full.get("mean_excess_pct"),
            "pool1_oos_excess": pool1_oos.get("mean_excess_pct"),
            "summary": _verdict_summary(
                s2_full,
                s2_oos,
                pool1_full,
                pool1_oos,
                beat_pool1_oos,
                beat_s2_oos,
                comparisons[0] if comparisons else None,
            ),
        },
    }


def _variant_track(variant_id: str, *, phase: int = 1) -> str:
    if phase == 2:
        if variant_id in ("P1-KIN", "P1-HYB-B", "P1-KIN-S"):
            return "soft_rank"
        if variant_id.endswith("-N2") or variant_id.endswith("-N3"):
            return "spread_streak_pause"
        if variant_id.endswith("-TB"):
            return "intraday_tiebreak"
        return "baseline"
    if variant_id.startswith("P1-OMEGA") or variant_id.startswith("P1-HYB"):
        return "gate"
    if variant_id.endswith("-SP"):
        return "spread_pause"
    if variant_id.endswith("-IC"):
        return "intraday_confirm"
    return "baseline"


def _verdict_summary(
    s2_full: dict[str, Any],
    s2_oos: dict[str, Any],
    pool1_full: dict[str, Any],
    pool1_oos: dict[str, Any],
    beat_pool1_oos: list[dict[str, Any]],
    beat_s2_oos: list[dict[str, Any]],
    best: dict[str, Any] | None,
) -> str:
    parts = [
        f"S2 FULL excess={s2_full.get('mean_excess_pct')}% · OOS={s2_oos.get('mean_excess_pct')}%",
        f"POOL1 FULL excess={pool1_full.get('mean_excess_pct')}% · OOS={pool1_oos.get('mean_excess_pct')}%",
        f"POOL1 Δswaps vs S2={int(pool1_full.get('swaps_total') or 0) - int(s2_full.get('swaps_total') or 0)}",
    ]
    if beat_pool1_oos:
        parts.append(f"{len(beat_pool1_oos)} variant(s) beat POOL1 OOS excess")
    else:
        parts.append("no variant beat POOL1 OOS excess")
    if beat_s2_oos:
        parts.append(f"{len(beat_s2_oos)} beat S2 OOS")
    if best:
        parts.append(
            f"best vs POOL1 OOS={best.get('variant_id')} "
            f"Δ={best.get('delta_excess_vs_pool1_oos_pp')}pp "
            f"Δswaps={best.get('delta_swaps_vs_pool1_full')}"
        )
    return " · ".join(parts)


def render_pool1_kinematic_gate_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict", {})
    gate = payload.get("gate_meta") or {}
    cov = gate.get("intraday_coverage") or {}
    phase = int(payload.get("phase") or 1)
    if phase == 2:
        subtitle = "> soft rank bonus · spread streak pause · 13:30 tiebreak"
        ic_col = "ic blocked"
    else:
        subtitle = "> Ω′ / hybrid gate · spread pause · 13:30 swap confirm"
        ic_col = "ic blocked"
    lines = [
        f"# C18acc · POOL1 kinematic gate sweep{' v2' if phase == 2 else ''}",
        "",
        subtitle,
        "",
        f"- Span: **{payload.get('date_start')} → {payload.get('date_end')}**",
        f"- IS ≤ {payload.get('is_end')} · OOS ≥ {payload.get('oos_start')}",
        f"- Baselines: **S2** · **POOL1**",
        "",
        "## Verdict",
        "",
        f"- {v.get('summary', '')}",
        "",
        "## Intraday coverage",
        "",
        f"- sample_from={cov.get('sample_from')} · intraday_dates={cov.get('n_intraday_dates')} · "
        f"dates_with_gate={cov.get('n_dates_with_gate')} · mean_gate_size={cov.get('mean_gate_size')}",
        "",
        "## vs POOL1 / S2 (FULL / OOS)",
        "",
        "| variant | track | Δ excess FULL (POOL1) | Δ excess OOS (POOL1) | Δ swaps (POOL1) | "
        f"Δ excess OOS (S2) | quad paused | {ic_col} |",
        "|---------|-------|----------------------|----------------------|-----------------|"
        f"-----------------|-------------|------------|",
    ]
    for c in payload.get("comparisons", []):
        full = c.get("full") or {}
        lines.append(
            f"| {c.get('variant_id')} | {c.get('track')} | "
            f"{c.get('delta_excess_vs_pool1_full_pp'):+.3f}pp | "
            f"{c.get('delta_excess_vs_pool1_oos_pp'):+.3f}pp | "
            f"{c.get('delta_swaps_vs_pool1_full'):+d} | "
            f"{c.get('delta_excess_vs_s2_oos_pp'):+.3f}pp | "
            f"{full.get('quad_force_exit_paused', 0)} | {full.get('intraday_swap_blocked', 0)} |"
        )

    lines.extend(["", "## Baseline reference", ""])
    for vid in ("S2", "POOL1"):
        block = (payload.get("variants") or {}).get(vid, {})
        full = block.get("FULL", {})
        oos = block.get("OOS", {})
        if full:
            port = full.get("portfolio") or {}
            lines.append(
                f"- **{vid} FULL**: excess={full.get('mean_excess_pct')}% · "
                f"swaps={full.get('swaps_total')} · CAGR={port.get('cagr_pct')}% · "
                f"force={full.get('force_exits')}"
            )
        if oos:
            lines.append(
                f"- **{vid} OOS**: excess={oos.get('mean_excess_pct')}% · "
                f"swaps={oos.get('swaps_total')} · force={oos.get('force_exits')}"
            )

    lines.extend(["", "## Hypotheses", ""])
    for hid, stmt in (payload.get("hypotheses") or {}).items():
        lines.append(f"- **{hid}**: {stmt}")

    return "\n".join(lines)
