"""C18acc · daily RRG Weakening quadrant stop-loss overlay sweep.

Post-process close champion periods with daily close RRG quad exit rules.
Compare vs I0 (C18acc only) and I36 (extension overlay).
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

from analytics.bench import bench_return_entry_to_exit
from market_benchmark import load_benchmark_close
from research.backtest.c18acc_extension_exit import _hold_dates
from research.backtest.c18acc_intraday_1m_hold import (
    BLEED_CASE_STUDY_IDS,
    BLEED_SWEEP_CONTROLS,
    Intraday1mHoldConfig,
    _identify_bleed_cohort,
    _leg_key,
    _percentile,
    _worst_intrahold_mtm,
    apply_intraday_1m_hold,
    bootstrap_paired_mean,
)
from research.backtest.c18acc_swap_block_audit import _stock_end_q
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, LENGTH
from research.backtest.rrg_mono_intraday_exit import _trading_days_between
from research.backtest.rrg_mono_score_swap_c import (
    _quad_streak_days,
    close_champion_score_swap_c_config,
    simulate_score_swap_c,
)
from rrg_rotation import compute_rrg_panel

DEFAULT_EXIT_QUADRANTS = ("weakening", "lagging")

HYPOTHESES_RRG_WK: dict[str, str] = {
    "H-RRG-WK-A1": "Δ vs I0 · paired 95% CI 下界 ≥ −0.3pp",
    "H-RRG-WK-A2": "Δ vs I36 · paired 95% CI 下界 ≥ −0.3pp",
    "H-RRG-WK-B1": "max_hold 腿 final ret≤−8% · 較 I36 減少 ≥30%",
    "H-RRG-WK-B2": "P5 intrahold MTM · 較 I36 改善 ≥2pp",
    "H-RRG-WK-C1": "C_bleed cohort · quad_stop 覆蓋率 ≥ I36 ext +10pp",
    "H-RRG-WK-C2": "C_bleed quad_stop 腿 · median save_vs_orig > 0",
    "H-RRG-WK-C3": "C_pure_drip · median save_vs_orig > 0（extension 未觸發）",
    "H-RRG-WK-D1": "W6 stacked · 同時通過 H-RRG-WK-A2 與 H-RRG-WK-B1",
}


@dataclass(frozen=True)
class DailyQuadExitConfig:
    variant_id: str
    label: str
    exit_quadrants: tuple[str, ...] = ("weakening",)
    consecutive_days: int = 1
    min_hold_before_quad_exit: int = 5
    mtm_threshold_pct: float | None = None
    stack_extension: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["exit_quadrants"] = list(self.exit_quadrants)
        return d


def _quad_streak_in_set(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    *,
    as_of: str,
    stock_id: str,
    entry_date: str,
    quads: tuple[str, ...],
) -> int:
    if len(quads) == 1:
        return _quad_streak_days(
            rs_ratio,
            rs_mom,
            full_dates,
            as_of=as_of,
            stock_id=stock_id,
            target_quad=quads[0],
            entry_date=entry_date,
        )
    if as_of not in full_dates or entry_date not in full_dates:
        return 0
    tgt = {q.lower() for q in quads}
    streak = 0
    si = full_dates.index(as_of)
    ei = full_dates.index(entry_date)
    for j in range(si, ei - 1, -1):
        d = full_dates[j]
        q = _stock_end_q(rs_ratio, rs_mom, full_dates, d, stock_id)
        if q and q.lower() in tgt:
            streak += 1
        else:
            break
    return streak


def apply_daily_quad_exit_overlay(
    conn: sqlite3.Connection,
    *,
    base_periods: list[dict[str, Any]],
    full_dates: list[str],
    close: pd.DataFrame,
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    config: DailyQuadExitConfig,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Daily close RRG quadrant stop on held legs (after min_hold_before_quad_exit)."""
    out: list[dict[str, Any]] = []
    triggers = 0
    save_deltas: list[float] = []
    quads = tuple(q.lower() for q in config.exit_quadrants)

    for pos in base_periods:
        sid = str(pos["stock_id"])
        entry = str(pos["entry_date"])
        orig_exit = str(pos["exit_date"])
        entry_px = float(pos["entry_px"])
        orig_exit_px = float(pos["exit_px"])
        orig_ret = float(pos["return_pct"])

        early_exit_d: str | None = None
        early_px: float | None = None

        for d in _hold_dates(full_dates, entry, orig_exit):
            held = _trading_days_between(full_dates, entry, d)
            if held < config.min_hold_before_quad_exit:
                continue
            streak = _quad_streak_in_set(
                rs_ratio,
                rs_mom,
                full_dates,
                as_of=d,
                stock_id=sid,
                entry_date=entry,
                quads=quads,
            )
            if streak < config.consecutive_days:
                continue
            if config.mtm_threshold_pct is not None:
                if d not in close.index or sid not in close.columns:
                    continue
                px_chk = float(close.at[d, sid])
                if px_chk <= 0:
                    continue
                mtm = (px_chk / entry_px - 1.0) * 100.0
                if mtm > config.mtm_threshold_pct:
                    continue
            if d not in close.index or sid not in close.columns:
                continue
            px = float(close.at[d, sid])
            if px != px or px <= 0:
                continue
            early_exit_d = d
            early_px = px
            triggers += 1
            break

        if early_exit_d and early_px:
            ret = (early_px / entry_px - 1.0) * 100.0
            bench = bench_return_entry_to_exit(conn, entry, early_exit_d, entry_price_mode="close")
            if bench is None:
                out.append(dict(pos))
                continue
            save = (orig_exit_px / entry_px - 1.0) * 100.0 - ret
            save_deltas.append(save)
            leg = dict(pos)
            leg.update(
                {
                    "exit_date": early_exit_d,
                    "exit_px": round(early_px, 4),
                    "exit_reason": f"quad_{'+'.join(quads)}",
                    "hold_days": _trading_days_between(full_dates, entry, early_exit_d),
                    "return_pct": round(ret, 4),
                    "bench_return_pct": round(bench, 4),
                    "excess_pct": round(ret - bench, 4),
                    "beat_bench": ret > bench,
                    "gross_win": ret > 0,
                    "overlay_variant": config.variant_id,
                    "orig_exit_date": orig_exit,
                    "orig_exit_px": orig_exit_px,
                    "orig_return_pct": orig_ret,
                    "save_vs_orig_pct": round(save, 4),
                    "quad_exit": True,
                }
            )
            out.append(leg)
        else:
            out.append(dict(pos))

    summary = summarize_periods(out)
    if summary.get("mean_return_pct") is not None and summary.get("mean_bench_pct") is not None:
        summary["mean_excess_pct"] = round(
            float(summary["mean_return_pct"]) - float(summary["mean_bench_pct"]), 4
        )
    summary.update(
        {
            "variant_id": config.variant_id,
            "label": config.label,
            "exit_mode": f"quad_{'+'.join(quads)}",
            "quad_exits": triggers,
            "median_save_vs_orig_pct": round(float(pd.Series(save_deltas).median()), 4)
            if save_deltas
            else None,
        }
    )
    return out, summary


