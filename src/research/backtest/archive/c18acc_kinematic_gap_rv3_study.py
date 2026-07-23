"""C18acc · MV gap (W20−W5 OR W5−W3) + RV3 decline exit interaction sweep.

Reads ``c18acc_kinematic_timeline-v1`` cache only.
Portfolio metric: fired legs exit at rule · else hold to ``return_pct``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

from research.backtest.archive.c18acc_kinematic_exit_sweep import load_kinematic_cache
from research.backtest.w3_rv_hl_winner_profile import _mean, _median

SCHEMA = "c18acc_kinematic_gap_rv3-v1"

GapLogic = Literal["or", "and", "w20_w5_only", "w5_w3_only"]
Rv3Mode = Literal["none", "drv", "poll_drop", "drv_and_poll", "drv_or_poll", "below_entry"]

GW5_GRID = (5.0, 6.0, 7.0, 8.0)
GW3_GRID = (1.0, 1.5, 2.0, 2.5)
DRV3_GRID = (0.15, 0.25, 0.35, 0.45)
DMV3_GRID = (0.0, 0.25, 0.35)
MIN_RET_GRID = (0.0, 5.0, 8.0, 10.0, 12.0, 15.0)
EXPAND_GRID = (0.0, 3.0, 4.0, 5.0)
CONSECUTIVE_GRID = (1, 2)
DIST_GRID = (0.0, 8.0)
PULLBACK_GRID = (False, True)


@dataclass(frozen=True)
class GapRv3Params:
    gap_logic: GapLogic
    gw5_min: float
    gw3_min: float
    gap_expand_min: float
    rv3_mode: Rv3Mode
    drv3_min: float
    dmv3_min: float
    min_ret_pct: float
    require_pullback: bool
    dist_min: float
    consecutive: int

    @property
    def variant_id(self) -> str:
        pb = "pb1" if self.require_pullback else "pb0"
        rv = self.rv3_mode
        dmv = int(self.dmv3_min * 100) if self.dmv3_min else 0
        exp = int(self.gap_expand_min) if self.gap_expand_min else 0
        return (
            f"GR_{self.gap_logic}_g5{int(self.gw5_min)}_g3{self.gw3_min:g}_x{exp}"
            f"_rv{rv}_d{int(self.drv3_min*100)}_mv3{dmv}"
            f"_r{int(self.min_ret_pct)}_{pb}_dist{int(self.dist_min)}_c{self.consecutive}"
        )


Leg = dict[str, Any]


def _rv3_ok(
    snap: dict[str, Any],
    prev: dict[str, Any] | None,
    entry: dict[str, Any],
    mode: Rv3Mode,
    drv_min: float,
) -> bool:
    if mode == "none":
        return True
    drv = snap.get("drv_w3")
    w3 = snap.get("w3_rv")
    prev_w3 = prev.get("w3_rv") if prev else None
    ent_w3 = entry.get("w3_rv")
    poll_drop = (
        w3 is not None
        and prev_w3 is not None
        and float(w3) < float(prev_w3) - 1e-9
    )
    drv_bad = drv is not None and float(drv) <= -drv_min + 1e-9
    if mode == "drv":
        return drv_bad
    if mode == "poll_drop":
        return poll_drop
    if mode == "drv_and_poll":
        return drv_bad and poll_drop
    if mode == "drv_or_poll":
        return drv_bad or poll_drop
    if mode == "below_entry":
        return (
            w3 is not None
            and ent_w3 is not None
            and float(w3) < float(ent_w3) - 1e-9
        )
    return False


def _gap_ok(
    snap: dict[str, Any],
    entry: dict[str, Any],
    params: GapRv3Params,
) -> bool:
    g5 = snap.get("gap_mv_w20_w5")
    g3 = snap.get("gap_mv_w5_w3")
    w5_ok = g5 is not None and float(g5) >= params.gw5_min - 1e-9
    w3_ok = g3 is not None and float(g3) >= params.gw3_min - 1e-9
    if params.gap_logic == "or":
        gap = w5_ok or w3_ok
    elif params.gap_logic == "and":
        gap = w5_ok and w3_ok
    elif params.gap_logic == "w20_w5_only":
        gap = w5_ok
    else:
        gap = w3_ok
    if params.gap_expand_min > 0:
        eg5 = entry.get("gap_mv_w20_w5")
        eg3 = entry.get("gap_mv_w5_w3")
        ex = False
        if g5 is not None and eg5 is not None and float(g5) - float(eg5) >= params.gap_expand_min - 1e-9:
            ex = True
        if g3 is not None and eg3 is not None and float(g3) - float(eg3) >= params.gap_expand_min - 1e-9:
            ex = True
        gap = gap and ex
    return gap


def _snap_ok(
    snap: dict[str, Any],
    prev: dict[str, Any] | None,
    entry: dict[str, Any],
    params: GapRv3Params,
) -> bool:
    if not _gap_ok(snap, entry, params):
        return False
    if not _rv3_ok(snap, prev, entry, params.rv3_mode, params.drv3_min):
        return False
    if params.dmv3_min > 0:
        d3 = snap.get("dmv_w3")
        if d3 is None or float(d3) > -params.dmv3_min + 1e-9:
            return False
    if params.require_pullback and snap.get("spread_class") != "pullback":
        return False
    if params.dist_min > 0:
        dist = snap.get("spread_dist")
        if dist is None or float(dist) < params.dist_min - 1e-9:
            return False
    if float(snap.get("ret_from_entry_pct") or 0) < params.min_ret_pct - 1e-9:
        return False
    return True


def _first_trigger_index(leg: Leg, params: GapRv3Params) -> int | None:
    timeline = leg.get("timeline") or []
    if len(timeline) < 2:
        return None
    entry = leg.get("entry_t0") or timeline[0]
    streak = 0
    for i in range(1, len(timeline)):
        prev = timeline[i - 1]
        ok = _snap_ok(timeline[i], prev, entry, params)
        streak = streak + 1 if ok else 0
        if streak >= params.consecutive:
            return i
    return None


def evaluate_leg_gap_rv3(leg: Leg, params: GapRv3Params) -> dict[str, Any]:
    timeline = leg.get("timeline") or []
    tri = _first_trigger_index(leg, params)
    peak_i = int(leg.get("peak_frame_idx") or 0)
    peak_ret = float(leg.get("peak_ret_pct") or 0)
    actual_ret = float(leg.get("return_pct") or 0)
    if tri is None:
        return {
            "fired": False,
            "exit_ret_pct": actual_ret,
            "vs_peak_pp": round(actual_ret - peak_ret, 3),
            "capture_pct": round(100.0 * actual_ret / peak_ret, 1) if peak_ret > 0.5 else None,
        }
    exit_ret = float(timeline[tri].get("ret_from_entry_pct") or 0)
    capture = round(100.0 * exit_ret / peak_ret, 1) if peak_ret > 0.5 else None
    return {
        "fired": True,
        "trigger_idx": tri,
        "exit_ret_pct": round(exit_ret, 3),
        "vs_peak_pp": round(exit_ret - peak_ret, 3),
        "vs_actual_pp": round(exit_ret - actual_ret, 3),
        "capture_pct": capture,
        "at_or_before_peak": tri <= peak_i,
        "trigger_snap": timeline[tri],
    }


def _summarize_portfolio(
    legs: list[Leg],
    results: list[dict[str, Any]],
    *,
    variant_id: str,
    params: GapRv3Params | None = None,
) -> dict[str, Any]:
    hold_mean = _mean([float(l["return_pct"]) for l in legs])
    all_rets = [float(r["exit_ret_pct"]) for r in results]
    fired = [r for r in results if r.get("fired")]
    nf = len(fired)
    not_fired_rets = [float(r["exit_ret_pct"]) for r in results if not r.get("fired")]
    summary: dict[str, Any] = {
        "variant_id": variant_id,
        "n_legs": len(legs),
        "n_fired": nf,
        "fire_rate_pct": round(100.0 * nf / len(legs), 1) if legs else 0.0,
        "mean_all_legs_ret_pct": _mean(all_rets),
        "median_all_legs_ret_pct": _median(all_rets),
        "delta_vs_hold_pp": round(_mean(all_rets) - hold_mean, 3),
        "hold_baseline_mean_pct": hold_mean,
        "mean_not_fired_ret_pct": _mean(not_fired_rets) if not_fired_rets else None,
    }
    if params is not None:
        summary["params"] = {
            "gap_logic": params.gap_logic,
            "gw5_min": params.gw5_min,
            "gw3_min": params.gw3_min,
            "gap_expand_min": params.gap_expand_min,
            "rv3_mode": params.rv3_mode,
            "drv3_min": params.drv3_min,
            "dmv3_min": params.dmv3_min,
            "min_ret_pct": params.min_ret_pct,
            "require_pullback": params.require_pullback,
            "dist_min": params.dist_min,
            "consecutive": params.consecutive,
        }
    if nf:
        summary.update(
            {
                "mean_exit_on_fired_pct": _mean([float(r["exit_ret_pct"]) for r in fired]),
                "mean_capture_on_fired_pct": _mean(
                    [float(r["capture_pct"]) for r in fired if r.get("capture_pct") is not None]
                ),
                "pct_fired_at_or_before_peak": round(
                    100.0 * sum(1 for r in fired if r.get("at_or_before_peak")) / nf,
                    1,
                ),
            }
        )
    return summary


def _rule_grid(*, full: bool = True) -> list[GapRv3Params]:
    """Parameter grid · pruned for runtime (~5k variants full)."""
    variants: list[GapRv3Params] = []

    def _add(
        gap_logic: GapLogic,
        gw5: float,
        gw3: float,
        expand: float,
        rv3_mode: Rv3Mode,
        drv3: float,
        dmv3: float,
        min_ret: float,
        pb: bool,
        dist: float,
        consecutive: int,
    ) -> None:
        variants.append(
            GapRv3Params(
                gap_logic=gap_logic,
                gw5_min=gw5,
                gw3_min=gw3,
                gap_expand_min=expand,
                rv3_mode=rv3_mode,
                drv3_min=drv3,
                dmv3_min=dmv3,
                min_ret_pct=min_ret,
                require_pullback=pb,
                dist_min=dist,
                consecutive=consecutive,
            )
        )

    gap_pairs_or = [(g5, g3) for g5 in GW5_GRID for g3 in GW3_GRID]
    gap_pairs_and = [(6.0, 1.5), (7.0, 2.0), (8.0, 2.5)]
    expands = (0.0, 4.0)
    min_rets = (0.0, 5.0, 8.0, 10.0, 12.0, 15.0) if full else (0.0, 10.0, 12.0)
    rv_modes: tuple[Rv3Mode, ...] = (
        "none",
        "drv",
        "poll_drop",
        "drv_or_poll",
        "drv_and_poll",
    )
    if not full:
        rv_modes = ("none", "drv", "poll_drop", "drv_or_poll")

    for expand in expands:
        for min_ret in min_rets:
            for pb in PULLBACK_GRID:
                dists = DIST_GRID if full and pb else (0.0,)
                for dist in dists:
                    for consecutive in CONSECUTIVE_GRID:
                        if not full and consecutive != 2:
                            continue
                        # --- gap OR (primary) ---
                        for gw5, gw3 in gap_pairs_or:
                            for rv3_mode in rv_modes:
                                drv_list = (
                                    DRV3_GRID
                                    if rv3_mode in ("drv", "drv_or_poll", "drv_and_poll")
                                    else (0.0,)
                                )
                                for drv3 in drv_list:
                                    _add(
                                        "or",
                                        gw5,
                                        gw3,
                                        expand,
                                        rv3_mode,
                                        drv3,
                                        0.0,
                                        min_ret,
                                        pb,
                                        dist,
                                        consecutive,
                                    )
                        if not full:
                            continue
                        # --- w20_w5 only / w5_w3 only ---
                        for gw5 in GW5_GRID:
                            for rv3_mode in rv_modes:
                                for drv3 in (
                                    DRV3_GRID
                                    if rv3_mode in ("drv", "drv_or_poll", "drv_and_poll")
                                    else (0.0,)
                                ):
                                    _add(
                                        "w20_w5_only",
                                        gw5,
                                        GW3_GRID[0],
                                        expand,
                                        rv3_mode,
                                        drv3,
                                        0.0,
                                        min_ret,
                                        pb,
                                        dist,
                                        consecutive,
                                    )
                        for gw3 in GW3_GRID:
                            for rv3_mode in rv_modes:
                                for drv3 in (
                                    DRV3_GRID
                                    if rv3_mode in ("drv", "drv_or_poll", "drv_and_poll")
                                    else (0.0,)
                                ):
                                    _add(
                                        "w5_w3_only",
                                        GW5_GRID[0],
                                        gw3,
                                        expand,
                                        rv3_mode,
                                        drv3,
                                        0.0,
                                        min_ret,
                                        pb,
                                        dist,
                                        consecutive,
                                    )
                        # --- gap AND (subset) ---
                        for gw5, gw3 in gap_pairs_and:
                            for rv3_mode in ("none", "drv", "drv_or_poll"):
                                for drv3 in (
                                    DRV3_GRID if rv3_mode != "none" else (0.0,)
                                ):
                                    _add(
                                        "and",
                                        gw5,
                                        gw3,
                                        expand,
                                        rv3_mode,
                                        drv3,
                                        0.0,
                                        min_ret,
                                        pb,
                                        dist,
                                        consecutive,
                                    )

    seen: set[str] = set()
    out: list[GapRv3Params] = []
    for v in variants:
        if v.variant_id not in seen:
            seen.add(v.variant_id)
            out.append(v)
    return out


def _ablation_vs_gap_only(
    legs: list[Leg],
    summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare best gap-only (rv3=none) vs best gap+rv3 at same gap logic."""
    gap_only = [s for s in summaries if (s.get("params") or {}).get("rv3_mode") == "none"]
    with_rv = [s for s in summaries if (s.get("params") or {}).get("rv3_mode") not in (None, "none")]
    best_gap = max(gap_only, key=lambda s: float(s.get("mean_all_legs_ret_pct") or 0), default={})
    best_rv = max(with_rv, key=lambda s: float(s.get("mean_all_legs_ret_pct") or 0), default={})
    return {
        "best_gap_only": best_gap,
        "best_gap_plus_rv3": best_rv,
        "rv3_lift_pp": round(
            float(best_rv.get("mean_all_legs_ret_pct") or 0)
            - float(best_gap.get("mean_all_legs_ret_pct") or 0),
            3,
        ),
    }


