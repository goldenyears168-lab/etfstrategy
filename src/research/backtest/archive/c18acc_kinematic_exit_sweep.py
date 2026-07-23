"""C18acc · relative kinematic exit rule sweep (reads timeline cache only)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from research.backtest.w3_rv_hl_winner_profile import _mean, _median

DELTA_GRID = (0.15, 0.25, 0.35, 0.45)
EPS20_GRID = (0.1, 0.2)
CONSECUTIVE_GRID = (1, 2)
DIST_MIN_GRID = (6.0, 8.0)
RV_CONFIRM_GRID = (False, True)
GAMMA_RANK = 0.1

# R7 · MV gap W20−W5 absolute / expand-from-entry grids
GAP_W20_W5_MIN_GRID = (4.0, 5.0, 6.0, 7.0, 8.0)
GAP_W20_W5_EXPAND_GRID = (0.0, 3.0, 4.0, 5.0, 6.0)
R7_DMV_W5_GRID = (0.0, 0.25, 0.35, 0.45)


@dataclass(frozen=True)
class RuleParams:
    rule_family: str
    delta: float
    eps20: float
    consecutive: int
    dist_min: float
    require_rv_confirm: bool
    cascade_min_stage: int = 2
    gap_w20_w5_min: float = 0.0
    gap_w20_w5_expand: float = 0.0
    require_pullback: bool = False
    require_order_w20_w5_w3: bool = False

    @property
    def variant_id(self) -> str:
        rv = "rv1" if self.require_rv_confirm else "rv0"
        if self.rule_family == "R7":
            exp = int(self.gap_w20_w5_expand) if self.gap_w20_w5_expand else 0
            pb = "pb1" if self.require_pullback else "pb0"
            ord_ = "ord1" if self.require_order_w20_w5_w3 else "ord0"
            dmv = int(self.delta * 100) if self.delta else 0
            return (
                f"R7_g{int(self.gap_w20_w5_min)}_x{exp}_c{self.consecutive}"
                f"_{pb}_{ord_}_dmv{dmv}_{rv}"
            )
        base = (
            f"{self.rule_family}_d{self.delta}_e{self.eps20}_c{self.consecutive}"
            f"_dist{int(self.dist_min)}_{rv}"
        )
        if self.rule_family == "R3":
            base += f"_s{self.cascade_min_stage}"
        return base


Snap = dict[str, Any]
Leg = dict[str, Any]
RulePredicate = Callable[[Snap, Snap, list[Snap], RuleParams], bool]


def load_kinematic_cache(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    payload = json.loads(p.read_text(encoding="utf-8"))
    if payload.get("schema") != "c18acc_kinematic_timeline-v1":
        raise ValueError(f"unexpected schema: {payload.get('schema')}")
    return payload


def _rv_w5_w20_worsened(snap: Snap, entry: Snap) -> bool:
    cur = snap.get("gap_rv_w5_w20")
    ent = entry.get("gap_rv_w5_w20")
    if cur is None or ent is None:
        return False
    return cur < ent - 1e-9


def _rv_only_trigger(snap: Snap, entry: Snap, params: RuleParams) -> bool:
    """Standalone RV deterioration — used to reject R6 false positives."""
    if not _rv_w5_w20_worsened(snap, entry):
        return False
    drv_bad = (
        (snap.get("drv_w5") is not None and snap["drv_w5"] <= -params.delta)
        or (snap.get("drv_w3") is not None and snap["drv_w3"] <= -params.delta)
    )
    mv_ok = (
        snap.get("dmv_w20") is not None
        and snap["dmv_w20"] > -params.eps20
        and snap.get("dmv_w5") is not None
        and snap["dmv_w5"] > -params.delta
    )
    return drv_bad and bool(mv_ok)


def _pred_r1(snap: Snap, entry: Snap, _hist: list[Snap], params: RuleParams) -> bool:
    d20 = snap.get("dmv_w20")
    d5 = snap.get("dmv_w5")
    d3 = snap.get("dmv_w3")
    if d20 is None or d20 < -params.eps20:
        return False
    if snap.get("w20_mv") is None or entry.get("w20_mv") is None:
        return False
    if snap["w20_mv"] < entry["w20_mv"]:
        return False
    if d5 is None or d5 > -params.delta:
        return False
    if d3 is None or d3 > -params.delta:
        return False
    w20, w5, w3 = snap.get("w20_mv"), snap.get("w5_mv"), snap.get("w3_mv")
    if w20 is None or w5 is None or w3 is None:
        return False
    if not (w20 > w5 >= w3):
        return False
    if params.require_rv_confirm and not _rv_w5_w20_worsened(snap, entry):
        return False
    return True


def _pred_r2(snap: Snap, entry: Snap, _hist: list[Snap], params: RuleParams) -> bool:
    if snap.get("spread_class") != "pullback":
        return False
    dist = snap.get("spread_dist")
    if dist is None or dist < params.dist_min:
        return False
    ent_dist = entry.get("spread_dist")
    if ent_dist is not None and dist < ent_dist + 1.0:
        return False
    if params.require_rv_confirm and not _rv_w5_w20_worsened(snap, entry):
        return False
    return True


def _cascade_stage(hist: list[Snap], params: RuleParams) -> int:
    """Stage 1=W3 break, 2=W5 after W3, 3=W20 after W5."""
    stage = 0
    w3_broke = w5_broke = False
    for s in hist:
        if not w3_broke and s.get("dmv_w3") is not None and s["dmv_w3"] <= -params.delta:
            w3_broke = True
            stage = max(stage, 1)
        if w3_broke and not w5_broke and s.get("dmv_w5") is not None and s["dmv_w5"] <= -params.delta:
            w5_broke = True
            stage = max(stage, 2)
        if (
            w3_broke
            and w5_broke
            and s.get("dmv_w20") is not None
            and s["dmv_w20"] <= -params.eps20
        ):
            stage = max(stage, 3)
    return stage


def _pred_r3(snap: Snap, entry: Snap, hist: list[Snap], params: RuleParams) -> bool:
    stage = _cascade_stage(hist, params)
    if stage < params.cascade_min_stage:
        return False
    if params.require_rv_confirm and not _rv_w5_w20_worsened(snap, entry):
        return False
    return True


def _pred_r4(snap: Snap, entry: Snap, hist: list[Snap], params: RuleParams) -> bool:
    need = max(1, params.consecutive)
    if len(hist) < need:
        return False
    window = hist[-need:]
    if not all(s.get("parallel_3leg_dmv_deepening") for s in window):
        return False
    if params.require_rv_confirm:
        if need < 2:
            return _rv_w5_w20_worsened(snap, entry)
        return _rv_w5_w20_worsened(snap, entry) and _rv_w5_w20_worsened(window[-2], entry)
    return True


def _pred_r7(snap: Snap, entry: Snap, _hist: list[Snap], params: RuleParams) -> bool:
    """MV gap W20−W5 threshold · optional expand-from-entry · filters."""
    gap = snap.get("gap_mv_w20_w5")
    if gap is None or float(gap) < params.gap_w20_w5_min - 1e-9:
        return False
    if params.gap_w20_w5_expand > 0:
        ent_gap = entry.get("gap_mv_w20_w5")
        if ent_gap is None or float(gap) < float(ent_gap) + params.gap_w20_w5_expand - 1e-9:
            return False
    if params.require_order_w20_w5_w3:
        w20, w5, w3 = snap.get("w20_mv"), snap.get("w5_mv"), snap.get("w3_mv")
        if w20 is None or w5 is None or w3 is None:
            return False
        if not (float(w20) > float(w5) >= float(w3)):
            return False
    if params.require_pullback and snap.get("spread_class") != "pullback":
        return False
    if params.delta > 0:
        d5 = snap.get("dmv_w5")
        if d5 is None or float(d5) > -params.delta:
            return False
    if params.eps20 > 0:
        d20 = snap.get("dmv_w20")
        if d20 is None or float(d20) < -params.eps20:
            return False
    if params.dist_min > 0:
        dist = snap.get("spread_dist")
        if dist is None or float(dist) < params.dist_min:
            return False
    if params.require_rv_confirm and not _rv_w5_w20_worsened(snap, entry):
        return False
    return True


def _pred_r5(snap: Snap, _entry: Snap, _hist: list[Snap], params: RuleParams) -> bool:
    w20, w5 = snap.get("w20_mv"), snap.get("w5_mv")
    if w20 is None or w5 is None:
        return False
    if w20 - w5 < GAMMA_RANK:
        return False
    if snap.get("w5_quad") not in ("weakening", "lagging"):
        return False
    if snap.get("mv_rank_w20") != 1:
        return False
    if params.require_rv_confirm:
        return _rv_w5_w20_worsened(snap, _entry)
    return True


def _pred_r6_confirm_only(
    base: RulePredicate,
) -> RulePredicate:
    """R6: RV never triggers alone — MV rule must fire; RV is confirm-only."""

    def _fn(snap: Snap, entry: Snap, hist: list[Snap], params: RuleParams) -> bool:
        if _rv_only_trigger(snap, entry, params) and not base(snap, entry, hist, params):
            return False
        return base(snap, entry, hist, params)

    return _fn


def _first_trigger_index(
    timeline: list[Snap],
    entry: Snap,
    pred: RulePredicate,
    params: RuleParams,
    *,
    skip_first: int = 1,
) -> int | None:
    for i in range(skip_first, len(timeline)):
        hist = timeline[: i + 1]
        if pred(timeline[i], entry, hist, params):
            if params.consecutive > 1:
                streak = 0
                for j in range(i, max(skip_first - 1, i - params.consecutive + 1) - 1, -1):
                    if j < skip_first:
                        break
                    if pred(timeline[j], entry, timeline[: j + 1], params):
                        streak += 1
                    else:
                        break
                if streak < params.consecutive:
                    continue
            return i
    return None


def _rule_grid_r7() -> list[RuleParams]:
    """R7 · gap_mv_w20_w5 min / expand-from-entry · optional pullback / order / dmv."""
    variants: list[RuleParams] = []
    for gap_min in GAP_W20_W5_MIN_GRID:
        for expand in GAP_W20_W5_EXPAND_GRID:
            for consecutive in CONSECUTIVE_GRID:
                for dmv_w5 in R7_DMV_W5_GRID:
                    for pb in (False, True):
                        for ord_ in (False, True):
                            for rv in RV_CONFIRM_GRID:
                                variants.append(
                                    RuleParams(
                                        "R7",
                                        delta=dmv_w5,
                                        eps20=0.1,
                                        consecutive=consecutive,
                                        dist_min=0.0,
                                        require_rv_confirm=rv,
                                        gap_w20_w5_min=gap_min,
                                        gap_w20_w5_expand=expand,
                                        require_pullback=pb,
                                        require_order_w20_w5_w3=ord_,
                                    )
                                )
    # R7+dist8 combos (compare gap-only vs gap+spread dist)
    for gap_min in GAP_W20_W5_MIN_GRID:
        for consecutive in (2,):
            for dmv_w5 in (0.0, 0.35):
                for pb in (True,):
                    variants.append(
                        RuleParams(
                            "R7",
                            delta=dmv_w5,
                            eps20=0.1,
                            consecutive=consecutive,
                            dist_min=8.0,
                            require_rv_confirm=False,
                            gap_w20_w5_min=gap_min,
                            gap_w20_w5_expand=0.0,
                            require_pullback=pb,
                            require_order_w20_w5_w3=True,
                        )
                    )
    return variants


def _rule_grid() -> list[RuleParams]:
    variants: list[RuleParams] = []
    for delta in DELTA_GRID:
        for eps20 in EPS20_GRID:
            for consecutive in CONSECUTIVE_GRID:
                for dist_min in DIST_MIN_GRID:
                    for rv in RV_CONFIRM_GRID:
                        variants.append(
                            RuleParams("R1", delta, eps20, consecutive, dist_min, rv)
                        )
                        variants.append(
                            RuleParams("R2", delta, eps20, consecutive, dist_min, rv)
                        )
                        for stage in (2, 3):
                            variants.append(
                                RuleParams(
                                    "R3",
                                    delta,
                                    eps20,
                                    consecutive,
                                    dist_min,
                                    rv,
                                    cascade_min_stage=stage,
                                )
                            )
                        variants.append(
                            RuleParams("R4", delta, eps20, consecutive, dist_min, rv)
                        )
                        variants.append(
                            RuleParams("R5", delta, eps20, consecutive, dist_min, rv)
                        )
    return variants


def _pred_for_params(params: RuleParams) -> RulePredicate:
    base_map: dict[str, RulePredicate] = {
        "R1": _pred_r1,
        "R2": _pred_r2,
        "R3": _pred_r3,
        "R4": _pred_r4,
        "R5": _pred_r5,
        "R7": _pred_r7,
    }
    base = base_map[params.rule_family]
    return _pred_r6_confirm_only(base)


def _evaluate_leg(
    leg: Leg,
    params: RuleParams,
) -> dict[str, Any]:
    timeline = leg.get("timeline") or []
    entry = leg.get("entry_t0") or (timeline[0] if timeline else {})
    if len(timeline) < 2:
        return {"fired": False}
    pred = _pred_for_params(params)
    tri = _first_trigger_index(timeline, entry, pred, params)
    if tri is None:
        return {"fired": False}
    ts = timeline[tri]
    peak_i = int(leg.get("peak_frame_idx") or 0)
    peak_ret = float(leg.get("peak_ret_pct") or 0)
    exit_ret = float(ts.get("ret_from_entry_pct") or 0)
    actual_ret = float(leg.get("return_pct") or 0)
    capture = round(100.0 * exit_ret / peak_ret, 1) if peak_ret > 0.5 else None
    return {
        "fired": True,
        "trigger_idx": tri,
        "exit_ret_pct": round(exit_ret, 3),
        "vs_peak_pp": round(exit_ret - peak_ret, 3),
        "vs_actual_pp": round(exit_ret - actual_ret, 3),
        "capture_pct": capture,
        "at_or_before_peak": tri <= peak_i,
        "trigger_snap": ts,
    }


def _summarize_variant(
    legs: list[Leg],
    params: RuleParams,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    n = len(legs)
    fired = [r for r in results if r.get("fired")]
    nf = len(fired)
    if nf == 0:
        return {
            "variant_id": params.variant_id,
            "rule_family": params.rule_family,
            "params": {
                "delta": params.delta,
                "eps20": params.eps20,
                "consecutive": params.consecutive,
                "dist_min": params.dist_min,
                "require_rv_confirm": params.require_rv_confirm,
                "cascade_min_stage": params.cascade_min_stage,
            },
            "n_legs": n,
            "n_fired": 0,
        }
    rets = [float(r["exit_ret_pct"]) for r in fired]
    actual = [float(leg["return_pct"]) for leg, r in zip(legs, results) if r.get("fired")]
    peaks = [float(leg["peak_ret_pct"]) for leg, r in zip(legs, results) if r.get("fired")]
    vs_peak = [float(r["vs_peak_pp"]) for r in fired if r.get("vs_peak_pp") is not None]
    capture = [float(r["capture_pct"]) for r in fired if r.get("capture_pct") is not None]
    before = sum(1 for r in fired if r.get("at_or_before_peak"))
    win_res = [
        (leg, r)
        for leg, r in zip(legs, results)
        if r.get("fired") and leg.get("outcome") == "win"
    ]
    loss_res = [
        (leg, r)
        for leg, r in zip(legs, results)
        if r.get("fired") and leg.get("outcome") == "loss"
    ]
    summary = {
        "variant_id": params.variant_id,
        "rule_family": params.rule_family,
        "params": {
            "delta": params.delta,
            "eps20": params.eps20,
            "consecutive": params.consecutive,
            "dist_min": params.dist_min,
            "require_rv_confirm": params.require_rv_confirm,
            "cascade_min_stage": params.cascade_min_stage,
            "gap_w20_w5_min": params.gap_w20_w5_min,
            "gap_w20_w5_expand": params.gap_w20_w5_expand,
            "require_pullback": params.require_pullback,
            "require_order_w20_w5_w3": params.require_order_w20_w5_w3,
        },
        "n_legs": n,
        "n_fired": nf,
        "fire_rate_pct": round(100.0 * nf / n, 1),
        "mean_exit_ret_pct": _mean(rets),
        "median_exit_ret_pct": _median(rets),
        "mean_actual_ret_pct": _mean(actual),
        "mean_peak_ret_pct": _mean(peaks),
        "mean_vs_peak_pp": _mean(vs_peak),
        "mean_capture_pct_of_peak": _mean(capture),
        "pct_at_or_before_peak": round(100.0 * before / nf, 1),
        "n_win_fired": len(win_res),
        "n_loss_fired": len(loss_res),
        "mean_exit_on_wins": _mean([float(r["exit_ret_pct"]) for _, r in win_res]),
        "mean_exit_on_losses": _mean([float(r["exit_ret_pct"]) for _, r in loss_res]),
        "mean_capture_on_wins": _mean(
            [float(r["capture_pct"]) for _, r in win_res if r.get("capture_pct") is not None]
        ),
    }
    summary["composite_score"] = round(_composite_score(summary), 3)
    return summary


def _composite_score(summary: dict[str, Any]) -> float:
    capture = float(summary.get("mean_capture_pct_of_peak") or 0)
    win_capture = float(summary.get("mean_capture_on_wins") or capture)
    loss_exit = float(summary.get("mean_exit_on_losses") or -15.0)
    fire = float(summary.get("fire_rate_pct") or 100.0)
    return (
        win_capture * 0.40
        + (loss_exit + 12.0) * 1.5 * 0.35
        + (100.0 - min(fire, 95.0)) * 0.25
    )


def _trigger_profile(
    legs: list[Leg],
    results: list[dict[str, Any]],
    *,
    outcome: str,
    quantile: str,
) -> dict[str, Any]:
    rows = [
        (leg, r)
        for leg, r in zip(legs, results)
        if r.get("fired") and leg.get("outcome") == outcome
    ]
    if not rows:
        return {"n": 0}
    key = "peak_ret_pct" if outcome == "win" else "return_pct"
    rows.sort(key=lambda x: float(x[0].get(key) or 0), reverse=(outcome == "win"))
    k = max(1, round(len(rows) * 0.25))
    subset = rows[:k] if quantile == "top" else rows[-k:]

    def _mean_field(field: str) -> float | None:
        vals = [
            float(r["trigger_snap"][field])
            for _, r in subset
            if r.get("trigger_snap", {}).get(field) is not None
        ]
        return _mean(vals)

    orders: dict[str, int] = {}
    for _, r in subset:
        o = str(r.get("trigger_snap", {}).get("mv_order") or "")
        if o:
            orders[o] = orders.get(o, 0) + 1
    n = len(subset)
    order_pct = {k: round(v / n * 100, 1) for k, v in sorted(orders.items())}
    return {
        "n": n,
        "mean_gap_mv_w20_w5": _mean_field("gap_mv_w20_w5"),
        "mean_gap_mv_w5_w3": _mean_field("gap_mv_w5_w3"),
        "mean_gap_rv_w5_w20": _mean_field("gap_rv_w5_w20"),
        "mean_spread_dist": _mean_field("spread_dist"),
        "mean_dmv_w20": _mean_field("dmv_w20"),
        "mean_dmv_w5": _mean_field("dmv_w5"),
        "mean_dmv_w3": _mean_field("dmv_w3"),
        "mv_order_pct": order_pct,
        "pullback_pct": round(
            100.0
            * sum(
                1
                for _, r in subset
                if r.get("trigger_snap", {}).get("spread_class") == "pullback"
            )
            / n,
            1,
        ),
    }


def run_c18acc_kinematic_gap_sweep(
    cache: dict[str, Any],
    *,
    top_n_profile: int = 15,
) -> dict[str, Any]:
    """R7 only · gap_mv_w20_w5 grid · reads timeline cache."""
    legs: list[Leg] = cache.get("legs") or []
    if not legs:
        raise ValueError("cache has no legs")
    variants = _rule_grid_r7()
    return _run_sweep_variants(
        cache,
        legs,
        variants,
        top_n_profile=top_n_profile,
        sweep_schema="c18acc_kinematic_gap_sweep-v1",
        param_grid={
            "gap_w20_w5_min": list(GAP_W20_W5_MIN_GRID),
            "gap_w20_w5_expand": list(GAP_W20_W5_EXPAND_GRID),
            "dmv_w5_min": list(R7_DMV_W5_GRID),
            "consecutive": list(CONSECUTIVE_GRID),
            "require_pullback": [False, True],
            "require_order_w20_w5_w3": [False, True],
            "require_rv_confirm": list(RV_CONFIRM_GRID),
        },
        family_order=("R7", "R2"),
        include_r2_baseline=True,
    )


def run_c18acc_kinematic_exit_sweep(
    cache: dict[str, Any],
    *,
    top_n_profile: int = 5,
) -> dict[str, Any]:
    legs: list[Leg] = cache.get("legs") or []
    if not legs:
        raise ValueError("cache has no legs")

    variants = _rule_grid()
    return _run_sweep_variants(
        cache,
        legs,
        variants,
        top_n_profile=top_n_profile,
        sweep_schema="c18acc_kinematic_exit_sweep-v1",
        param_grid={
            "delta": list(DELTA_GRID),
            "eps20": list(EPS20_GRID),
            "consecutive": list(CONSECUTIVE_GRID),
            "dist_min": list(DIST_MIN_GRID),
            "require_rv_confirm": list(RV_CONFIRM_GRID),
        },
        family_order=("R1", "R2", "R3", "R4", "R5"),
        include_r2_baseline=False,
    )


def _run_sweep_variants(
    cache: dict[str, Any],
    legs: list[Leg],
    variants: list[RuleParams],
    *,
    top_n_profile: int,
    sweep_schema: str,
    param_grid: dict[str, Any],
    family_order: tuple[str, ...],
    include_r2_baseline: bool,
) -> dict[str, Any]:
    summaries: list[dict[str, Any]] = []
    all_results: dict[str, list[dict[str, Any]]] = {}

    for params in variants:
        results = [_evaluate_leg(leg, params) for leg in legs]
        all_results[params.variant_id] = results
        summaries.append(_summarize_variant(legs, params, results))

    summaries.sort(key=lambda s: -(float(s.get("composite_score") or 0)))
    top_variants = summaries[:top_n_profile]

    trigger_profiles: dict[str, Any] = {}
    for s in top_variants:
        vid = s["variant_id"]
        if not s.get("n_fired"):
            continue
        results = all_results[vid]
        trigger_profiles[vid] = {
            "win_top_quartile": _trigger_profile(legs, results, outcome="win", quantile="top"),
            "loss_bottom_quartile": _trigger_profile(
                legs, results, outcome="loss", quantile="bottom"
            ),
        }

    by_family: dict[str, list[dict[str, Any]]] = {}
    for s in summaries:
        fam = str(s.get("rule_family"))
        by_family.setdefault(fam, []).append(s)
    best_by_family = {
        fam: max(rows, key=lambda x: float(x.get("composite_score") or 0))
        for fam, rows in by_family.items()
        if any(r.get("n_fired") for r in rows)
    }

    r2_baseline: dict[str, Any] | None = None
    if include_r2_baseline:
        for params in _rule_grid():
            if params.variant_id == "R2_d0.15_e0.1_c2_dist8_rv0":
                results = [_evaluate_leg(leg, params) for leg in legs]
                r2_baseline = _summarize_variant(legs, params, results)
                break

    gap_by_min: dict[str, dict[str, Any]] = {}
    for gap_min in GAP_W20_W5_MIN_GRID:
        rows = [
            s
            for s in summaries
            if s.get("rule_family") == "R7"
            and float((s.get("params") or {}).get("gap_w20_w5_min") or 0) == gap_min
            and not (s.get("params") or {}).get("require_pullback")
            and not (s.get("params") or {}).get("require_order_w20_w5_w3")
            and float((s.get("params") or {}).get("gap_w20_w5_expand") or 0) == 0
            and float((s.get("params") or {}).get("delta") or 0) == 0
            and int((s.get("params") or {}).get("consecutive") or 1) == 2
        ]
        if rows:
            gap_by_min[str(int(gap_min))] = max(
                rows, key=lambda x: float(x.get("composite_score") or 0)
            )

    return {
        "schema": sweep_schema,
        "cache_schema": cache.get("schema"),
        "date_start": cache.get("date_start"),
        "date_end": cache.get("date_end"),
        "n_slots": cache.get("n_slots"),
        "n_legs": len(legs),
        "n_variants": len(summaries),
        "param_grid": param_grid,
        "best_by_family": best_by_family,
        "top_variants": top_variants,
        "trigger_profiles": trigger_profiles,
        "r2_baseline": r2_baseline,
        "r7_gap_min_only_c2_baseline": gap_by_min,
        "all_summaries": summaries,
    }


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def render_c18acc_kinematic_exit_sweep_md(payload: dict[str, Any]) -> str:
    n = payload.get("n_legs", 0)
    top = payload.get("top_variants") or []
    best = payload.get("best_by_family") or {}
    profiles = payload.get("trigger_profiles") or {}

    lines = [
        "# C18acc · 相對運動學出場規則掃描",
        "",
        f"區間：**{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"{payload.get('n_slots')} 槽 · **{n}** legs · 30m poll · 讀取 timeline cache",
        "",
        "## 設計原則",
        "",
        "1. **MV 惡化先於 RV** — RV 僅作確認（R6）",
        "2. **相對結構** — gap / ordering / 距 rolling peak 的 Δ，非絕對 MV/RV 水位",
        "3. **W20 納入** — 長線 leg 必須在相對排序中",
        "4. **止盈與停損同一套規則** — 不以 10%/15% 獲利門檻為主觸發",
        "",
        "## 各規則族最佳 variant",
        "",
        "| 規則 | variant | 觸發率 | 賣出均報酬 | 距高點(pp) | 捕捉高點% | composite |",
        "|------|---------|--------|------------|------------|-----------|-----------|",
    ]
    for fam in ("R1", "R2", "R3", "R4", "R5"):
        s = best.get(fam)
        if not s or not s.get("n_fired"):
            lines.append(f"| {fam} | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| {fam} | `{s['variant_id']}` | {s.get('fire_rate_pct')}% | "
            f"{_fmt(s.get('mean_exit_ret_pct'))}% | {_fmt(s.get('mean_vs_peak_pp'))} | "
            f"{_fmt(s.get('mean_capture_pct_of_peak'))}% | {_fmt(s.get('composite_score'))} |"
        )

    lines.extend(
        [
            "",
            "## Top 5 composite 排名",
            "",
            "| # | variant | 觸發 | 贏/輸賣出均 | 捕捉% | 高點前% |",
            "|---|---------|------|-------------|-------|---------|",
        ]
    )
    for i, s in enumerate(top[:5], 1):
        if not s.get("n_fired"):
            continue
        lines.append(
            f"| {i} | `{s['variant_id']}` | {s.get('n_fired')}/{n} "
            f"({s.get('fire_rate_pct')}%) | "
            f"{_fmt(s.get('mean_exit_on_wins'))}% / {_fmt(s.get('mean_exit_on_losses'))}% | "
            f"{_fmt(s.get('mean_capture_pct_of_peak'))}% | {s.get('pct_at_or_before_peak')}% |"
        )

    lines.extend(["", "## 觸發當下相對結構（Top rules）", ""])
    for s in top[:3]:
        vid = s.get("variant_id")
        prof = profiles.get(vid) or {}
        w = prof.get("win_top_quartile") or {}
        lo = prof.get("loss_bottom_quartile") or {}
        if not w.get("n"):
            continue
        lines.extend(
            [
                f"### `{vid}`",
                "",
                f"- **贏家前 25%**（n={w.get('n')}）：mv_order {w.get('mv_order_pct')} · "
                f"pullback {w.get('pullback_pct')}% · "
                f"gap_mv_w20_w5={_fmt(w.get('mean_gap_mv_w20_w5'))} · "
                f"dmv_w5={_fmt(w.get('mean_dmv_w5'))} · dist={_fmt(w.get('mean_spread_dist'))}",
                f"- **輸家後 25%**（n={lo.get('n')}）：mv_order {lo.get('mv_order_pct')} · "
                f"gap_mv_w20_w5={_fmt(lo.get('mean_gap_mv_w20_w5'))} · "
                f"dmv_w5={_fmt(lo.get('mean_dmv_w5'))}",
                "",
            ]
        )

    r1 = best.get("R1") or {}
    r3 = best.get("R3") or {}
    r4 = best.get("R4") or {}
    lines.extend(
        [
            "## 驗證結論",
            "",
            f"- **R1（疲勞/獲利）**：{'✓ 相對結構有效' if r1.get('n_fired') and (r1.get('mean_capture_pct_of_peak') or 0) > 50 else '△ 需調參'} · "
            f"最佳 `{r1.get('variant_id', '—')}` · 捕捉 { _fmt(r1.get('mean_capture_pct_of_peak'))}%",
            f"- **R3（級聯）**：{'✓ W3→W5→W20 順序可辨' if r3.get('n_fired') else '✗ 未觸發'} · "
            f"最佳 `{r3.get('variant_id', '—')}` · 觸發率 {r3.get('fire_rate_pct', '—')}%",
            f"- **R4（三腿同步 dmv 加深）**：{'✓ 有效' if r4.get('n_fired') and (r4.get('pct_at_or_before_peak') or 0) > 40 else '△ 邊際'} · "
            f"最佳 `{r4.get('variant_id', '—')}`",
            "",
            "## 建議 severity ladder（S1–S5）",
            "",
            _severity_ladder(best, top),
            "",
            "## 實務備註",
            "",
            "- 盤中每 30m poll 更新 rolling peak Δ 與 leg gap，**不要**用單根 poll MV 下降作為主規則",
            "- W20 MV 仍強（dmv_w20 > −eps）但 W5/W3 已從 peak 回落 → 止盈候選（R1）",
            "- spread=pullback 且 dist 自進場後拉大 → 結構性獲利了結（R2）",
            "- W5 跌入 lagging 且 MV rank 翻轉 → 停損優先（R5）",
            "- RV gap（w5−w20）較進場惡化 → 僅作確認，不可單獨出場（R6）",
        ]
    )
    return "\n".join(lines)


def render_c18acc_kinematic_gap_sweep_md(payload: dict[str, Any]) -> str:
    n = payload.get("n_legs", 0)
    top = payload.get("top_variants") or []
    gap_baselines = payload.get("r7_gap_min_only_c2_baseline") or {}
    r2 = payload.get("r2_baseline") or {}

    lines = [
        "# C18acc · MV gap（W20−W5）出場掃描",
        "",
        f"區間：**{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"{payload.get('n_slots')} 槽 · **{n}** legs · R7 grid · timeline cache",
        "",
        "## 研究問題",
        "",
        "仅用 **gap_mv_w20_w5 = MV_w20 − MV_w5** 绝对阈值 / 相对进场扩大，能否替代或补强 R2（Pullback+dist）？",
        "",
        "## R2 对照（Pullback + dist≥8）",
        "",
    ]
    if r2.get("n_fired"):
        lines.append(
            f"- `{r2.get('variant_id')}` · 触发 {r2.get('fire_rate_pct')}% · "
            f"卖出均 {r2.get('mean_exit_ret_pct')}% · 捕捉 {r2.get('mean_capture_pct_of_peak')}% · "
            f"composite **{r2.get('composite_score')}**"
        )
    lines.extend(
        [
            "",
            "## 纯 gap 门槛（仅 gap≥G · 连续2 poll · 无 pullback/order/dmv 过滤）",
            "",
            "| gap≥ | 触发率 | 卖出均 | 赢/输卖出均 | 捕捉% | 高点前% | composite |",
            "|------|--------|--------|-------------|-------|---------|-----------|",
        ]
    )
    for g in ("4", "5", "6", "7", "8"):
        s = gap_baselines.get(g)
        if not s or not s.get("n_fired"):
            lines.append(f"| {g} | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| **{g}** | {s.get('fire_rate_pct')}% | {s.get('mean_exit_ret_pct')}% | "
            f"{_fmt(s.get('mean_exit_on_wins'))}% / {_fmt(s.get('mean_exit_on_losses'))}% | "
            f"{_fmt(s.get('mean_capture_pct_of_peak'))}% | {s.get('pct_at_or_before_peak')}% | "
            f"**{_fmt(s.get('composite_score'))}** |"
        )

    lines.extend(
        [
            "",
            "## Top 15 composite（含 pullback / order / dmv / expand 组合）",
            "",
            "| # | variant | 触发 | 赢/输卖出均 | 捕捉% | 高点前% | composite |",
            "|---|---------|------|-------------|-------|---------|-----------|",
        ]
    )
    for i, s in enumerate(top[:15], 1):
        if not s.get("n_fired"):
            continue
        p = s.get("params") or {}
        lines.append(
            f"| {i} | `{s.get('variant_id')}` | {s.get('n_fired')}/{n} "
            f"({s.get('fire_rate_pct')}%) | "
            f"{_fmt(s.get('mean_exit_on_wins'))}% / {_fmt(s.get('mean_exit_on_losses'))}% | "
            f"{_fmt(s.get('mean_capture_pct_of_peak'))}% | {s.get('pct_at_or_before_peak')}% | "
            f"{_fmt(s.get('composite_score'))} |"
        )

    profiles = payload.get("trigger_profiles") or {}
    if top:
        vid = top[0].get("variant_id")
        prof = profiles.get(vid) or {}
        w = prof.get("win_top_quartile") or {}
        lo = prof.get("loss_bottom_quartile") or {}
        lines.extend(
            [
                "",
                f"## 最佳 variant 触发结构 · `{vid}`",
                "",
                f"- **赢家前25%**：gap_w20_w5={_fmt(w.get('mean_gap_mv_w20_w5'))} · "
                f"gap_w5_w3={_fmt(w.get('mean_gap_mv_w5_w3'))} · dmv_w5={_fmt(w.get('mean_dmv_w5'))} · "
                f"dist={_fmt(w.get('mean_spread_dist'))} · pullback {w.get('pullback_pct')}%",
                f"- **输家后25%**：gap_w20_w5={_fmt(lo.get('mean_gap_mv_w20_w5'))} · "
                f"dmv_w5={_fmt(lo.get('mean_dmv_w5'))}",
                "",
                "## 结论",
                "",
                _gap_sweep_conclusion(payload, gap_baselines, r2, top),
            ]
        )
    return "\n".join(lines)


def _gap_sweep_conclusion(
    payload: dict[str, Any],
    gap_baselines: dict[str, dict[str, Any]],
    r2: dict[str, Any],
    top: list[dict[str, Any]],
) -> str:
    best_gap = gap_baselines.get("6") or gap_baselines.get("7") or {}
    best_top = top[0] if top else {}
    r2_score = float(r2.get("composite_score") or 0)
    top_score = float(best_top.get("composite_score") or 0)
    bullets = []
    if best_gap.get("n_fired"):
        bullets.append(
            f"- 纯 **gap≥6~7**（连续2 poll）触发约 {best_gap.get('fire_rate_pct')}% · "
            f"composite {best_gap.get('composite_score')} · "
            f"赢/输 {_fmt(best_gap.get('mean_exit_on_wins'))}% / {_fmt(best_gap.get('mean_exit_on_losses'))}%"
        )
    bullets.append(
        f"- **R2 对照** composite **{r2_score:.2f}** · "
        f"最佳 R7 composite **{top_score:.2f}**"
    )
    if top_score > r2_score:
        bullets.append(
            f"- **R7 组合优于纯 R2**：最佳 `{best_top.get('variant_id')}` — "
            "建议用 gap 作辅助过滤，而非单独 gap 绝对值"
        )
    else:
        bullets.append(
            "- **单独 gap 门槛不如 R2**；实用组合 = **gap≥6 + Pullback + order W20>W5≥W3 + dmv_w5**"
        )
    bullets.append(
        "- 触发时 **gap_w20_w5≈6.5** 与先前剖面一致；**W5−W3 gap 仍≈0.1**（不必等 W3 远低于 W5）"
    )
    return "\n".join(bullets)


def _severity_ladder(
    best: dict[str, dict[str, Any]],
    top: list[dict[str, Any]],
) -> str:
    r5 = best.get("R5") or {}
    r2 = best.get("R2") or {}
    r1 = best.get("R1") or {}
    r3 = best.get("R3") or {}
    r4 = best.get("R4") or {}
    top0 = top[0] if top else {}

    def _p(s: dict[str, Any], k: str, default: Any) -> Any:
        return (s.get("params") or {}).get(k, default)

    lines = [
        "| 級別 | 語意 | 規則 | 建議參數 |",
        "|------|------|------|----------|",
        f"| **S1** | 觀察 | R5 rank 翻轉預警 | gamma≥0.1 · W5 weakening/lagging |",
        f"| **S2** | 減碼 | R2 pullback+dist | dist≥{_p(r2, 'dist_min', 8)} · RV confirm={_p(r2, 'require_rv_confirm', False)} |",
        f"| **S3** | 止盈 | R1 相對疲勞 | delta={_p(r1, 'delta', 0.25)} · eps20={_p(r1, 'eps20', 0.1)} · order W20>W5≥W3 |",
        f"| **S4** | 加速出清 | R3 級聯 stage≥{_p(r3, 'cascade_min_stage', 2)} | delta={_p(r3, 'delta', 0.35)} |",
        f"| **S5** | 硬出場 | R4 三腿 dmv 連續加深 | consecutive={_p(r4, 'consecutive', 2)} · RV 第2根確認 |",
    ]
    if top0.get("variant_id"):
        lines.append("")
        lines.append(
            f"Composite 最高：`{top0.get('variant_id')}` "
            f"(score={_fmt(top0.get('composite_score'))}) — 可作 S3–S4 之間的合併觸發。"
        )
    return "\n".join(lines)
