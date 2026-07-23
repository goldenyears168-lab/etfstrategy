"""ABC v3+F1 · multisignal shape library study (RP-5).

Enumerates ≥15 candidate entry shapes, runs the RP-6 three-stage validation
pipeline (Discovery → Validation → Holdout), applies BH FDR on Validation,
and reports the deduplicated union of shapes that pass Holdout.

Each shape is an independent entry filter on raw ABC-V3-F1 first-trigger legs;
forward return uses the champion TP-only exit (same as entry multifactor sweep).
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean, pstdev
from typing import Any

from project_config import DEFAULT_ETF_CODES
from research.backtest.abc_v3_f1_entry_factor_scan import (
    attach_normalized_gate_features,
    build_stock_daily_gap_rv_series,
)
from research.backtest.abc_v3_f1_entry_gate_tp_sweep import collect_abc_v3_f1_raw_legs
from research.backtest.abc_v3_f1_entry_multifactor_tp_sweep import (
    BASE_GAP_BAND,
    BASE_MIN_POLL_IDX,
    CHAMPION_COMBO,
    _leg_entry_trade_dict,
    _matches_base_gap_gate,
)
from research.backtest.abc_v3_f1_entry_structure_sweep import GapSweetBand
from research.backtest.archive.abc_v3_f1_kinematic_tp_sl_study import evaluate_leg_tp_sl
from research.backtest.abc_v3_f1_shape_validation_framework import (
    _split_dates_three_way,
    apply_fdr_correction,
    min_n_for_power,
    one_sample_t_test,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.w3_rv_hl_winner_profile import _trade_matches_filter

SCHEMA = "abc_v3_f1_shape_library-v1"

TIGHT_GAP_BAND = GapSweetBand(
    gap_w20_w5_min=3.0,
    gap_w20_w5_max=6.0,
    gap_w5_w3_min=-0.5,
    gap_w5_w3_max=1.0,
)

MIDHIGH_RAW_GAP = (4.0, 8.0)


@dataclass(frozen=True)
class ShapeDef:
    id: str
    label: str
    category: str
    matcher: Callable[[dict[str, Any]], bool]
    definition: dict[str, Any] = field(default_factory=dict)


def _poll_ok(leg: dict[str, Any], *, min_poll: int) -> bool:
    poll = leg.get("gate_poll_idx")
    return poll is not None and int(poll) >= min_poll


def _gap_band_ok(leg: dict[str, Any], band: GapSweetBand, *, min_poll: int = 0) -> bool:
    if not _poll_ok(leg, min_poll=min_poll):
        return False
    g20 = leg.get("gate_gap_w20_w5")
    g53 = leg.get("gate_gap_w5_w3")
    if g20 is None or g53 is None:
        return False
    return (
        band.gap_w20_w5_min <= float(g20) <= band.gap_w20_w5_max
        and band.gap_w5_w3_min <= float(g53) <= band.gap_w5_w3_max
    )


def _overlay_ok(leg: dict[str, Any], filter_ids: tuple[str, ...]) -> bool:
    trade = _leg_entry_trade_dict(leg)
    return all(_trade_matches_filter(trade, fid) for fid in filter_ids)


def _base_overlay(leg: dict[str, Any], filter_ids: tuple[str, ...]) -> bool:
    return _matches_base_gap_gate(leg) and _overlay_ok(leg, filter_ids)


def _z_gt(leg: dict[str, Any], key: str, *, threshold: float = 0.0) -> bool:
    z = leg.get(key)
    return z is not None and float(z) > threshold


def _pct_gte(leg: dict[str, Any], key: str, *, threshold: float) -> bool:
    pct = leg.get(key)
    return pct is not None and float(pct) >= threshold


def default_shape_catalog() -> tuple[ShapeDef, ...]:
    """≥15 diverse candidate shapes for RP-5 (base / timing / overlay / combo)."""
    shapes: list[ShapeDef] = [
        ShapeDef(
            id="gap_band_base_v1",
            label="Gap sweet-band base gate",
            category="base_gate",
            matcher=lambda leg: _matches_base_gap_gate(leg),
            definition={
                "gap_band": BASE_GAP_BAND.label(),
                "min_poll_idx": BASE_MIN_POLL_IDX,
                "rebound": "none",
            },
        ),
        ShapeDef(
            id="gap_band_tight",
            label="Tighter gap band [3,6]·[-0.5,1]",
            category="base_gate",
            matcher=lambda leg: _gap_band_ok(leg, TIGHT_GAP_BAND, min_poll=BASE_MIN_POLL_IDX),
            definition={"gap_band": TIGHT_GAP_BAND.label(), "min_poll_idx": BASE_MIN_POLL_IDX},
        ),
        ShapeDef(
            id="poll_ge2_base",
            label="Base gate · poll≥2",
            category="base_gate",
            matcher=lambda leg: _matches_base_gap_gate(leg) and _poll_ok(leg, min_poll=2),
            definition={"gap_band": BASE_GAP_BAND.label(), "min_poll_idx": 2},
        ),
        ShapeDef(
            id="gap_w20_w5_z_pos",
            label="gap_w20_w5 60d z-score > 0 (RP-3 timing)",
            category="timing",
            matcher=lambda leg: _z_gt(leg, "gap_w20_w5_z"),
            definition={"factor": "gap_w20_w5_z", "threshold": 0, "baseline_window_days": 60},
        ),
        ShapeDef(
            id="gap_w20_w5_pct_top",
            label="gap_w20_w5 60d pct ≥ 67",
            category="timing",
            matcher=lambda leg: _pct_gte(leg, "gap_w20_w5_pct", threshold=67.0),
            definition={"factor": "gap_w20_w5_pct", "threshold": 67},
        ),
        ShapeDef(
            id="gap_w5_w3_z_pos",
            label="gap_w5_w3 60d z-score > 0",
            category="timing",
            matcher=lambda leg: _z_gt(leg, "gap_w5_w3_z"),
            definition={"factor": "gap_w5_w3_z", "threshold": 0},
        ),
        ShapeDef(
            id="gap_w20_w5_raw_midhigh",
            label="gap_w20_w5 raw ∈ [4,8]",
            category="timing",
            matcher=lambda leg: (
                leg.get("gate_gap_w20_w5") is not None
                and MIDHIGH_RAW_GAP[0] <= float(leg["gate_gap_w20_w5"]) <= MIDHIGH_RAW_GAP[1]
            ),
            definition={"gap_w20_w5_range": list(MIDHIGH_RAW_GAP)},
        ),
        ShapeDef(
            id="rv3_rebound_z_nonpos",
            label="rv3_rebound 60d z ≤ 0 (low rebound entry)",
            category="timing",
            matcher=lambda leg: leg.get("rv3_rebound_z") is not None and float(leg["rv3_rebound_z"]) <= 0,
            definition={"factor": "rv3_rebound_z", "threshold": 0, "direction": "lte"},
        ),
        ShapeDef(
            id="w3_improving_overlay",
            label="Base gate + W3 MV>100",
            category="overlay",
            matcher=lambda leg: _base_overlay(leg, ("w3_improving",)),
            definition={"base_gate": BASE_GAP_BAND.label(), "overlay": "w3_improving"},
        ),
        ShapeDef(
            id="avoid_w3_lagging_overlay",
            label="Base gate + W3 ∉ Lagging",
            category="overlay",
            matcher=lambda leg: _base_overlay(leg, ("avoid_w3_lagging",)),
            definition={"base_gate": BASE_GAP_BAND.label(), "overlay": "avoid_w3_lagging"},
        ),
        ShapeDef(
            id="w3_rv_gte_99_5_overlay",
            label="Base gate + W3 RV≥99.5",
            category="overlay",
            matcher=lambda leg: _base_overlay(leg, ("w3_rv_gte_99.5",)),
            definition={"base_gate": BASE_GAP_BAND.label(), "overlay": "w3_rv_gte_99.5"},
        ),
        ShapeDef(
            id="w5_mv_gt_100_overlay",
            label="Base gate + W5 MV>100",
            category="overlay",
            matcher=lambda leg: _base_overlay(leg, ("w5_mv_gt_100",)),
            definition={"base_gate": BASE_GAP_BAND.label(), "overlay": "w5_mv_gt_100"},
        ),
        ShapeDef(
            id="spread_pullback_overlay",
            label="Base gate + spread pullback",
            category="overlay",
            matcher=lambda leg: _base_overlay(leg, ("spread_pullback",)),
            definition={"base_gate": BASE_GAP_BAND.label(), "overlay": "spread_pullback"},
        ),
        ShapeDef(
            id="w20_rv_lte_105_5_overlay",
            label="Base gate + W20 RV≤105.5",
            category="overlay",
            matcher=lambda leg: _base_overlay(leg, ("w20_rv_lte_105.5",)),
            definition={"base_gate": BASE_GAP_BAND.label(), "overlay": "w20_rv_lte_105.5"},
        ),
        ShapeDef(
            id="w20_w3_spread_lte_6_overlay",
            label="Base gate + W20-W3 RV spread≤6",
            category="overlay",
            matcher=lambda leg: _base_overlay(leg, ("w20_w3_spread_lte_6",)),
            definition={"base_gate": BASE_GAP_BAND.label(), "overlay": "w20_w3_spread_lte_6"},
        ),
        ShapeDef(
            id="w3_improving_avoid_lag",
            label="Base gate + W3 improving · avoid lagging",
            category="combo",
            matcher=lambda leg: _base_overlay(leg, ("w3_improving", "avoid_w3_lagging")),
            definition={"overlays": ["w3_improving", "avoid_w3_lagging"]},
        ),
        ShapeDef(
            id="w3_improving_w5_mv",
            label="Base gate + W3 improving · W5 MV>100",
            category="combo",
            matcher=lambda leg: _base_overlay(leg, ("w3_improving", "w5_mv_gt_100")),
            definition={"overlays": ["w3_improving", "w5_mv_gt_100"]},
        ),
        ShapeDef(
            id="w3_improving_spread_pb",
            label="Base gate + W3 improving · spread pullback",
            category="combo",
            matcher=lambda leg: _base_overlay(leg, ("w3_improving", "spread_pullback")),
            definition={"overlays": ["w3_improving", "spread_pullback"]},
        ),
    ]
    return tuple(shapes)


def annotate_tp_returns(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for leg in legs:
        leg = dict(leg)
        ev = evaluate_leg_tp_sl(leg, CHAMPION_COMBO)
        leg["tp_ret_pct"] = float(ev.get("exit_ret_pct") or leg.get("return_pct") or 0)
        out.append(leg)
    return out


def prepare_annotated_legs(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    hold_days: int = 5,
    baseline_window: int = 60,
    min_trailing_days: int = 10,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> list[dict[str, Any]]:
    raw = collect_abc_v3_f1_raw_legs(
        conn, dates=dates, hold_days=hold_days, etf_codes=etf_codes
    )
    if not raw:
        return []
    stock_ids = sorted({str(l["stock_id"]) for l in raw})
    close, _, _ = load_price_panels(conn)
    hist_dates = [d for d in close.index.astype(str).tolist() if d <= dates[-1]]
    baseline = build_stock_daily_gap_rv_series(
        conn, stock_ids=stock_ids, dates=hist_dates, etf_codes=etf_codes
    )
    enriched = attach_normalized_gate_features(
        raw,
        baseline,
        window=baseline_window,
        min_trailing=min_trailing_days,
    )
    return annotate_tp_returns(enriched)


def _leg_key(leg: dict[str, Any]) -> tuple[str, str]:
    return (str(leg.get("stock_id")), str(leg.get("entry_date")))


def _filter_legs(
    legs: list[dict[str, Any]],
    *,
    date_set: set[str] | None,
    matcher: Callable[[dict[str, Any]], bool],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for leg in legs:
        if date_set is not None and str(leg.get("entry_date")) not in date_set:
            continue
        if matcher(leg):
            out.append(leg)
    return out


def _stage_stats(
    matched: list[dict[str, Any]],
    *,
    baseline_mean: float,
    target_pct: float,
) -> dict[str, Any]:
    rets = [float(l["tp_ret_pct"]) for l in matched]
    n = len(rets)
    if n == 0:
        return {
            "n": 0,
            "mean_ret_pct": None,
            "sigma_pct": None,
            "baseline_mean_pct": round(baseline_mean, 3),
            "p_raw": None,
            "win_rate_pct": None,
            "pct_ge_target": None,
            "skipped": True,
        }
    mu = round(mean(rets), 3)
    sigma = round(pstdev(rets), 3) if n > 1 else None
    ttest = one_sample_t_test(rets, target_pct)
    return {
        "n": n,
        "mean_ret_pct": mu,
        "sigma_pct": sigma,
        "baseline_mean_pct": round(baseline_mean, 3),
        "delta_vs_baseline_pp": round(mu - baseline_mean, 3),
        "p_raw": ttest.get("p_two_sided"),
        "win_rate_pct": round(100.0 * sum(1 for r in rets if r > 0) / n, 1),
        "pct_ge_target": round(100.0 * sum(1 for r in rets if r >= target_pct) / n, 1),
        "skipped": False,
    }


def _baseline_mean(legs: list[dict[str, Any]], date_set: set[str]) -> float:
    rets = [
        float(l["tp_ret_pct"])
        for l in legs
        if str(l.get("entry_date")) in date_set
    ]
    return round(mean(rets), 3) if rets else 0.0


def _overlap_rate(keys_a: set[tuple[str, str]], keys_b: set[tuple[str, str]]) -> float | None:
    if not keys_a or not keys_b:
        return None
    inter = len(keys_a & keys_b)
    union = len(keys_a | keys_b)
    return round(100.0 * inter / union, 1) if union else None


def _pairwise_overlap_matrix(
    shape_keys: dict[str, set[tuple[str, str]]],
) -> list[dict[str, Any]]:
    ids = sorted(shape_keys)
    rows: list[dict[str, Any]] = []
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            rows.append(
                {
                    "shape_a": a,
                    "shape_b": b,
                    "overlap_pct": _overlap_rate(shape_keys[a], shape_keys[b]),
                    "shared_n": len(shape_keys[a] & shape_keys[b]),
                }
            )
    rows.sort(key=lambda r: float(r.get("overlap_pct") or 0), reverse=True)
    return rows


def _annualized_n(n_events: int, *, n_trading_days: int) -> float:
    if n_trading_days <= 0:
        return 0.0
    return round(n_events * 252.0 / n_trading_days, 1)


def run_shape_library_study(
    legs: list[dict[str, Any]],
    *,
    date_start: str,
    date_end: str,
    hold_days: int = 5,
    target_pct: float = 8.0,
    discovery_min_n: int = 10,
    validation_min_n: int | None = None,
    fdr_q: float = 0.10,
    is_ratio: float = 0.50,
    val_ratio: float = 0.25,
    min_overlap_pass_pct: float = 70.0,
    shape_catalog: tuple[ShapeDef, ...] | None = None,
) -> dict[str, Any]:
    if not legs:
        raise ValueError("no annotated legs")

    catalog = shape_catalog or default_shape_catalog()
    all_dates = sorted({str(l["entry_date"]) for l in legs})
    is_dates, val_dates, hold_dates = _split_dates_three_way(
        all_dates, is_ratio=is_ratio, val_ratio=val_ratio
    )
    is_set, val_set, hold_set = set(is_dates), set(val_dates), set(hold_dates)

    min_n_power = validation_min_n
    if min_n_power is None:
        min_n_power = min_n_for_power(
            effect_pp=target_pct - 3.0,
            sigma_pp=10.0,
            power=0.80,
            alpha=0.05,
        )

    shape_rows: list[dict[str, Any]] = []
    for shape in catalog:
        is_matched = _filter_legs(legs, date_set=is_set, matcher=shape.matcher)
        val_matched = _filter_legs(legs, date_set=val_set, matcher=shape.matcher)
        hold_matched = _filter_legs(legs, date_set=hold_set, matcher=shape.matcher)

        is_base = _baseline_mean(legs, is_set)
        val_base = _baseline_mean(legs, val_set)
        hold_base = _baseline_mean(legs, hold_set)

        is_stats = _stage_stats(is_matched, baseline_mean=is_base, target_pct=target_pct)
        val_stats = _stage_stats(val_matched, baseline_mean=val_base, target_pct=target_pct)
        hold_stats = _stage_stats(hold_matched, baseline_mean=hold_base, target_pct=target_pct)

        discovery_pass = (
            not is_stats.get("skipped")
            and int(is_stats["n"]) >= discovery_min_n
            and float(is_stats["mean_ret_pct"] or 0) > 0
        )
        validation_candidate = (
            discovery_pass
            and not val_stats.get("skipped")
            and int(val_stats["n"]) >= discovery_min_n
            and float(val_stats["mean_ret_pct"] or 0) >= target_pct
            and (
                float(val_stats["mean_ret_pct"] or 0) >= float(is_stats["mean_ret_pct"] or 0) * 0.5
                or float(val_stats["delta_vs_baseline_pp"] or 0) > 0
            )
        )

        shape_rows.append(
            {
                "id": shape.id,
                "label": shape.label,
                "category": shape.category,
                "definition": shape.definition,
                "discovery": {**is_stats, "window_dates": [is_dates[0], is_dates[-1]] if is_dates else []},
                "validation": {**val_stats, "window_dates": [val_dates[0], val_dates[-1]] if val_dates else []},
                "holdout": {**hold_stats, "window_dates": [hold_dates[0], hold_dates[-1]] if hold_dates else []},
                "discovery_pass": discovery_pass,
                "validation_candidate": validation_candidate,
                "holdout_pass": False,
                "status": "rejected",
            }
        )

    val_candidates = [r for r in shape_rows if r.get("validation_candidate")]
    n_tested = len(val_candidates)
    p_vals: list[float | None] = []
    labels: list[str] = []
    for row in val_candidates:
        p = (row.get("validation") or {}).get("p_raw")
        p_vals.append(float(p) if p is not None else None)
        labels.append(str(row["id"]))

    fdr_payload = (
        apply_fdr_correction(p_vals, q=fdr_q, labels=labels)
        if n_tested
        else {"method": "bh", "q": fdr_q, "m": 0, "n_significant": 0, "results": []}
    )
    sig_ids = {
        r["label"]
        for r in fdr_payload.get("results") or []
        if r.get("significant")
    }
    for row in shape_rows:
        if row["id"] not in sig_ids:
            continue
        val = row.get("validation") or {}
        if int(val.get("n") or 0) < min_n_power:
            row["validation_fdr_pass"] = False
            row["validation_block_reason"] = "insufficient_n_for_power"
            continue
        row["validation_fdr_pass"] = True
        row["status"] = "validated"

    holdout_candidates = [r for r in shape_rows if r.get("validation_fdr_pass")]
    for row in holdout_candidates:
        hold = row.get("holdout") or {}
        if (
            not hold.get("skipped")
            and int(hold.get("n") or 0) >= max(discovery_min_n, 5)
            and float(hold.get("mean_ret_pct") or 0) >= target_pct
        ):
            row["holdout_pass"] = True
            row["status"] = "holdout_passed"

    passed = [r for r in shape_rows if r.get("holdout_pass")]
    holdout_keys_by_shape = {
        r["id"]: {_leg_key(l) for l in _filter_legs(legs, date_set=hold_set, matcher=next(s.matcher for s in catalog if s.id == r["id"]))}
        for r in passed
    }
    overlap_pairs = _pairwise_overlap_matrix(holdout_keys_by_shape)
    max_overlap = max((float(p["overlap_pct"]) for p in overlap_pairs), default=0.0)

    # Union on full window (dedupe stock-day)
    union_keys: dict[tuple[str, str], dict[str, Any]] = {}
    for shape in catalog:
        row = next((r for r in shape_rows if r["id"] == shape.id), None)
        if not row or not row.get("holdout_pass"):
            continue
        for leg in _filter_legs(legs, date_set=None, matcher=shape.matcher):
            k = _leg_key(leg)
            prev = union_keys.get(k)
            score = float((row.get("validation") or {}).get("mean_ret_pct") or 0)
            if prev is None or score > float(prev.get("priority_score") or 0):
                union_keys[k] = {
                    "stock_id": k[0],
                    "entry_date": k[1],
                    "tp_ret_pct": float(leg["tp_ret_pct"]),
                    "shape_id": shape.id,
                    "priority_score": score,
                }

    union_rets = [float(v["tp_ret_pct"]) for v in union_keys.values()]
    n_trading_days = len(all_dates)
    union_n = len(union_rets)
    union_mean = round(mean(union_rets), 3) if union_rets else None
    union_annual_n = _annualized_n(union_n, n_trading_days=n_trading_days)

    success = {
        "min_shapes_holdout_passed": len(passed) >= 5,
        "union_annual_n_ge_150": union_annual_n >= 150,
        "union_mean_ge_target": bool(union_mean is not None and union_mean >= target_pct),
        "max_overlap_below_threshold": max_overlap < min_overlap_pass_pct,
    }
    verdict = "passed" if all(success.values()) else "not_passed"

    return {
        "schema": SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": date_end,
        "hold_days": hold_days,
        "target_pct": target_pct,
        "n_raw_legs": len(legs),
        "n_trading_days": n_trading_days,
        "split": {
            "is_ratio": is_ratio,
            "val_ratio": val_ratio,
            "holdout_ratio": round(1.0 - is_ratio - val_ratio, 4),
            "is_dates": [is_dates[0], is_dates[-1], len(is_dates)] if is_dates else [],
            "val_dates": [val_dates[0], val_dates[-1], len(val_dates)] if val_dates else [],
            "holdout_dates": [hold_dates[0], hold_dates[-1], len(hold_dates)] if hold_dates else [],
        },
        "policy": {
            "discovery_min_n": discovery_min_n,
            "validation_min_n_power": min_n_power,
            "fdr_q": fdr_q,
            "exit_rule": CHAMPION_COMBO.tp.variant_id,
        },
        "n_shapes_tested": len(catalog),
        "n_discovery_pass": sum(1 for r in shape_rows if r.get("discovery_pass")),
        "n_validation_candidates": n_tested,
        "n_validation_fdr_pass": sum(1 for r in shape_rows if r.get("validation_fdr_pass")),
        "n_holdout_passed": len(passed),
        "shapes": shape_rows,
        "validation_fdr": fdr_payload,
        "holdout_passed_ids": [r["id"] for r in passed],
        "overlap_pairs_top10": overlap_pairs[:10],
        "max_pairwise_overlap_pct": max_overlap,
        "union": {
            "n_unique_stock_days": union_n,
            "annualized_n": union_annual_n,
            "mean_ret_pct": union_mean,
            "win_rate_pct": round(100.0 * sum(1 for r in union_rets if r > 0) / union_n, 1) if union_n else None,
        },
        "success_criteria": success,
        "verdict": verdict,
    }


def render_shape_library_md(payload: dict[str, Any]) -> str:
    target = float(payload.get("target_pct") or 8.0)
    passed = payload.get("holdout_passed_ids") or []
    union = payload.get("union") or {}
    crit = payload.get("success_criteria") or {}
    split = payload.get("split") or {}

    lines = [
        f"# ABC v3+F1 · multisignal shape library（RP-5）",
        "",
        f"> Verdict: **{payload.get('verdict')}** · 窗口 **{payload.get('date_start')}** .. "
        f"**{payload.get('date_end')}** · hold **{payload.get('hold_days')}d** · "
        f"raw legs **{payload.get('n_raw_legs')}** · 候選 shapes **{payload.get('n_shapes_tested')}**",
        "",
        "## 研究問題",
        "",
        "能否找到一組彼此獨立定義的進場形狀，使得每一個經三段式驗證均報酬 ≥8%，",
        "且聯集（stock-day 去重）年化 n ≈ 200？",
        "",
        "## 三段式切分",
        "",
        f"- IS {split.get('is_ratio', 0.5):.0%}：{split.get('is_dates')} "
        f"· Validation {split.get('val_ratio', 0.25):.0%}：{split.get('val_dates')} "
        f"· Holdout {split.get('holdout_ratio', 0.25):.0%}：{split.get('holdout_dates')}",
        f"- Validation FDR q={payload.get('policy', {}).get('fdr_q')} · "
        f"power min_n={payload.get('policy', {}).get('validation_min_n_power')}",
        "",
        "## 成功標準（RP-5 §5）",
        "",
        f"- ≥5 shapes Holdout 通過：{'✔' if crit.get('min_shapes_holdout_passed') else '✗'} "
        f"（實際 **{payload.get('n_holdout_passed')}**）",
        f"- 聯集年化 n ≥ 150：{'✔' if crit.get('union_annual_n_ge_150') else '✗'} "
        f"（**{union.get('annualized_n')}**）",
        f"- 聯集均報酬 ≥ {target:g}%：{'✔' if crit.get('union_mean_ge_target') else '✗'} "
        f"（**{union.get('mean_ret_pct')}%**）",
        f"- 最大 pairwise 重疊 < 70%：{'✔' if crit.get('max_overlap_below_threshold') else '✗'} "
        f"（**{payload.get('max_pairwise_overlap_pct')}%**）",
        "",
        "## Pipeline 摘要",
        "",
        f"- Discovery 通過：{payload.get('n_discovery_pass')} / {payload.get('n_shapes_tested')}",
        f"- Validation 候選：{payload.get('n_validation_candidates')}",
        f"- Validation FDR 通過：{payload.get('n_validation_fdr_pass')}",
        f"- Holdout 通過：**{payload.get('n_holdout_passed')}** → `{', '.join(passed) or '—'}`",
        "",
    ]

    if passed:
        lines.extend(["## Holdout 通過 shapes", ""])
        for row in payload.get("shapes") or []:
            if row.get("id") not in passed:
                continue
            hold = row.get("holdout") or {}
            val = row.get("validation") or {}
            lines.append(
                f"- `{row.get('id')}`（{row.get('label')}）· Holdout n={hold.get('n')} · "
                f"均 **{hold.get('mean_ret_pct')}%** · Validation 均 **{val.get('mean_ret_pct')}%**"
            )
        lines.append("")

    lines.extend(
        [
            "## 聯集（全窗口去重）",
            "",
            f"- 唯一 stock-day：**{union.get('n_unique_stock_days')}** · 年化 n ≈ **{union.get('annualized_n')}**",
            f"- 加權均報酬（每 stock-day 取 Validation 均最高之 shape）：**{union.get('mean_ret_pct')}%** · "
            f"勝率 **{union.get('win_rate_pct')}%**",
            "",
            "## 全候選三段式結果",
            "",
            "| Shape | 類別 | IS n | IS 均% | Val n | Val 均% | Hold n | Hold 均% | 狀態 |",
            "|-------|------|-----:|-------:|------:|--------:|-------:|---------:|------|",
        ]
    )
    for row in payload.get("shapes") or []:
        d, v, h = row.get("discovery") or {}, row.get("validation") or {}, row.get("holdout") or {}
        lines.append(
            f"| {row.get('id')} | {row.get('category')} | {d.get('n')} | {d.get('mean_ret_pct')} | "
            f"{v.get('n')} | {v.get('mean_ret_pct')} | {h.get('n')} | {h.get('mean_ret_pct')} | "
            f"{row.get('status')} |"
        )

    if payload.get("overlap_pairs_top10"):
        lines.extend(["", "## Holdout 通過 shapes 重疊率（前 10）", ""])
        for p in payload.get("overlap_pairs_top10") or []:
            lines.append(
                f"- `{p.get('shape_a')}` × `{p.get('shape_b')}`：overlap **{p.get('overlap_pct')}%** "
                f"（shared n={p.get('shared_n')}）"
            )

    lines.extend(
        [
            "",
            "## 設計說明",
            "",
            "- 候選 shape 來自 RP-1/2/3 線索 + 既有 overlay 池（`default_shape_catalog`）",
            "- 每 shape 獨立 matcher；forward return = champion TP-only（NEVER_SL）",
            "- Discovery：n≥10 且 IS 均>0 · Validation：均≥8% + BH FDR + min_n power · Holdout：一次性 ≥8%",
            "- 模組：`src/research/backtest/abc_v3_f1_shape_library_study.py`",
        ]
    )
    return "\n".join(lines)