def _target_fire_rate_for_mean(
    hold_mean: float,
    fired_mean: float,
    not_fired_mean: float,
    target: float,
) -> float | None:
    """Solve portfolio mean = target for fire rate (0..1)."""
    denom = fired_mean - not_fired_mean
    if abs(denom) < 1e-9:
        return None
    rate = (target - not_fired_mean) / denom
    if rate < 0 or rate > 1:
        return None
    return round(rate * 100.0, 1)


def run_c18acc_kinematic_gap_rv3_study(
    cache: dict[str, Any],
    *,
    full_grid: bool = True,
    top_n: int = 25,
) -> dict[str, Any]:
    legs: list[Leg] = cache.get("legs") or []
    if not legs:
        raise ValueError("cache has no legs")
    hold_mean = _mean([float(l["return_pct"]) for l in legs])
    peak_mean = _mean([float(l["peak_ret_pct"]) for l in legs])

    variants = _rule_grid(full=full_grid)
    summaries: list[dict[str, Any]] = []
    for params in variants:
        results = [evaluate_leg_gap_rv3(leg, params) for leg in legs]
        summaries.append(_summarize_portfolio(legs, results, variant_id=params.variant_id, params=params))

    summaries.sort(key=lambda s: -(float(s.get("mean_all_legs_ret_pct") or 0)))
    top = summaries[:top_n]
    ablation = _ablation_vs_gap_only(legs, summaries)
    best = top[0] if top else {}
    best_nf = float(best.get("mean_not_fired_ret_pct") or hold_mean)
    best_fm = float(best.get("mean_exit_on_fired_pct") or 0)
    target_9_rate = _target_fire_rate_for_mean(hold_mean, best_fm, best_nf, 9.0)

    # Interaction slices: fix gap OR + gw5/gw3 from best · sweep rv3/min_ret
    anchor = best.get("params") or {}
    interaction: list[dict[str, Any]] = []
    if anchor:
        base_kw = dict(
            gap_logic=anchor.get("gap_logic", "or"),
            gw5_min=float(anchor.get("gw5_min", 7)),
            gw3_min=float(anchor.get("gw3_min", 2.0)),
            gap_expand_min=float(anchor.get("gap_expand_min", 0)),
            require_pullback=bool(anchor.get("require_pullback")),
            dist_min=float(anchor.get("dist_min", 0)),
            consecutive=int(anchor.get("consecutive", 2)),
        )
        for rv3_mode in ("none", "drv", "poll_drop", "drv_or_poll", "drv_and_poll"):
            for drv3 in DRV3_GRID:
                for min_ret in (0.0, 8.0, 10.0, 12.0, 15.0):
                    p = GapRv3Params(
                        **base_kw,
                        rv3_mode=rv3_mode,
                        drv3_min=drv3 if rv3_mode != "none" else 0.0,
                        dmv3_min=0.0,
                        min_ret_pct=min_ret,
                    )
                    results = [evaluate_leg_gap_rv3(leg, p) for leg in legs]
                    interaction.append(
                        _summarize_portfolio(legs, results, variant_id=p.variant_id, params=p)
                    )
        interaction.sort(key=lambda s: -(float(s.get("mean_all_legs_ret_pct") or 0)))

    return {
        "schema": SCHEMA,
        "cache_schema": cache.get("schema"),
        "date_start": cache.get("date_start"),
        "date_end": cache.get("date_end"),
        "n_slots": cache.get("n_slots"),
        "n_legs": len(legs),
        "poll": cache.get("poll"),
        "n_variants": len(summaries),
        "hold_baseline": {
            "mean_all_legs_ret_pct": hold_mean,
            "mean_peak_ret_pct": peak_mean,
            "portfolio_capture_pct": round(100.0 * hold_mean / peak_mean, 1) if peak_mean else None,
        },
        "target_9pct_analysis": {
            "best_observed_mean_pct": best.get("mean_all_legs_ret_pct"),
            "best_observed_delta_pp": best.get("delta_vs_hold_pp"),
            "best_fired_mean_pct": best.get("mean_exit_on_fired_pct"),
            "best_fire_rate_pct": best.get("fire_rate_pct"),
            "implied_fire_rate_for_9pct": target_9_rate,
            "feasible_without_9pct": float(best.get("mean_all_legs_ret_pct") or 0) < 9.0,
        },
        "ablation": ablation,
        "top_variants": top,
        "interaction_slice": interaction[:20],
        "all_summaries": summaries,
    }


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def render_c18acc_kinematic_gap_rv3_study_md(payload: dict[str, Any]) -> str:
    n = payload.get("n_legs", 0)
    hold = payload.get("hold_baseline") or {}
    top = payload.get("top_variants") or []
    abl = payload.get("ablation") or {}
    t9 = payload.get("target_9pct_analysis") or {}
    inter = payload.get("interaction_slice") or []
    best_gap = abl.get("best_gap_only") or {}
    best_rv = abl.get("best_gap_plus_rv3") or {}

    lines = [
        "# C18acc · MV gap OR + RV3 下降 · 交互出場掃描",
        "",
        f"區間：**{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"**{n}** legs · {payload.get('n_variants')} variants · poll {payload.get('poll')}",
        "",
        "## 規則結構",
        "",
        "- **Gap**：`(gap_mv_w20_w5 ≥ G5) OR (gap_mv_w5_w3 ≥ G3)`（亦掃 AND / 單腿）",
        "- **RV3 下降**：`drv_w3 ≤ −δ` · 或 W3 RV 較前一根 poll 下降 · 或兩者",
        "- 可選：`dmv_w3` · Pullback · dist≥8 · 最低獲利 `min_ret` · 連續 2 poll",
        "- **Portfolio 均報酬**：觸發 leg 規則賣出 · 未觸發 hold",
        "",
        "## Baseline",
        "",
        f"- Hold 均報酬 **{_fmt(hold.get('mean_all_legs_ret_pct'))}%** · "
        f"peak oracle **{_fmt(hold.get('mean_peak_ret_pct'))}%** · "
        f"捕捉 **{hold.get('portfolio_capture_pct')}%**",
        "",
        "## 9% 目標可行性",
        "",
        f"- 本次最佳 portfolio 均報酬 **{_fmt(t9.get('best_observed_mean_pct'))}%** "
        f"(Δ {_fmt(t9.get('best_observed_delta_pp'))}pp)",
        f"- 最佳組合：觸發 **{t9.get('best_fire_rate_pct')}%** · 觸發 leg 賣出均 "
        f"**{_fmt(t9.get('best_fired_mean_pct'))}%**",
    ]
    if t9.get("implied_fire_rate_for_9pct") is not None:
        lines.append(
            f"- 若維持觸發 leg 賣出均 **{_fmt(t9.get('best_fired_mean_pct'))}%**，"
            f"portfolio 要達 **9%** 需觸發率约 **{t9.get('implied_fire_rate_for_9pct')}%**"
        )
    else:
        lines.append("- 在現有觸發 leg 賣出均下，**無法**僅靠提高觸發率達 9% portfolio 均報酬")
    lines.extend(
        [
            "",
            "> 7%→9% 需 +2pp：未觸發 leg 仍約 6.9% 拖累；"
            "除非觸發 leg 賣出均 >30% 且觸發率 >15%，或改變 hold 合約本身。",
            "",
            "## Gap-only vs Gap+RV3（ablation）",
            "",
            f"- 最佳 **gap-only**：`{best_gap.get('variant_id')}` · "
            f"均 **{_fmt(best_gap.get('mean_all_legs_ret_pct'))}%**",
            f"- 最佳 **gap+RV3**：`{best_rv.get('variant_id')}` · "
            f"均 **{_fmt(best_rv.get('mean_all_legs_ret_pct'))}%** · "
            f"RV3 增量 **{_fmt(abl.get('rv3_lift_pp'))}pp**",
            "",
            "## Top 20 portfolio 均報酬",
            "",
            "| # | variant | 觸發率 | 全 leg 均 | Δ hold | 觸發賣出均 | 捕捉% |",
            "|---|---------|--------|---------|--------|-----------|-------|",
        ]
    )
    for i, s in enumerate(top[:20], 1):
        lines.append(
            f"| {i} | `{s.get('variant_id')}` | {s.get('fire_rate_pct')}% | "
            f"**{_fmt(s.get('mean_all_legs_ret_pct'))}%** | "
            f"{_fmt(s.get('delta_vs_hold_pp'))}pp | "
            f"{_fmt(s.get('mean_exit_on_fired_pct'))}% | "
            f"{_fmt(s.get('mean_capture_on_fired_pct'))}% |"
        )

    if inter:
        lines.extend(
            [
                "",
                "## 交互切片（固定最佳 gap 門檻 · 掃 RV3 × min_ret）",
                "",
                "| rv3 | drv3 | min_ret | 觸發率 | 全 leg 均 | Δ hold |",
                "|-----|------|---------|--------|---------|--------|",
            ]
        )
        for s in inter[:12]:
            p = s.get("params") or {}
            lines.append(
                f"| {p.get('rv3_mode')} | {p.get('drv3_min')} | {p.get('min_ret_pct')}% | "
                f"{s.get('fire_rate_pct')}% | **{_fmt(s.get('mean_all_legs_ret_pct'))}%** | "
                f"{_fmt(s.get('delta_vs_hold_pp'))}pp |"
            )

    best0 = top[0] if top else {}
    p0 = best0.get("params") or {}
    lines.extend(
        [
            "",
            "## 結論",
            "",
            f"- **推薦組合**：`{best0.get('variant_id')}`",
            f"  - Gap **{p0.get('gap_logic')}** · W20−W5≥{p0.get('gw5_min')} OR W5−W3≥{p0.get('gw3_min')}",
            f"  - RV3 **{p0.get('rv3_mode')}** δ={p0.get('drv3_min')} · min_ret≥{p0.get('min_ret_pct')}%",
            f"  - 全 leg 均 **{_fmt(best0.get('mean_all_legs_ret_pct'))}%** "
            f"(較 hold +{_fmt(best0.get('delta_vs_hold_pp'))}pp)",
            "",
            "### 實務分層（對照 lead-lag）",
            "",
            "1. **Gap OR 成立**（W20−W5 或 W5−W3 拉開）→ 結構進入獲利區",
            "2. **RV3 drv 轉負**（短線 RV 自 peak 回落）→ MV 領先價格確認（≈25 poll 領先）",
            "3. **min_ret≥10%** 過濾過早賣出 · 連續 2 poll",
            "",
            "### 9%+ 路徑（需超出本 sweep）",
            "",
            "- 觸發 leg 加 **partial scale-out**（規則賣 50% · 餘 hold）",
            "- 或 **ABC v3+f1 子池**（lead-lag 90d 樣本）單獨標定",
            "- 或提高 hold 合約本身（非出場規則）",
        ]
    )
    return "\n".join(lines)