def _pick_earliest_exit(
    base: dict[str, Any],
    *variants: dict[str, Any],
) -> dict[str, Any]:
    """Pick leg with earliest exit among base and overlay variants."""
    orig_exit = str(base["exit_date"])

    def _sort_key(leg: dict[str, Any]) -> tuple[str, int]:
        d = str(leg["exit_date"])
        intraday = 0 if leg.get("exit_minute") else 1
        return (d, intraday)

    candidates = [base]
    for v in variants:
        if str(v["exit_date"]) < orig_exit:
            candidates.append(v)
        elif str(v["exit_date"]) == orig_exit and v.get("exit_minute"):
            candidates.append(v)
    return min(candidates, key=_sort_key)


def _merge_rrg_extension_stack(
    base_periods: list[dict[str, Any]],
    rrg_periods: list[dict[str, Any]],
    ext_periods: list[dict[str, Any]],
    *,
    variant_id: str,
    label: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    out: list[dict[str, Any]] = []
    quad_triggers = 0
    ext_triggers = 0
    save_deltas: list[float] = []

    for base, rrg, ext in zip(base_periods, rrg_periods, ext_periods):
        picked = _pick_earliest_exit(base, rrg, ext)
        leg = dict(picked)
        leg["variant_id"] = variant_id
        leg["overlay_variant"] = variant_id
        if picked.get("quad_exit"):
            quad_triggers += 1
        if picked.get("ext_exit"):
            ext_triggers += 1
        if picked.get("save_vs_orig_pct") is not None:
            save_deltas.append(float(picked["save_vs_orig_pct"]))
        out.append(leg)

    summary = summarize_periods(out)
    if summary.get("mean_return_pct") is not None and summary.get("mean_bench_pct") is not None:
        summary["mean_excess_pct"] = round(
            float(summary["mean_return_pct"]) - float(summary["mean_bench_pct"]), 4
        )
    summary.update(
        {
            "variant_id": variant_id,
            "label": label,
            "exit_mode": "quad+ext_stack",
            "quad_exits": quad_triggers,
            "ext_exits": ext_triggers,
            "median_save_vs_orig_pct": round(statistics.median(save_deltas), 4)
            if save_deltas
            else None,
        }
    )
    return out, summary


def rrg_weakening_stop_grid() -> list[DailyQuadExitConfig]:
    """Preregistered treatment arms (6) + controls applied separately."""
    return [
        DailyQuadExitConfig(
            "W1",
            "weakening 1d · min_hold_before=5",
            exit_quadrants=("weakening",),
            consecutive_days=1,
            min_hold_before_quad_exit=5,
        ),
        DailyQuadExitConfig(
            "W2",
            "weakening 2d consecutive · min_hold_before=5",
            exit_quadrants=("weakening",),
            consecutive_days=2,
            min_hold_before_quad_exit=5,
        ),
        DailyQuadExitConfig(
            "W3",
            "weakening|lagging 1d · min_hold_before=5",
            exit_quadrants=("weakening", "lagging"),
            consecutive_days=1,
            min_hold_before_quad_exit=5,
        ),
        DailyQuadExitConfig(
            "W4",
            "weakening 1d · min_hold_before=0",
            exit_quadrants=("weakening",),
            consecutive_days=1,
            min_hold_before_quad_exit=0,
        ),
        DailyQuadExitConfig(
            "W5",
            "weakening 1d · MTM≤−5% · min_hold_before=5",
            exit_quadrants=("weakening",),
            consecutive_days=1,
            min_hold_before_quad_exit=5,
            mtm_threshold_pct=-5.0,
        ),
        DailyQuadExitConfig(
            "W6",
            "weakening 2d + I36 extension stack",
            exit_quadrants=("weakening",),
            consecutive_days=2,
            min_hold_before_quad_exit=5,
            stack_extension=True,
        ),
    ]


def _paired_delta_vs_ref(
    ref_periods: list[dict[str, Any]],
    var_periods: list[dict[str, Any]],
) -> list[float]:
    deltas: list[float] = []
    for ref, var in zip(ref_periods, var_periods):
        re, ve = ref.get("excess_pct"), var.get("excess_pct")
        if re is None or ve is None:
            continue
        deltas.append(float(ve) - float(re))
    return deltas


def _identify_pure_drip_cohort(
    bleed_cohort: set[tuple[str, str]],
    i36_periods: list[dict[str, Any]],
) -> set[tuple[str, str]]:
    """C_pure_drip = C_bleed legs where I36 extension did not fire."""
    missed: set[tuple[str, str]] = set()
    for p in i36_periods:
        key = _leg_key(p)
        if key in bleed_cohort and not p.get("ext_exit"):
            missed.add(key)
    return missed


def _wk_variant_metrics(
    periods: list[dict[str, Any]],
    *,
    bleed_cohort: set[tuple[str, str]],
    pure_drip_cohort: set[tuple[str, str]],
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    full_dates: list[str],
    kbar_cache: dict | None = None,
) -> dict[str, Any]:
    mtm_vals: list[float] = []
    max_hold_deep = 0
    max_hold_total = 0
    bleed_quad = 0
    bleed_total = len(bleed_cohort)
    bleed_saves: list[float] = []
    drip_saves: list[float] = []

    for pos in periods:
        key = _leg_key(pos)
        entry_px = float(pos["entry_px"])
        worst = _worst_intrahold_mtm(
            conn,
            close,
            full_dates,
            stock_id=key[0],
            entry_date=key[1],
            exit_date=str(pos["exit_date"]),
            entry_px=entry_px,
            kbar_cache=kbar_cache,
        )
        if worst is not None:
            mtm_vals.append(worst)
        reason = str(pos.get("exit_reason") or "")
        ret = float(pos.get("return_pct") or 0)
        if reason in ("max_hold", "max_hold_fallback"):
            max_hold_total += 1
            if ret <= -8.0:
                max_hold_deep += 1
        if key in bleed_cohort:
            triggered = bool(pos.get("quad_exit"))
            if triggered:
                bleed_quad += 1
                sv = pos.get("save_vs_orig_pct")
                if sv is not None:
                    bleed_saves.append(float(sv))
        if key in pure_drip_cohort and pos.get("quad_exit"):
            sv = pos.get("save_vs_orig_pct")
            if sv is not None:
                drip_saves.append(float(sv))

    return {
        "p5_intrahold_mtm_pct": _percentile(mtm_vals, 5),
        "max_hold_deep_loss_n": max_hold_deep,
        "max_hold_n": max_hold_total,
        "max_hold_deep_loss_pct": round(max_hold_deep / max_hold_total * 100, 2)
        if max_hold_total
        else None,
        "bleed_cohort_quad_n": bleed_quad,
        "bleed_cohort_n": bleed_total,
        "bleed_cohort_quad_pct": round(bleed_quad / bleed_total * 100, 2) if bleed_total else None,
        "bleed_cohort_median_save_pct": round(statistics.median(bleed_saves), 4)
        if bleed_saves
        else None,
        "pure_drip_n": len(pure_drip_cohort),
        "pure_drip_quad_n": len(drip_saves),
        "pure_drip_median_save_pct": round(statistics.median(drip_saves), 4) if drip_saves else None,
    }


def _evaluate_rrg_wk_hypotheses(
    *,
    summaries: dict[str, dict[str, Any]],
    periods_by_id: dict[str, list[dict[str, Any]]],
    bleed_cohort: set[tuple[str, str]],
    pure_drip_cohort: set[tuple[str, str]],
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    full_dates: list[str],
    kbar_cache: dict,
) -> dict[str, dict[str, Any]]:
    i0_periods = periods_by_id.get("I0", [])
    i36_periods = periods_by_id.get("I36", [])
    i36_m = _wk_variant_metrics(
        i36_periods,
        bleed_cohort=bleed_cohort,
        pure_drip_cohort=pure_drip_cohort,
        conn=conn,
        close=close,
        full_dates=full_dates,
        kbar_cache=kbar_cache,
    )
    i36_bleed_ext_n = sum(
        1 for p in i36_periods if _leg_key(p) in bleed_cohort and p.get("ext_exit")
    )
    i36_ext_pct = round(i36_bleed_ext_n / len(bleed_cohort) * 100, 2) if bleed_cohort else 0.0
    i36_deep = int(i36_m.get("max_hold_deep_loss_n") or 0)
    i36_p5 = float(i36_m.get("p5_intrahold_mtm_pct") or -999)
    i36_bleed_med = float(i36_m.get("bleed_cohort_median_save_pct") or -999)

    per_variant: dict[str, dict[str, Any]] = {}
    for vid, s in summaries.items():
        if vid in ("I0",):
            continue
        periods = periods_by_id.get(vid, [])
        wm = _wk_variant_metrics(
            periods,
            bleed_cohort=bleed_cohort,
            pure_drip_cohort=pure_drip_cohort,
            conn=conn,
            close=close,
            full_dates=full_dates,
            kbar_cache=kbar_cache,
        )
        paired_i0 = bootstrap_paired_mean(_paired_delta_vs_ref(i0_periods, periods))
        paired_i36 = bootstrap_paired_mean(_paired_delta_vs_ref(i36_periods, periods))
        deep_n = int(wm.get("max_hold_deep_loss_n") or 0)
        deep_reduction = round((1.0 - deep_n / i36_deep) * 100, 2) if i36_deep > 0 else None
        p5 = wm.get("p5_intrahold_mtm_pct")
        p5_improve = round(float(p5) - i36_p5, 4) if p5 is not None else None
        quad_pct = float(wm.get("bleed_cohort_quad_pct") or 0)
        bleed_med = wm.get("bleed_cohort_median_save_pct")
        drip_med = wm.get("pure_drip_median_save_pct")

        a1_pass = paired_i0.get("ci95_lo") is not None and float(paired_i0["ci95_lo"]) >= -0.3
        a2_pass = paired_i36.get("ci95_lo") is not None and float(paired_i36["ci95_lo"]) >= -0.3
        b1_pass = deep_reduction is not None and deep_reduction >= 30.0
        b2_pass = p5_improve is not None and p5_improve >= 2.0
        c1_pass = (quad_pct - i36_ext_pct) >= 10.0
        c2_pass = bleed_med is not None and float(bleed_med) > 0
        c3_pass = drip_med is not None and float(drip_med) > 0
        d1_pass = a2_pass and b1_pass if vid == "W6" else None

        per_variant[vid] = {
            "delta_vs_i0_pp": s.get("delta_vs_i0_pp"),
            "delta_vs_i36_pp": s.get("delta_vs_i36_pp"),
            "paired_vs_i0": paired_i0,
            "paired_vs_i36": paired_i36,
            "wk_metrics": wm,
            "max_hold_deep_reduction_pct": deep_reduction,
            "p5_mtm_improve_pp": p5_improve,
            "bleed_quad_pct_delta_pp": round(quad_pct - i36_ext_pct, 2),
            "hypotheses": {
                "H-RRG-WK-A1": a1_pass,
                "H-RRG-WK-A2": a2_pass,
                "H-RRG-WK-B1": b1_pass,
                "H-RRG-WK-B2": b2_pass,
                "H-RRG-WK-C1": c1_pass,
                "H-RRG-WK-C2": c2_pass,
                "H-RRG-WK-C3": c3_pass,
                "H-RRG-WK-D1": d1_pass,
            },
        }

    return {
        "I36_baseline_wk": i36_m,
        "I36_bleed_ext_pct": i36_ext_pct,
        "pure_drip_cohort_n": len(pure_drip_cohort),
        "variants": per_variant,
    }


def _wk_case_studies(
    periods_by_id: dict[str, list[dict[str, Any]]],
    *,
    stock_ids: tuple[str, ...] = BLEED_CASE_STUDY_IDS,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sid in stock_ids:
        row: dict[str, Any] = {"stock_id": sid, "legs": []}
        for vid in ("I0", "I36", "W2", "W6"):
            for p in periods_by_id.get(vid, []):
                if str(p.get("stock_id")) != sid:
                    continue
                row["legs"].append(
                    {
                        "variant_id": vid,
                        "entry_date": p.get("entry_date"),
                        "exit_date": p.get("exit_date"),
                        "exit_reason": p.get("exit_reason"),
                        "return_pct": p.get("return_pct"),
                        "save_vs_orig_pct": p.get("save_vs_orig_pct"),
                        "quad_exit": p.get("quad_exit"),
                        "ext_exit": p.get("ext_exit"),
                    }
                )
        out.append(row)
    return out


def _run_window(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    label: str,
    close_cfg: Any,
    i36_cfg: Intraday1mHoldConfig,
    grid: list[DailyQuadExitConfig],
) -> dict[str, Any]:
    from market_breadth_ma import build_breadth_panel

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")

    base_periods, i0_summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=close_cfg,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        entry_c_config=c0_cfg,
    )
    i0_summary = dict(i0_summary)
    i0_summary["variant_id"] = "I0"
    i0_summary["label"] = "C18acc-close 基線"
    if i0_summary.get("mean_return_pct") is not None and i0_summary.get("mean_bench_pct") is not None:
        i0_summary["mean_excess_pct"] = round(
            float(i0_summary["mean_return_pct"]) - float(i0_summary["mean_bench_pct"]), 4
        )
    i0_summary["quad_exits"] = 0
    i0_summary["ext_exits"] = 0

    kbar_cache: dict = {}
    ma_cache: dict = {}
    i36_periods, i36_summary = apply_intraday_1m_hold(
        conn,
        base_periods=base_periods,
        close=close,
        full_dates=full_dates,
        config=i36_cfg,
        kbar_cache=kbar_cache,
        ma20_cache=ma_cache,
    )

    summaries: list[dict[str, Any]] = [i0_summary, i36_summary]
    periods_by_id: dict[str, list[dict[str, Any]]] = {
        "I0": base_periods,
        "I36": i36_periods,
    }

    i0_ex = i0_summary.get("mean_excess_pct")
    i36_ex = i36_summary.get("mean_excess_pct")
    i36_summary["delta_vs_i0_pp"] = (
        round(float(i36_summary["mean_excess_pct"]) - float(i0_ex), 4)
        if i36_ex is not None and i0_ex is not None
        else None
    )
    i36_summary["delta_vs_i36_pp"] = 0.0

    for cfg in grid:
        rrg_periods, rrg_summary = apply_daily_quad_exit_overlay(
            conn,
            base_periods=base_periods,
            full_dates=full_dates,
            close=close,
            rs_ratio=rs_ratio,
            rs_mom=rs_mom,
            config=cfg,
        )
        if cfg.stack_extension:
            periods, summary = _merge_rrg_extension_stack(
                base_periods,
                rrg_periods,
                i36_periods,
                variant_id=cfg.variant_id,
                label=cfg.label,
            )
        else:
            periods, summary = rrg_periods, rrg_summary

        if i0_ex is not None and summary.get("mean_excess_pct") is not None:
            summary["delta_vs_i0_pp"] = round(
                float(summary["mean_excess_pct"]) - float(i0_ex), 4
            )
        if i36_ex is not None and summary.get("mean_excess_pct") is not None:
            summary["delta_vs_i36_pp"] = round(
                float(summary["mean_excess_pct"]) - float(i36_ex), 4
            )
        summaries.append(summary)
        periods_by_id[cfg.variant_id] = periods

    bleed_cohort = _identify_bleed_cohort(
        conn,
        base_periods,
        close,
        full_dates,
        rs_ratio,
        rs_mom,
        kbar_cache=kbar_cache,
    )
    pure_drip = _identify_pure_drip_cohort(bleed_cohort, i36_periods)
    hypo = _evaluate_rrg_wk_hypotheses(
        summaries={str(s["variant_id"]): s for s in summaries},
        periods_by_id=periods_by_id,
        bleed_cohort=bleed_cohort,
        pure_drip_cohort=pure_drip,
        conn=conn,
        close=close,
        full_dates=full_dates,
        kbar_cache=kbar_cache,
    )

    return {
        "label": label,
        "date_start": date_start,
        "date_end": date_end,
        "bleed_cohort_n": len(bleed_cohort),
        "pure_drip_cohort_n": len(pure_drip),
        "summaries": summaries,
        "hypothesis_results": hypo,
        "case_studies": _wk_case_studies(periods_by_id),
    }


def run_rrg_weakening_stop_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-30",
    is_end: str = "2025-12-31",
    oos_start: str = "2026-01-01",
) -> dict[str, Any]:
    """C18acc-close × daily RRG Weakening stop · IS/OOS/FULL."""
    close_cfg = close_champion_score_swap_c_config()
    i36_cfg = next(c for c in BLEED_SWEEP_CONTROLS if c.variant_id == "I36")
    grid = rrg_weakening_stop_grid()

    is_result = _run_window(
        conn, date_start=date_start, date_end=is_end, label="IS", close_cfg=close_cfg, i36_cfg=i36_cfg, grid=grid
    )
    oos_result = _run_window(
        conn, date_start=oos_start, date_end=date_end, label="OOS", close_cfg=close_cfg, i36_cfg=i36_cfg, grid=grid
    )
    full_result = _run_window(
        conn,
        date_start=date_start,
        date_end=date_end,
        label="FULL",
        close_cfg=close_cfg,
        i36_cfg=i36_cfg,
        grid=grid,
    )

    ranked = sorted(
        [s for s in full_result.get("summaries") or [] if s.get("variant_id") not in ("I0",)],
        key=lambda s: (
            float(s.get("delta_vs_i36_pp") or -999),
            float(
                (full_result.get("hypothesis_results") or {})
                .get("variants", {})
                .get(s.get("variant_id"), {})
                .get("wk_metrics", {})
                .get("bleed_cohort_median_save_pct")
                or -999
            ),
        ),
        reverse=True,
    )

    return {
        "study": "c18acc_rrg_weakening_stop",
        "base_config": close_cfg.to_dict(),
        "date_start": date_start,
        "date_end": date_end,
        "is_end": is_end,
        "oos_start": oos_start,
        "hypotheses": HYPOTHESES_RRG_WK,
        "treatments": [c.to_dict() for c in grid],
        "controls": [c.to_dict() for c in BLEED_SWEEP_CONTROLS],
        "cohorts": {
            "C_bleed": "intrahold MTM≤−8% · orig max_hold · exit 日 RRG weakening/lagging",
            "C_pure_drip": "C_bleed ∩ I36 未觸發 extension",
        },
        "is": is_result,
        "oos": oos_result,
        "full": full_result,
        "best_by_delta_i36": ranked[0] if ranked else None,
        "note": (
            "Base：close_champion_score_swap_c_config · min_hold=5 max_hold=10 · "
            "日線 close RRG Weakening quadrant 停損 overlay · 對照 I0 / I36。"
        ),
    }


