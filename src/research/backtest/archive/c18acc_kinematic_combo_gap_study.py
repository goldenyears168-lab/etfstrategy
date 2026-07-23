"""C18acc · R2+R7 layered exit combo + MV gap expansion timing study.

Reads ``c18acc_kinematic_timeline-v1`` cache only (no trajectory rebuild).
Lead-lag context: ABC v3+f1 study shows champion legs W3 MV leads price peak
~25 polls · w3>w5>w20 cascade — gap expansion toward peak precedes MV decline.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Literal

from research.backtest.archive.c18acc_kinematic_exit_sweep import (
    RuleParams,
    _evaluate_leg,
    _first_trigger_index,
    _pred_for_params,
    load_kinematic_cache,
)
from research.backtest.w3_rv_hl_winner_profile import _mean, _median

SCHEMA = "c18acc_kinematic_combo_gap-v1"

# Champion params from prior sweeps
DEFAULT_R2 = RuleParams("R2", 0.15, 0.1, 2, 8.0, False)
DEFAULT_R7 = RuleParams(
    "R7", 0.0, 0.1, 2, 0.0, False, gap_w20_w5_min=6.0, gap_w20_w5_expand=4.0
)

LayerMode = Literal[
    "hold",
    "R2_only",
    "R7_only",
    "first_fire",
    "R7_priority",
    "R2_warn_R7_exit",
    "R2_then_R7",
    "R2_and_R7",
]

LAYER_MODE_LABELS: dict[str, str] = {
    "hold": "完全不賣（hold to strategy exit）",
    "R2_only": "僅 R2（Pullback+dist≥8）",
    "R7_only": "僅 R7（gap≥6 · expand≥4）",
    "first_fire": "R2/R7 谁先触发",
    "R7_priority": "R7 優先 · 否則 R2",
    "R2_warn_R7_exit": "僅 R7 出場（R2 只作 early warning）",
    "R2_then_R7": "R2 曾触发 → R7 確認出場",
    "R2_and_R7": "同一 poll R2∧R7",
}

GapPair = Literal["w20_w5", "w20_w3", "w5_w3"]
GapMode = Literal["abs", "expand", "pct_w20"]

GAP_FIELD: dict[GapPair, str] = {
    "w20_w5": "gap_mv_w20_w5",
    "w20_w3": "gap_mv_w20_w3",
    "w5_w3": "gap_mv_w5_w3",
}

ABS_GRID: dict[GapPair, tuple[float, ...]] = {
    "w20_w5": (3.0, 4.0, 5.0, 6.0, 7.0, 8.0),
    "w20_w3": (3.0, 4.0, 5.0, 6.0, 7.0, 8.0),
    "w5_w3": (0.0, 0.5, 1.0, 1.5, 2.0, 2.5),
}

EXPAND_GRID: dict[GapPair, tuple[float, ...]] = {
    "w20_w5": (2.0, 3.0, 4.0, 5.0, 6.0),
    "w20_w3": (2.0, 3.0, 4.0, 5.0, 6.0),
    "w5_w3": (0.5, 1.0, 1.5, 2.0, 2.5),
}

PCT_W20_GRID: dict[GapPair, tuple[float, ...]] = {
    "w20_w5": (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0),
    "w20_w3": (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0),
    "w5_w3": (0.2, 0.4, 0.6, 0.8, 1.0, 1.5),
}


@dataclass(frozen=True)
class LayerSpec:
    mode: LayerMode
    r2: RuleParams = DEFAULT_R2
    r7: RuleParams = DEFAULT_R7


Leg = dict[str, Any]
Snap = dict[str, Any]


def _leg_exit_result(leg: Leg, tri: int | None, *, source: str) -> dict[str, Any]:
    timeline = leg.get("timeline") or []
    entry = leg.get("entry_t0") or (timeline[0] if timeline else {})
    peak_i = int(leg.get("peak_frame_idx") or 0)
    peak_ret = float(leg.get("peak_ret_pct") or 0)
    actual_ret = float(leg.get("return_pct") or 0)
    if tri is None:
        return {
            "fired": False,
            "source": source,
            "exit_ret_pct": actual_ret,
            "vs_peak_pp": round(actual_ret - peak_ret, 3),
            "vs_actual_pp": 0.0,
            "capture_pct": round(100.0 * actual_ret / peak_ret, 1) if peak_ret > 0.5 else None,
            "at_or_before_peak": None,
            "r2_warn": False,
        }
    ts = timeline[tri]
    exit_ret = float(ts.get("ret_from_entry_pct") or 0)
    capture = round(100.0 * exit_ret / peak_ret, 1) if peak_ret > 0.5 else None
    return {
        "fired": True,
        "source": source,
        "trigger_idx": tri,
        "exit_ret_pct": round(exit_ret, 3),
        "vs_peak_pp": round(exit_ret - peak_ret, 3),
        "vs_actual_pp": round(exit_ret - actual_ret, 3),
        "capture_pct": capture,
        "at_or_before_peak": tri <= peak_i,
        "trigger_snap": ts,
        "r2_warn": False,
    }


def _trigger_indices(
    leg: Leg,
    r2: RuleParams,
    r7: RuleParams,
) -> tuple[int | None, int | None]:
    timeline = leg.get("timeline") or []
    if len(timeline) < 2:
        return None, None
    entry = leg.get("entry_t0") or timeline[0]
    r2_idx = _first_trigger_index(timeline, entry, _pred_for_params(r2), r2)
    r7_idx = _first_trigger_index(timeline, entry, _pred_for_params(r7), r7)
    return r2_idx, r7_idx


def evaluate_leg_layered(
    leg: Leg,
    spec: LayerSpec,
) -> dict[str, Any]:
    """Portfolio-style exit: fired legs use rule exit · else hold to ``return_pct``."""
    mode = spec.mode
    if mode == "hold":
        return _leg_exit_result(leg, None, source="hold")

    r2_idx, r7_idx = _trigger_indices(leg, spec.r2, spec.r7)
    tri: int | None = None
    source = mode

    if mode == "R2_only":
        tri = r2_idx
        source = "R2"
    elif mode == "R7_only":
        tri = r7_idx
        source = "R7"
    elif mode == "first_fire":
        cands = [(i, s) for i, s in ((r2_idx, "R2"), (r7_idx, "R7")) if i is not None]
        if cands:
            tri, source = min(cands, key=lambda x: x[0])
    elif mode == "R7_priority":
        if r7_idx is not None:
            tri, source = r7_idx, "R7"
        elif r2_idx is not None:
            tri, source = r2_idx, "R2"
    elif mode == "R2_warn_R7_exit":
        tri, source = r7_idx, "R7"
    elif mode == "R2_then_R7":
        if r7_idx is not None and r2_idx is not None and r2_idx <= r7_idx:
            tri, source = r7_idx, "R7|R2_confirmed"
    elif mode == "R2_and_R7":
        if r2_idx is not None and r7_idx is not None and r2_idx == r7_idx:
            tri, source = r2_idx, "R2∧R7"

    out = _leg_exit_result(leg, tri, source=source)
    out["r2_warn"] = r2_idx is not None
    out["r2_idx"] = r2_idx
    out["r7_idx"] = r7_idx
    return out


def _summarize_portfolio(
    legs: list[Leg],
    results: list[dict[str, Any]],
    *,
    variant_id: str,
) -> dict[str, Any]:
    n = len(legs)
    hold_mean = _mean([float(l["return_pct"]) for l in legs])
    all_rets = [float(r["exit_ret_pct"]) for r in results]
    fired = [r for r in results if r.get("fired")]
    nf = len(fired)
    peaks = [float(l["peak_ret_pct"]) for l in legs]
    peak_mean = _mean(peaks)
    capture_all = round(100.0 * _mean(all_rets) / peak_mean, 1) if peak_mean else None
    summary: dict[str, Any] = {
        "variant_id": variant_id,
        "n_legs": n,
        "n_fired": nf,
        "fire_rate_pct": round(100.0 * nf / n, 1) if n else 0.0,
        "mean_all_legs_ret_pct": _mean(all_rets),
        "median_all_legs_ret_pct": _median(all_rets),
        "delta_vs_hold_pp": round(_mean(all_rets) - hold_mean, 3) if all_rets else None,
        "hold_baseline_mean_pct": hold_mean,
        "mean_peak_ret_pct": peak_mean,
        "portfolio_capture_pct_of_peak": capture_all,
    }
    if nf:
        summary.update(
            {
                "mean_exit_on_fired_pct": _mean([float(r["exit_ret_pct"]) for r in fired]),
                "mean_capture_on_fired_pct": _mean(
                    [float(r["capture_pct"]) for r in fired if r.get("capture_pct") is not None]
                ),
                "pct_fired_at_or_before_peak": round(
                    100.0
                    * sum(1 for r in fired if r.get("at_or_before_peak"))
                    / nf,
                    1,
                ),
            }
        )
    if any(r.get("r2_warn") for r in results):
        summary["r2_early_warn_rate_pct"] = round(
            100.0 * sum(1 for r in results if r.get("r2_warn")) / n, 1
        )
    return summary


def run_layer_combo_study(
    cache: dict[str, Any],
    *,
    r2: RuleParams = DEFAULT_R2,
    r7: RuleParams = DEFAULT_R7,
) -> dict[str, Any]:
    legs: list[Leg] = cache.get("legs") or []
    if not legs:
        raise ValueError("cache has no legs")

    modes: list[LayerMode] = [
        "hold",
        "R2_only",
        "R7_only",
        "first_fire",
        "R7_priority",
        "R2_warn_R7_exit",
        "R2_then_R7",
        "R2_and_R7",
    ]
    summaries: list[dict[str, Any]] = []
    for mode in modes:
        spec = LayerSpec(mode=mode, r2=r2, r7=r7)
        results = [evaluate_leg_layered(leg, spec) for leg in legs]
        summaries.append(_summarize_portfolio(legs, results, variant_id=mode))

    # Reference single-rule fired-only stats
    r2_fired = [_evaluate_leg(leg, r2) for leg in legs]
    r7_fired = [_evaluate_leg(leg, r7) for leg in legs]

    return {
        "r2_params": r2.variant_id,
        "r7_params": r7.variant_id,
        "layer_summaries": summaries,
        "r2_fired_only": _summarize_portfolio(
            legs,
            [
                {
                    **r,
                    "exit_ret_pct": r.get("exit_ret_pct") if r.get("fired") else leg["return_pct"],
                }
                for leg, r in zip(legs, r2_fired)
            ],
            variant_id="R2_fired_only_ref",
        ),
        "r7_fired_only": _summarize_portfolio(
            legs,
            [
                {
                    **r,
                    "exit_ret_pct": r.get("exit_ret_pct") if r.get("fired") else leg["return_pct"],
                }
                for leg, r in zip(legs, r7_fired)
            ],
            variant_id="R7_fired_only_ref",
        ),
    }


def _gap_value(snap: Snap, pair: GapPair, mode: GapMode, entry: Snap) -> float | None:
    field = GAP_FIELD[pair]
    gap = snap.get(field)
    if gap is None:
        return None
    g = float(gap)
    if mode == "abs":
        return g
    if mode == "expand":
        ent = entry.get(field)
        if ent is None:
            return None
        return g - float(ent)
    # pct_w20: gap as % of W20 MV
    w20 = snap.get("w20_mv")
    if w20 is None or float(w20) <= 0.05:
        return None
    return 100.0 * g / float(w20)


def _first_gap_cross_index(
    timeline: list[Snap],
    entry: Snap,
    *,
    pair: GapPair,
    mode: GapMode,
    threshold: float,
    consecutive: int = 2,
) -> int | None:
    if len(timeline) < 2:
        return None
    streak = 0
    for i in range(1, len(timeline)):
        val = _gap_value(timeline[i], pair, mode, entry)
        if val is not None and val >= threshold - 1e-9:
            streak += 1
            if streak >= consecutive:
                return i
        else:
            streak = 0
    return None


def evaluate_leg_gap_threshold(
    leg: Leg,
    *,
    pair: GapPair,
    mode: GapMode,
    threshold: float,
    consecutive: int = 2,
) -> dict[str, Any]:
    timeline = leg.get("timeline") or []
    entry = leg.get("entry_t0") or (timeline[0] if timeline else {})
    tri = _first_gap_cross_index(
        timeline, entry, pair=pair, mode=mode, threshold=threshold, consecutive=consecutive
    )
    variant_id = f"gap_{pair}_{mode}_{threshold:g}_c{consecutive}"
    return {**_leg_exit_result(leg, tri, source=variant_id), "variant_id": variant_id}


def _peak_gap_profile(legs: list[Leg]) -> dict[str, Any]:
    """Gap levels at price peak · by outcome."""
    fields = list(GAP_FIELD.values()) + ["spread_dist"]
    out: dict[str, Any] = {}

    def _collect(subset: list[Leg]) -> dict[str, Any]:
        rows: dict[str, list[float]] = {f: [] for f in fields}
        expand: dict[str, list[float]] = {
            f"{f}_expand": [] for f in GAP_FIELD.values()
        }
        pct_w20: dict[str, list[float]] = {
            f"{f}_pct_w20": [] for f in GAP_FIELD.values()
        }
        for leg in subset:
            timeline = leg.get("timeline") or []
            if not timeline:
                continue
            peak_i = int(leg.get("peak_frame_idx") or 0)
            if peak_i >= len(timeline):
                continue
            snap = timeline[peak_i]
            entry = leg.get("entry_t0") or timeline[0]
            for f in fields:
                v = snap.get(f)
                if v is not None:
                    rows[f].append(float(v))
            for pair, field in GAP_FIELD.items():
                ev = _gap_value(snap, pair, "expand", entry)
                pv = _gap_value(snap, pair, "pct_w20", entry)
                if ev is not None:
                    expand[f"{field}_expand"].append(ev)
                if pv is not None:
                    pct_w20[f"{field}_pct_w20"].append(pv)
        stats = {k: _mean(v) for k, v in rows.items() if v}
        stats.update({k: _mean(v) for k, v in expand.items() if v})
        stats.update({k: _mean(v) for k, v in pct_w20.items() if v})
        stats["n"] = len(subset)
        return stats

    wins = [l for l in legs if l.get("outcome") == "win"]
    losses = [l for l in legs if l.get("outcome") == "loss"]
    out["all"] = _collect(legs)
    out["winners"] = _collect(wins)
    out["losers"] = _collect(losses)
    top = sorted(legs, key=lambda l: float(l.get("peak_ret_pct") or 0), reverse=True)[
        : max(1, len(legs) // 4)
    ]
    out["top_quartile_peak"] = _collect(top)
    return out


def run_gap_expansion_sweep(
    legs: list[Leg],
    *,
    consecutive: int = 2,
) -> dict[str, Any]:
    """Sweep abs / expand / pct_w20 thresholds · portfolio mean return."""
    rows: list[dict[str, Any]] = []
    grids: list[tuple[GapPair, GapMode, tuple[float, ...]]] = [
        (p, "abs", ABS_GRID[p]) for p in GAP_FIELD
    ] + [(p, "expand", EXPAND_GRID[p]) for p in GAP_FIELD] + [
        (p, "pct_w20", PCT_W20_GRID[p]) for p in GAP_FIELD
    ]
    hold_mean = _mean([float(l["return_pct"]) for l in legs])

    for pair, mode, grid in grids:
        for thr in grid:
            results = [
                evaluate_leg_gap_threshold(
                    leg, pair=pair, mode=mode, threshold=thr, consecutive=consecutive
                )
                for leg in legs
            ]
            summary = _summarize_portfolio(
                legs,
                results,
                variant_id=f"{pair}_{mode}_{thr:g}",
            )
            summary["gap_pair"] = pair
            summary["gap_mode"] = mode
            summary["threshold"] = thr
            summary["consecutive"] = consecutive
            fired_snaps = [
                r["trigger_snap"]
                for r in results
                if r.get("fired") and r.get("trigger_snap")
            ]
            if fired_snaps:
                summary["mean_gap_w20_w5_at_trigger"] = _mean(
                    [float(s["gap_mv_w20_w5"]) for s in fired_snaps if s.get("gap_mv_w20_w5") is not None]
                )
                summary["mean_gap_w5_w3_at_trigger"] = _mean(
                    [float(s["gap_mv_w5_w3"]) for s in fired_snaps if s.get("gap_mv_w5_w3") is not None]
                )
            rows.append(summary)

    rows.sort(key=lambda r: -(float(r.get("mean_all_legs_ret_pct") or 0)))
    best_by_pair_mode: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = f"{r['gap_pair']}_{r['gap_mode']}"
        if key not in best_by_pair_mode:
            best_by_pair_mode[key] = r

    return {
        "hold_baseline_mean_pct": hold_mean,
        "n_legs": len(legs),
        "consecutive": consecutive,
        "peak_gap_profile": _peak_gap_profile(legs),
        "top_portfolio": rows[:20],
        "best_by_pair_mode": best_by_pair_mode,
        "all_rows": rows,
    }


def run_c18acc_kinematic_combo_gap_study(
    cache: dict[str, Any],
    *,
    r2: RuleParams = DEFAULT_R2,
    r7: RuleParams = DEFAULT_R7,
) -> dict[str, Any]:
    legs: list[Leg] = cache.get("legs") or []
    if not legs:
        raise ValueError("cache has no legs")
    layer = run_layer_combo_study(cache, r2=r2, r7=r7)
    gap = run_gap_expansion_sweep(legs)
    hold = _mean([float(l["return_pct"]) for l in legs])
    return {
        "schema": SCHEMA,
        "cache_schema": cache.get("schema"),
        "date_start": cache.get("date_start"),
        "date_end": cache.get("date_end"),
        "n_slots": cache.get("n_slots"),
        "n_legs": len(legs),
        "poll": cache.get("poll"),
        "hold_baseline": {
            "mean_all_legs_ret_pct": hold,
            "mean_peak_ret_pct": _mean([float(l["peak_ret_pct"]) for l in legs]),
            "win_rate_pct": round(
                100.0 * sum(1 for l in legs if float(l.get("return_pct") or 0) > 0) / len(legs),
                1,
            ),
        },
        "layer_combo": layer,
        "gap_expansion": gap,
        "leadlag_context": {
            "source": "abc_v3_f1_kinematic_leadlag_study_90d",
            "champion_w3_lead_peak_polls": 24.7,
            "champion_cascade_w3_w5_w20_pct": 95.8,
            "note": (
                "Gap expansion (W20 pulling from W5/W3) peaks near price peak; "
                "MV decline cascade w3>w5>w20 follows ~25 polls later on winners."
            ),
        },
    }


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def render_c18acc_kinematic_combo_gap_study_md(payload: dict[str, Any]) -> str:
    n = payload.get("n_legs", 0)
    hold = payload.get("hold_baseline") or {}
    layer = payload.get("layer_combo") or {}
    gap = payload.get("gap_expansion") or {}
    ll = payload.get("leadlag_context") or {}
    summaries = layer.get("layer_summaries") or []
    peak_prof = gap.get("peak_gap_profile") or {}
    top_gap = gap.get("top_portfolio") or []
    best_pm = gap.get("best_by_pair_mode") or {}

    lines = [
        "# C18acc · R2+R7 分層出場 + MV gap 擴大時機",
        "",
        f"區間：**{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"**{n}** legs · {payload.get('n_slots')} 槽 · poll {payload.get('poll')}",
        "",
        f"R2：`{layer.get('r2_params')}` · R7：`{layer.get('r7_params')}`",
        "",
        "## 完全不賣 baseline",
        "",
        f"- 全 leg 均報酬 **{_fmt(hold.get('mean_all_legs_ret_pct'))}%** · "
        f"peak oracle **{_fmt(hold.get('mean_peak_ret_pct'))}%** · "
        f"勝率 **{hold.get('win_rate_pct')}%**",
        "",
        "## R2 + R7 分層組合（全 leg portfolio 均報酬）",
        "",
        "| 模式 | 說明 | 觸發率 | 全 leg 均報酬 | Δ vs hold | 觸發 leg 賣出均 | 捕捉 peak% |",
        "|------|------|--------|-------------|-----------|---------------|-----------|",
    ]
    for s in summaries:
        mode = s.get("variant_id", "")
        label = LAYER_MODE_LABELS.get(str(mode), str(mode))
        lines.append(
            f"| **{mode}** | {label} | {s.get('fire_rate_pct')}% | "
            f"**{_fmt(s.get('mean_all_legs_ret_pct'))}%** | "
            f"{_fmt(s.get('delta_vs_hold_pp'))}pp | "
            f"{_fmt(s.get('mean_exit_on_fired_pct'))}% | "
            f"{_fmt(s.get('portfolio_capture_pct_of_peak'))}% |"
        )

    lines.extend(
        [
            "",
            "### 解讀",
            "",
            "- **R7_priority** / **R2_then_R7**：R7 作主出場 · R2 作預警或確認",
            "- **first_fire** / **R2_only** 若 Δ vs hold 為負 → 過早出場拖累組合",
            "- **R2_warn_R7_exit** 等同 R7_only（R2 只統計 early warn rate）",
            "",
            "## MV gap 擴大 · 最佳賣出門檻（全 leg portfolio）",
            "",
            "掃描：`abs` 絕對 gap · `expand` 自進場擴大 · `pct_w20` gap 佔 W20 MV%",
            "",
            "### Top 10 portfolio 均報酬",
            "",
            "| # | pair | mode | 門檻 | 觸發率 | 全 leg 均報酬 | Δ vs hold | 觸發時 gap_w20_w5 |",
            "|---|------|------|------|--------|-------------|-----------|------------------|",
        ]
    )
    for i, r in enumerate(top_gap[:10], 1):
        lines.append(
            f"| {i} | {r.get('gap_pair')} | {r.get('gap_mode')} | {r.get('threshold')} | "
            f"{r.get('fire_rate_pct')}% | **{_fmt(r.get('mean_all_legs_ret_pct'))}%** | "
            f"{_fmt(r.get('delta_vs_hold_pp'))}pp | "
            f"{_fmt(r.get('mean_gap_w20_w5_at_trigger'))} |"
        )

    lines.extend(["", "### 各 gap pair × mode 最佳門檻", ""])
    for key in sorted(best_pm.keys()):
        r = best_pm[key]
        lines.append(
            f"- **{key}** · 門檻 **{r.get('threshold')}** · "
            f"全 leg 均 **{_fmt(r.get('mean_all_legs_ret_pct'))}%** "
            f"(Δ {_fmt(r.get('delta_vs_hold_pp'))}pp) · 觸發 {r.get('fire_rate_pct')}%"
        )

    wpeak = peak_prof.get("winners") or {}
    tpeak = peak_prof.get("top_quartile_peak") or {}
    lines.extend(
        [
            "",
            "## 價格高點當下 gap 剖面（贏家 / 漲幅前 25%）",
            "",
            f"- **贏家 peak** · gap_w20_w5={_fmt(wpeak.get('gap_mv_w20_w5'))} · "
            f"expand={_fmt(wpeak.get('gap_mv_w20_w5_expand'))} · "
            f"pct_w20={_fmt(wpeak.get('gap_mv_w20_w5_pct_w20'))}% · "
            f"gap_w5_w3={_fmt(wpeak.get('gap_mv_w5_w3'))}",
            f"- **漲幅前 25% peak** · gap_w20_w5={_fmt(tpeak.get('gap_mv_w20_w5'))} · "
            f"expand={_fmt(tpeak.get('gap_mv_w20_w5_expand'))} · "
            f"pct_w20={_fmt(tpeak.get('gap_mv_w20_w5_pct_w20'))}% · "
            f"dist={_fmt(tpeak.get('spread_dist'))}",
            "",
            "> **Peak vs 觸發**：價格高點當下 gap 往往較小（W5/W3 仍贴近 W20）；"
            "R2/R7 觸發多在 **Pullback 段** gap 拉開（W20−W5 常 ≥6）。"
            "最佳賣點通常略 **晚於 price peak**、在短線 leg 相對 W20 擴大時。",
            "",
            "## Lead-lag 參考（ABC v3+f1 · 90d）",
            "",
            f"- Champion **W3 MV 領先 price peak ~{ll.get('champion_w3_lead_peak_polls')} polls** · "
            f"cascade w3>w5>w20 **{ll.get('champion_cascade_w3_w5_w20_pct')}%**",
            f"- {ll.get('note')}",
            "",
            "## 結論",
            "",
        ]
    )

    best_layer = max(
        (s for s in summaries if s.get("variant_id") != "hold"),
        key=lambda s: float(s.get("mean_all_legs_ret_pct") or 0),
        default={},
    )
    best_gap_row = top_gap[0] if top_gap else {}
    lines.append(
        f"- **最佳分層**：`{best_layer.get('variant_id')}` · "
        f"全 leg 均 **{_fmt(best_layer.get('mean_all_legs_ret_pct'))}%** "
        f"(Δ {_fmt(best_layer.get('delta_vs_hold_pp'))}pp)"
    )
    lines.append(
        f"- **最佳 gap 門檻**：`{best_gap_row.get('gap_pair')}` · "
        f"{best_gap_row.get('gap_mode')}≥{best_gap_row.get('threshold')} · "
        f"全 leg 均 **{_fmt(best_gap_row.get('mean_all_legs_ret_pct'))}%**"
    )
    w20_best = best_pm.get("w20_w5_abs") or {}
    lines.append(
        f"- **W20−W5 絕對 gap** 最佳：**≥{w20_best.get('threshold')}** · "
        f"均 **{_fmt(w20_best.get('mean_all_legs_ret_pct'))}%** · "
        f"peak 贏家 gap 仅 **{_fmt(wpeak.get('gap_mv_w20_w5'))}** · "
        f"R7 触发时 gap≈6.5（见 gap sweep）"
    )
    lines.append(
        f"- **R2_then_R7**（R2 预警→R7 确认）："
        f"全 leg 均 **{_fmt(next((s.get('mean_all_legs_ret_pct') for s in summaries if s.get('variant_id')=='R2_then_R7'), None))}%** · "
        f"触发 {next((s.get('fire_rate_pct') for s in summaries if s.get('variant_id')=='R2_then_R7'), '—')}%"
    )
    return "\n".join(lines)