# --- Fixed hold-day cap study (truncate timeline · no cache rebuild) ---

HOLD_CAP_SCHEMA = "c18acc_kinematic_hold_cap_gap_rv3-v1"

CHAMPION_GAP_RV3 = GapRv3Params(
    gap_logic="or",
    gw5_min=8.0,
    gw3_min=2.0,
    gap_expand_min=0.0,
    rv3_mode="drv_and_poll",
    drv3_min=0.45,
    dmv3_min=0.0,
    min_ret_pct=12.0,
    require_pullback=False,
    dist_min=0.0,
    consecutive=1,
)


def truncate_leg_to_hold_days(leg: Leg, hold_days: int) -> Leg | None:
    """Cap leg at ``hold_days`` trading days from entry (inclusive anchor)."""
    timeline = leg.get("timeline") or []
    if len(timeline) < 2 or hold_days < 1:
        return None
    entry_date = str(leg.get("entry_date") or timeline[0].get("trade_date") or "")
    dates = sorted({str(s.get("trade_date")) for s in timeline if s.get("trade_date")})
    if not dates:
        return None
    if entry_date not in dates:
        entry_date = str(timeline[0].get("trade_date") or dates[0])
    try:
        idx = dates.index(entry_date)
    except ValueError:
        return None
    cap_idx = min(idx + hold_days - 1, len(dates) - 1)
    exit_date = dates[cap_idx]
    sub = [s for s in timeline if str(s.get("trade_date") or "") <= exit_date]
    if len(sub) < 2:
        return None
    peak_i = max(range(len(sub)), key=lambda i: float(sub[i].get("ret_from_entry_pct") or 0))
    hold_ret = round(float(sub[-1].get("ret_from_entry_pct") or 0), 3)
    return {
        **leg,
        "timeline": sub,
        "entry_t0": sub[0],
        "exit_date": exit_date,
        "hold_days_cap": hold_days,
        "return_pct": hold_ret,
        "outcome": "win" if hold_ret > 0 else "loss",
        "peak_ret_pct": round(float(sub[peak_i].get("ret_from_entry_pct") or 0), 3),
        "peak_frame_idx": peak_i,
    }