def render_rrg_weakening_stop_md(payload: dict[str, Any]) -> str:
    full = payload.get("full") or {}
    hypo_root = full.get("hypothesis_results") or {}
    i36_wk = hypo_root.get("I36_baseline_wk") or {}
    variants = hypo_root.get("variants") or {}

    lines = [
        "# C18acc Daily RRG Weakening Stop Sweep",
        "",
        payload.get("note", ""),
        "",
        f"**FULL**：{payload.get('date_start')} .. {payload.get('date_end')}  ",
        f"**IS**：{payload.get('date_start')} .. {payload.get('is_end')}  ",
        f"**OOS**：{payload.get('oos_start')} .. {payload.get('date_end')}",
        "",
        f"C_bleed cohort（FULL）：**{full.get('bleed_cohort_n')}** legs · "
        f"C_pure_drip：**{full.get('pure_drip_cohort_n')}** legs",
        "",
        "## 假說（H-RRG-WK-*）",
        "",
    ]
    for k, v in (payload.get("hypotheses") or {}).items():
        lines.append(f"- **{k}**：{v}")

    lines += [
        "",
        "## 全樣本 · 均超額 vs I36",
        "",
        "| id | mode | quad | ext | 均超額% | Δ I36 | max_hold深損 | P5 MTM | bleed quad% | bleed med save | A2 | B1 | C3 |",
        "|----|------|------|-----|---------|-------|-------------|--------|-------------|----------------|----|----|-----|",
    ]
    for s in full.get("summaries") or []:
        vid = s.get("variant_id")
        if vid == "I0":
            continue
        vh = variants.get(vid, {})
        hm = vh.get("hypotheses") or {}
        wm = vh.get("wk_metrics") or {}
        lines.append(
            f"| {vid} | {s.get('exit_mode', '—')} | {s.get('quad_exits', 0)} "
            f"| {s.get('ext_exits', 0)} | {s.get('mean_excess_pct')} | {s.get('delta_vs_i36_pp')} "
            f"| {wm.get('max_hold_deep_loss_n')} | {wm.get('p5_intrahold_mtm_pct')} "
            f"| {wm.get('bleed_cohort_quad_pct')} | {wm.get('bleed_cohort_median_save_pct')} "
            f"| {'✓' if hm.get('H-RRG-WK-A2') else '✗'} "
            f"| {'✓' if hm.get('H-RRG-WK-B1') else '✗'} "
            f"| {'✓' if hm.get('H-RRG-WK-C3') else '✗'} |"
        )

    lines += [
        "",
        f"I36 基線：max_hold 深損 **{i36_wk.get('max_hold_deep_loss_n')}** · "
        f"P5 MTM **{i36_wk.get('p5_intrahold_mtm_pct')}%** · "
        f"bleed ext **{hypo_root.get('I36_bleed_ext_pct')}%**（extension）",
        "",
        "## IS / OOS · Δ I36",
        "",
        "| id | IS Δ | OOS Δ | IS A2 | OOS A2 | IS B1 | OOS B1 | IS C3 | OOS C3 |",
        "|----|------|-------|-------|--------|-------|--------|-------|--------|",
    ]
    is_summ = {s["variant_id"]: s for s in (payload.get("is") or {}).get("summaries") or []}
    oos_summ = {s["variant_id"]: s for s in (payload.get("oos") or {}).get("summaries") or []}
    is_hypo = ((payload.get("is") or {}).get("hypothesis_results") or {}).get("variants") or {}
    oos_hypo = ((payload.get("oos") or {}).get("hypothesis_results") or {}).get("variants") or {}
    for cfg in rrg_weakening_stop_grid():
        vid = cfg.variant_id
        ih = is_hypo.get(vid, {}).get("hypotheses") or {}
        oh = oos_hypo.get(vid, {}).get("hypotheses") or {}
        lines.append(
            f"| {vid} | {is_summ.get(vid, {}).get('delta_vs_i36_pp')} "
            f"| {oos_summ.get(vid, {}).get('delta_vs_i36_pp')} "
            f"| {'✓' if ih.get('H-RRG-WK-A2') else '✗'} "
            f"| {'✓' if oh.get('H-RRG-WK-A2') else '✗'} "
            f"| {'✓' if ih.get('H-RRG-WK-B1') else '✗'} "
            f"| {'✓' if oh.get('H-RRG-WK-B1') else '✗'} "
            f"| {'✓' if ih.get('H-RRG-WK-C3') else '✗'} "
            f"| {'✓' if oh.get('H-RRG-WK-C3') else '✗'} |"
        )
    lines.append(
        f"| I36 | {is_summ.get('I36', {}).get('delta_vs_i36_pp')} "
        f"| {oos_summ.get('I36', {}).get('delta_vs_i36_pp')} "
        f"| ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |"
    )

    best = payload.get("best_by_delta_i36") or {}
    if best:
        bvid = best.get("variant_id")
        bh = variants.get(bvid, {}).get("hypotheses") or {}
        lines += [
            "",
            f"## 最佳（Δ I36）：**{bvid}** · Δ {best.get('delta_vs_i36_pp')}pp",
            f"- H-RRG-WK-D1（W6 only）：{'✓' if bh.get('H-RRG-WK-D1') else '✗'}",
        ]

    # Pass/fail summary
    lines += ["", "## 假說通過表（FULL）", "", "| 假說 | 通過 variant |", "|------|-------------|"]
    hypo_ids = list(HYPOTHESES_RRG_WK.keys())
    for hid in hypo_ids:
        passed = [vid for vid, vh in variants.items() if (vh.get("hypotheses") or {}).get(hid)]
        lines.append(f"| {hid} | {', '.join(passed) if passed else '—'} |")

    lines += ["", "## Case studies", ""]
    for cs in full.get("case_studies") or []:
        lines.append(f"### {cs.get('stock_id')}")
        for leg in cs.get("legs") or []:
            lines.append(
                f"- **{leg.get('variant_id')}** {leg.get('entry_date')}→{leg.get('exit_date')} "
                f"· {leg.get('exit_reason')} · ret {leg.get('return_pct')}% "
                f"· save {leg.get('save_vs_orig_pct')}"
            )
        lines.append("")

    lines += [
        "---",
        "模組：`scripts/run_c18acc_rrg_weakening_stop_sweep.py`",
        "",
    ]
    return "\n".join(lines)