def truncate_cache_legs(legs: list[Leg], hold_days: int) -> list[Leg]:
    out: list[Leg] = []
    for leg in legs:
        t = truncate_leg_to_hold_days(leg, hold_days)
        if t is not None:
            out.append(t)
    return out


def _pct_hit_target(legs: list[Leg], target_pct: float) -> float:
    if not legs:
        return 0.0
    hits = sum(1 for l in legs if float(l.get("return_pct") or 0) >= target_pct - 1e-9)
    return round(100.0 * hits / len(legs), 1)


def _eval_variant_on_legs(
    legs: list[Leg],
    params: GapRv3Params | None,
    *,
    variant_id: str,
) -> dict[str, Any]:
    if params is None:
        results = [
            {"fired": False, "exit_ret_pct": float(l["return_pct"])} for l in legs
        ]
    else:
        results = [evaluate_leg_gap_rv3(leg, params) for leg in legs]
    s = _summarize_portfolio(legs, results, variant_id=variant_id, params=params)
    s["pct_legs_hit_10"] = _pct_hit_target(
        [{"return_pct": r["exit_ret_pct"]} for r in results], 10.0
    )
    return s


def run_hold_cap_gap_rv3_study(
    cache: dict[str, Any],
    *,
    hold_days_list: tuple[int, ...] = (4, 5),
    target_pct: float = 10.0,
) -> dict[str, Any]:
    """Compare hold cap 4d/5d vs sim baseline · champion Gap+RV3 · min_ret sweep."""
    legs_raw: list[Leg] = cache.get("legs") or []
    if not legs_raw:
        raise ValueError("cache has no legs")

    sim_hold = _summarize_portfolio(
        legs_raw,
        [{"fired": False, "exit_ret_pct": float(l["return_pct"])} for l in legs_raw],
        variant_id="sim_baseline",
    )
    sim_hold["pct_legs_hit_10"] = _pct_hit_target(legs_raw, target_pct)
    sim_hold["label"] = "sim_dynamic_exit (~10d mean)"

    by_hold: dict[str, Any] = {}
    min_ret_grid = (8.0, 10.0, 12.0, 15.0)
    rv_modes = ("drv_and_poll", "poll_drop", "drv_or_poll")

    for hd in hold_days_list:
        legs = truncate_cache_legs(legs_raw, hd)
        hold_row = _eval_variant_on_legs(legs, None, variant_id=f"hold_{hd}d")
        hold_row["hold_days_cap"] = hd
        hold_row["n_legs_truncated"] = len(legs)
        hold_row["pct_legs_hit_10"] = _pct_hit_target(legs, target_pct)

        rule_rows: list[dict[str, Any]] = []
        for min_ret in min_ret_grid:
            for rv_mode in rv_modes:
                for gw5 in (7.0, 8.0):
                    for consecutive in (1, 2):
                        p = GapRv3Params(
                            gap_logic="or",
                            gw5_min=gw5,
                            gw3_min=2.0,
                            gap_expand_min=0.0,
                            rv3_mode=rv_mode,
                            drv3_min=0.45 if rv_mode != "poll_drop" else 0.0,
                            dmv3_min=0.0,
                            min_ret_pct=min_ret,
                            require_pullback=False,
                            dist_min=0.0,
                            consecutive=consecutive,
                        )
                        rule_rows.append(_eval_variant_on_legs(legs, p, variant_id=p.variant_id))

        rule_rows.sort(key=lambda s: -(float(s.get("mean_all_legs_ret_pct") or 0)))
        best = rule_rows[0] if rule_rows else {}
        hit_10 = [r for r in rule_rows if float(r.get("mean_all_legs_ret_pct") or 0) >= target_pct]
        by_hold[str(hd)] = {
            "hold_days_cap": hd,
            "n_legs": len(legs),
            "hold_only": hold_row,
            "best_rule": best,
            "rules_at_or_above_target": hit_10[:5],
            "top_rules": rule_rows[:10],
            "champion_min_ret10": _eval_variant_on_legs(
                legs,
                replace(CHAMPION_GAP_RV3, min_ret_pct=10.0),
                variant_id="champion_gap_rv3_min10",
            ),
        }

    return {
        "schema": HOLD_CAP_SCHEMA,
        "cache_schema": cache.get("schema"),
        "date_start": cache.get("date_start"),
        "date_end": cache.get("date_end"),
        "n_slots": cache.get("n_slots"),
        "n_legs_source": len(legs_raw),
        "target_pct": target_pct,
        "sim_baseline": sim_hold,
        "by_hold_days": by_hold,
    }


def render_hold_cap_gap_rv3_study_md(payload: dict[str, Any]) -> str:
    target = float(payload.get("target_pct") or 10.0)
    sim = payload.get("sim_baseline") or {}
    by_hold = payload.get("by_hold_days") or {}

    lines = [
        f"# C18acc · 持有上限 4/5 天 × Gap OR + RV3（目標 **{target:g}%**）",
        "",
        f"區間：**{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"源 legs **{payload.get('n_legs_source')}** · timeline 截斷（不重跑 trajectory）",
        "",
        "## Sim 動態出場 baseline（對照）",
        "",
        f"- 均報酬 **{_fmt(sim.get('mean_all_legs_ret_pct'))}%** · "
        f"≥{target:g}% leg 占比 **{sim.get('pct_legs_hit_10')}%**",
        "",
    ]

    for hd_key in sorted(by_hold.keys(), key=int):
        block = by_hold[hd_key]
        hd = block.get("hold_days_cap")
        hold = block.get("hold_only") or {}
        best = block.get("best_rule") or {}
        champ = block.get("champion_min_ret10") or {}
        above = block.get("rules_at_or_above_target") or []

        lines.extend(
            [
                f"## 持有上限 **{hd}** 個交易日",
                "",
                f"- **Hold only**：均 **{_fmt(hold.get('mean_all_legs_ret_pct'))}%** · "
                f"≥{target:g}% 占比 **{hold.get('pct_legs_hit_10')}%** · n={block.get('n_legs')}",
                f"- **最佳 Gap+RV3**：`{best.get('variant_id')}` · 均 **{_fmt(best.get('mean_all_legs_ret_pct'))}%** "
                f"(Δ {_fmt(best.get('delta_vs_hold_pp'))}pp) · 觸發 {best.get('fire_rate_pct')}% · "
                f"≥{target:g}% 占比 **{best.get('pct_legs_hit_10')}%**",
                f"- **Champion min_ret=10%**：均 **{_fmt(champ.get('mean_all_legs_ret_pct'))}%** · "
                f"觸發 {champ.get('fire_rate_pct')}%",
                "",
            ]
        )
        if above:
            lines.append(f"### 均報酬 ≥ {target:g}% 的規則")
            lines.append("")
            for r in above:
                lines.append(
                    f"- `{r.get('variant_id')}` · 均 **{_fmt(r.get('mean_all_legs_ret_pct'))}%** · "
                    f"觸發 {r.get('fire_rate_pct')}%"
                )
            lines.append("")

        lines.extend(
            [
                f"### Top 5（{hd}d）",
                "",
                "| variant | 全 leg 均 | Δ hold | 觸發率 | ≥10% 占比 |",
                "|---------|---------|--------|--------|----------|",
            ]
        )
        for r in (block.get("top_rules") or [])[:5]:
            lines.append(
                f"| `{r.get('variant_id')}` | **{_fmt(r.get('mean_all_legs_ret_pct'))}%** | "
                f"{_fmt(r.get('delta_vs_hold_pp'))}pp | {r.get('fire_rate_pct')}% | "
                f"{r.get('pct_legs_hit_10')}% |"
            )
        lines.append("")

    # Conclusion
    best_val, best_hd, best_block = max(
        (
            (
                float((b.get("best_rule") or {}).get("mean_all_legs_ret_pct") or 0),
                hd,
                b,
            )
            for hd, b in by_hold.items()
        ),
        default=(0.0, "", {}),
    )
    lines.extend(
        [
            "## 結論",
            "",
            f"- **{target:g}% portfolio 均報酬**："
            + (
                "**可達**"
                if best_val >= target
                else f"**未達**（最佳 **{best_val:.2f}%** · hold **{best_hd}d**）"
            ),
            "",
            "- 縮短持有日 → hold-only 均報酬通常 **下降**（來不及跑完 trend）",
            "- Gap+RV3 價值在 **提早鎖定 ≥10–12%** 的 leg，減少 cap 到期前回吐",
            "- 若 cap=4/5d 且目標 10%：宜 **min_ret=10%** + RV3 確認，gap 門檻可略放寬（W20−W5≥7）",
        ]
    )
    return "\n".join(lines)
