"""Detach Gate rebuy · long-sample adoption stress (1h primary + 5m check).

Research topic: detach-gate-trough-find · long-history sample expansion.
Study-only — does NOT wire Order-layer auto-rebuy (rebuy stays false).

Primary panel
  Yahoo ^TWII × NQ=F **1h** (~730 calendar days). Spread window = last completed
  hour (≈60m), not rolling 30m. IMPROVE / TF-3 / cooldown are 1h-native:
    - IMPROVE = first Δ(spread_1h) ≥ GAP_EPS (gap_pulse)
    - TF-3 = hour-bar both↑ and tw_bar_roc ≥ nq_bar_roc + δ
    - cooldown ≥30m on 5m → **≥1 hour bar (≥60m)** here (distortion documented)

Secondary
  Yahoo 5m (~59d) high-res check with the same champion family (true 30m cool).

Analyst adoption gates (documented in report):
  A1 n_RED ≥ 40 and n_MT ≥ 25
  A2 IS/OOS FPR both ≤ 0.40, or |IS−OOS| ≤ 0.15 with both ≤ 0.55
  A3 FPR(no-MT) ≤ 0.40 on full 1h sample
  A4 median headroom ≤ 1.0% OR near-trough rate > 0 with Wilson CI lo > 0
  A5 case-day family fires before trough when case is in panel
  A6 pool consistency: 5m champion FPR/headroom not worse than prior short panel
     by more than +0.15 FPR / +0.5pp headroom (directional only)

Three-way 採納 targets (independent):
  (A) human manual rebuy SOP
  (B) Strategy layer advisory
  (C) Order auto-rebuy  — always stricter; fails unless A–B pass + FPR≤0.25 + near>0
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.backtest.detach_gate_tf2_trough_find_study import (
    CASE_DAY,
    MEANINGFUL_TROUGH_DD,
    NEAR_TROUGH_BP,
    load_day_polls as load_day_polls_5m,
)
from research.backtest.detach_gate_tf23_repair_confirm_study import (
    VariantSpec,
    aggregate_variant,
    analyze_day_variants,
)
from research.backtest.us_tw_30m_rolling_risk_gate_study import GAP_EPS
from research.backtest.us_tw_divergence_sell_gate_study import fetch_yahoo_ohlc
from research.backtest.us_tw_sell_only_live_readiness_study import (
    SellSpec,
    _session_dates_1h,
    build_poll_frame_1h,
    frozen_sell_gate_spec,
)

# ── Adoption gates (analyst-grade for manual / Strategy; Order stricter) ─────
GATE_N_RED = 40
GATE_N_MT = 25
GATE_FPR = 0.40
GATE_FPR_SOFT_CAP = 0.55  # only with IS/OOS stability exception
GATE_IS_OOS_ABS = 0.15
GATE_MED_HEADROOM = 1.0
GATE_ORDER_FPR = 0.25
GATE_ORDER_NEAR = 0.05  # need some near-trough mass for auto

DELTA_SWEEP = (0.05, 0.10, 0.15)
DEFAULT_DELTA = 0.10
# 1h-native cooldown minutes (true 30m impossible on hourly bars)
COOLDOWN_1H = (60, 120)  # ≥1 bar / ≥2 bars
CUTOFFS = ("12:30", "13:00")
LOOKBACK_1H_DAYS = 730  # Yahoo 1h hard-ish calendar cap


def frozen_red_spec_1h() -> SellSpec:
    """Same numeric RED thresholds as 5m frozen, evaluated on 1h-native spread.

    Distortion: lookback ≈ 60m (last hour) vs 5m rolling 30m; arm clock still
    09:40–12:30 but polls are typically hourly (:00), so first eligible bar is
    often 10:00.
    """
    return SellSpec(
        strategy_id="s1h_proxy_s5_w-0.6_c1_down-0.5_arm0940-1230",
        panel="1h60",
        win_thr=-0.6,
        confirm_polls=1,
        max_tw_from_open=-0.5,
        arm_after="09:40",
        arm_before="12:30",
        lookback_bars=1,
    )


def enrich_poll_1h_native(poll: pd.DataFrame) -> pd.DataFrame:
    """Add TF-2/TF-3 pulse cols on 1h poll (spread_30m holds last-hour spread).

    TF-3 roc uses **hour-bar returns** (`tw_win` / `nq_win`) when present — that is the
    native 1h definition of 「這一小時雙向上」. Falls back to Δ(from_open) otherwise.
    IMPROVE still uses first difference of the hour-spread column.
    """
    out = poll.copy()
    if "spread_30m" not in out.columns and "spread_win" in out.columns:
        out["spread_30m"] = out["spread_win"]
    if "tw_win" in out.columns and "nq_win" in out.columns:
        out["tw_bar_roc"] = out["tw_win"].astype(float)
        out["nq_bar_roc"] = out["nq_win"].astype(float)
    else:
        out["tw_bar_roc"] = out["tw_from_open"].diff()
        out["nq_bar_roc"] = out["nq_from_open"].diff()
    out["d_spread_30m"] = out["spread_30m"].diff()

    def _gap_pulse(x: float) -> str:
        if not math.isfinite(x):
            return "NA"
        if x <= -GAP_EPS:
            return "WORSEN"
        if x >= GAP_EPS:
            return "IMPROVE"
        return "FLAT"

    def _sync_pulse(tw_r: float, nq_r: float) -> str:
        if not (math.isfinite(tw_r) and math.isfinite(nq_r)):
            return "NA"
        if abs(tw_r) < 0.02 and abs(nq_r) < 0.02:
            return "QUIET"
        st = np.sign(tw_r)
        sn = np.sign(nq_r)
        if st == 0 or sn == 0:
            return "ONE_SIDE"
        if st == sn:
            return "FOLLOW"
        return "OPPOSE"

    out["gap_pulse"] = [_gap_pulse(float(x)) for x in out["d_spread_30m"]]
    out["sync_pulse"] = [
        _sync_pulse(float(a), float(b)) for a, b in zip(out["tw_bar_roc"], out["nq_bar_roc"])
    ]
    return out


def frozen_red_spec_1h_wide() -> SellSpec:
    """Sensitivity: softer RED to expand n — thr −0.5 · no open-down filter."""
    return SellSpec(
        strategy_id="s1h_wide_w-0.5_c1_arm0940-1230",
        panel="1h60",
        win_thr=-0.5,
        confirm_polls=1,
        max_tw_from_open=None,
        arm_after="09:40",
        arm_before="12:30",
        lookback_bars=1,
    )


def variant_catalog_1h() -> list[VariantSpec]:
    """Modest champion-family grid in 1h-native cooldown terms."""
    specs: list[VariantSpec] = [
        VariantSpec(id="A_tf2", family="A", label="TF-2 first IMPROVE (1h Δspread)"),
    ]
    for d in DELTA_SWEEP:
        ds = f"{d:.2f}".rstrip("0").rstrip(".")
        specs.append(
            VariantSpec(
                id=f"B_tf3_d{ds}",
                family="B",
                label=f"TF-3 alone δ={ds} (1h bar ROC)",
                delta=d,
            )
        )
        specs.append(
            VariantSpec(
                id=f"C_arm_tf3_d{ds}",
                family="C",
                label=f"IMPROVE arm → TF-3 δ={ds}",
                delta=d,
                arm_improve=True,
            )
        )

    ds = f"{DEFAULT_DELTA:.2f}".rstrip("0").rstrip(".")
    for k in COOLDOWN_1H:
        # id keeps minutes so reports stay searchable; label states bar wait
        n_bars = max(1, int(round(k / 60.0)))
        specs.append(
            VariantSpec(
                id=f"D_cool_red_K{k}_d{ds}",
                family="D",
                label=f"TF-3 ≥{k}m (≥{n_bars}h bar) after RED δ={ds}",
                delta=DEFAULT_DELTA,
                cooldown_min=k,
                cooldown_from="red",
            )
        )
        specs.append(
            VariantSpec(
                id=f"D_cool_imp_K{k}_d{ds}",
                family="D",
                label=f"TF-3 ≥{k}m (≥{n_bars}h bar) after IMPROVE δ={ds}",
                delta=DEFAULT_DELTA,
                arm_improve=True,
                cooldown_min=k,
                cooldown_from="improve",
            )
        )

    specs.extend(
        [
            VariantSpec(
                id=f"E_tf3_follow_d{ds}",
                family="E",
                label=f"TF-3 + FOLLOW δ={ds}",
                delta=DEFAULT_DELTA,
                require_follow=True,
            ),
            VariantSpec(
                id=f"D_E_cool_imp60_follow_d{ds}",
                family="combo",
                label=f"≥60m after IMPROVE + FOLLOW δ={ds}",
                delta=DEFAULT_DELTA,
                arm_improve=True,
                cooldown_min=60,
                cooldown_from="improve",
                require_follow=True,
            ),
        ]
    )

    # Soft veto on primary 1h champion proxy (cool-imp 60m ≈ 5m K30)
    base = VariantSpec(
        id="D_cool_imp_K60_d0.1",
        family="D",
        label="TF-3 ≥60m after IMPROVE δ=0.1",
        delta=DEFAULT_DELTA,
        arm_improve=True,
        cooldown_min=60,
        cooldown_from="improve",
    )
    for cut in CUTOFFS:
        cut_tag = cut.replace(":", "")
        specs.append(
            VariantSpec(
                id=f"G_{base.id}_cut{cut_tag}",
                family="G",
                label=f"{base.label} · cutoff <{cut}",
                delta=base.delta,
                arm_improve=base.arm_improve,
                cooldown_min=base.cooldown_min,
                cooldown_from=base.cooldown_from,
                cutoff=cut,
            )
        )
    return specs


def variant_catalog_5m_check() -> list[VariantSpec]:
    """High-res check: short list including prior champion."""
    ds = f"{DEFAULT_DELTA:.2f}".rstrip("0").rstrip(".")
    return [
        VariantSpec(id="A_tf2", family="A", label="TF-2 first IMPROVE"),
        VariantSpec(id=f"B_tf3_d{ds}", family="B", label=f"TF-3 alone δ={ds}", delta=DEFAULT_DELTA),
        VariantSpec(
            id=f"C_arm_tf3_d{ds}",
            family="C",
            label=f"IMPROVE arm → TF-3 δ={ds}",
            delta=DEFAULT_DELTA,
            arm_improve=True,
        ),
        VariantSpec(
            id=f"D_cool_imp_K30_d{ds}",
            family="D",
            label=f"TF-3 ≥30m after IMPROVE δ={ds}",
            delta=DEFAULT_DELTA,
            arm_improve=True,
            cooldown_min=30,
            cooldown_from="improve",
        ),
        VariantSpec(
            id=f"D_cool_imp_K60_d{ds}",
            family="D",
            label=f"TF-3 ≥60m after IMPROVE δ={ds}",
            delta=DEFAULT_DELTA,
            arm_improve=True,
            cooldown_min=60,
            cooldown_from="improve",
        ),
        VariantSpec(
            id=f"G_D_cool_imp_K30_d{ds}_cut1300",
            family="G",
            label=f"≥30m after IMPROVE δ={ds} · cutoff <13:00",
            delta=DEFAULT_DELTA,
            arm_improve=True,
            cooldown_min=30,
            cooldown_from="improve",
            cutoff="13:00",
        ),
    ]


def load_day_polls_1h(
    *,
    date_end: str | date | None = None,
    lookback_days: int = LOOKBACK_1H_DAYS,
) -> dict[str, pd.DataFrame]:
    end = date.fromisoformat(str(date_end)) if date_end else date.today()
    start = end - timedelta(days=int(lookback_days))
    tw = fetch_yahoo_ohlc("^TWII", start, end, interval="1h")
    nq = fetch_yahoo_ohlc("NQ=F", start, end, interval="1h")
    dates = _session_dates_1h(tw, nq)
    polls: dict[str, pd.DataFrame] = {}
    for d in dates:
        pf = build_poll_frame_1h(d, tw, nq)
        if pf is None or len(pf) < 2:
            continue
        polls[d] = enrich_poll_1h_native(pf)
    return polls


def _wilson_ci_lo(successes: int, n: int, z: float = 1.96) -> float | None:
    if n <= 0:
        return None
    p = successes / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2.0 * n)
    margin = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def _split_is_oos(
    day_rows: list[dict[str, Any]],
    variant_id: str,
) -> dict[str, Any]:
    events = [r for r in day_rows if r.get("has_event")]
    if len(events) < 4:
        return {"cut_date": None, "is": None, "oos": None, "n_is": 0, "n_oos": 0}
    dates = sorted(r["date"] for r in events)
    cut = dates[len(dates) // 2]
    is_rows = [r for r in day_rows if r.get("has_event") and r["date"] < cut]
    oos_rows = [r for r in day_rows if r.get("has_event") and r["date"] >= cut]
    return {
        "cut_date": cut,
        "n_is": sum(1 for r in is_rows if r.get("has_event")),
        "n_oos": sum(1 for r in oos_rows if r.get("has_event")),
        "is": aggregate_variant(is_rows, variant_id),
        "oos": aggregate_variant(oos_rows, variant_id),
    }


def evaluate_gates(
    *,
    champion_id: str,
    agg: dict[str, Any],
    is_oos: dict[str, Any],
    day_rows: list[dict[str, Any]],
    case_row: dict[str, Any] | None,
    panel_5m_agg: dict[str, Any] | None,
) -> dict[str, Any]:
    n_red = int(agg.get("n_event_days") or 0)
    n_mt = int(agg.get("n_meaningful_trough") or 0)
    fpr = agg.get("fpr_sig_on_no_mt")
    head = agg.get("median_headroom_vs_trough_pct")
    near = agg.get("buy_near_trough_rate")
    n_before = int(agg.get("n_before") or 0)
    near_n = 0
    if near is not None and n_before > 0 and math.isfinite(float(near)):
        near_n = int(round(float(near) * n_before))
    near_ci_lo = _wilson_ci_lo(near_n, n_before) if n_before else None

    is_agg = is_oos.get("is") or {}
    oos_agg = is_oos.get("oos") or {}
    fpr_is = is_agg.get("fpr_sig_on_no_mt")
    fpr_oos = oos_agg.get("fpr_sig_on_no_mt")
    head_is = is_agg.get("median_headroom_vs_trough_pct")
    head_oos = oos_agg.get("median_headroom_vs_trough_pct")

    a1 = bool(n_red >= GATE_N_RED and n_mt >= GATE_N_MT)

    a2 = False
    a2_note = "insufficient IS/OOS"
    if fpr_is is not None and fpr_oos is not None:
        both_hard = float(fpr_is) <= GATE_FPR and float(fpr_oos) <= GATE_FPR
        both_soft = (
            float(fpr_is) <= GATE_FPR_SOFT_CAP
            and float(fpr_oos) <= GATE_FPR_SOFT_CAP
            and abs(float(fpr_is) - float(fpr_oos)) <= GATE_IS_OOS_ABS
        )
        a2 = bool(both_hard or both_soft)
        a2_note = (
            f"FPR IS={fpr_is} OOS={fpr_oos} |Δ|={round(abs(float(fpr_is) - float(fpr_oos)), 4)}; "
            f"head IS={head_is} OOS={head_oos}"
        )

    a3 = bool(fpr is not None and float(fpr) <= GATE_FPR)

    a4_head = bool(head is not None and float(head) <= GATE_MED_HEADROOM)
    a4_near = bool(
        near is not None
        and float(near) > 0.0
        and near_ci_lo is not None
        and float(near_ci_lo) > 0.0
    )
    # Missing headroom (no MT signals) cannot pass A4
    a4 = bool((a4_head or a4_near) and (agg.get("n_mt_with_signal") or 0) >= 1)

    a5 = False
    a5_note = "case not in panel / no RED"
    if case_row and case_row.get("has_event"):
        sig = (case_row.get("signals") or {}).get(champion_id) or {}
        a5 = bool(sig.get("poll") and sig.get("before_trough"))
        a5_note = (
            f"case signal={sig.get('poll')} before={sig.get('before_trough')} "
            f"headroom={sig.get('headroom_vs_trough_pct')}"
        )

    a6 = True
    a6_note = "5m check unavailable (skip = pass with caveat)"
    if panel_5m_agg is not None:
        f5 = panel_5m_agg.get("fpr_sig_on_no_mt")
        h5 = panel_5m_agg.get("median_headroom_vs_trough_pct")
        # Prior short panel: FPR≈0.75 · head≈0.82 — consistency = not exploding
        prior_fpr, prior_head = 0.75, 0.82
        ok_f = f5 is None or float(f5) <= prior_fpr + 0.15
        ok_h = h5 is None or float(h5) <= prior_head + 0.5
        a6 = bool(ok_f and ok_h)
        a6_note = f"5m champion FPR={f5} head={h5} vs prior≈{prior_fpr}/{prior_head}"

    gates = {
        "A1_sample_floor": {
            "pass": a1,
            "detail": f"n_RED={n_red} (need ≥{GATE_N_RED}) · n_MT={n_mt} (need ≥{GATE_N_MT})",
        },
        "A2_is_oos_stability": {"pass": a2, "detail": a2_note},
        "A3_fpr_no_mt": {
            "pass": a3,
            "detail": f"FPR(no-MT)={fpr} (need ≤{GATE_FPR})",
        },
        "A4_headroom_or_near": {
            "pass": a4,
            "detail": (
                f"med_head={head} (≤{GATE_MED_HEADROOM}?) · near={near} "
                f"Wilson_lo={None if near_ci_lo is None else round(near_ci_lo, 4)}"
            ),
        },
        "A5_case_consistency": {"pass": a5, "detail": a5_note},
        "A6_5m_pool_consistency": {"pass": a6, "detail": a6_note},
    }
    n_pass = sum(1 for g in gates.values() if g["pass"])
    return {
        "champion_id": champion_id,
        "gates": gates,
        "n_pass": n_pass,
        "n_gates": len(gates),
        "all_pass": n_pass == len(gates),
        "near_n": near_n,
        "near_wilson_ci_lo": None if near_ci_lo is None else round(near_ci_lo, 4),
        "n_panel_days": len(day_rows),
    }


def three_way_verdict(gate_eval: dict[str, Any], agg: dict[str, Any]) -> dict[str, Any]:
    """Independent (A)/(B)/(C) 採納 recommendations — honest defaults to 不採納."""
    gates = gate_eval.get("gates") or {}
    a1 = bool((gates.get("A1_sample_floor") or {}).get("pass"))
    a2 = bool((gates.get("A2_is_oos_stability") or {}).get("pass"))
    a3 = bool((gates.get("A3_fpr_no_mt") or {}).get("pass"))
    a4 = bool((gates.get("A4_headroom_or_near") or {}).get("pass"))
    a5 = bool((gates.get("A5_case_consistency") or {}).get("pass"))
    a6 = bool((gates.get("A6_5m_pool_consistency") or {}).get("pass"))
    fpr = agg.get("fpr_sig_on_no_mt")
    near = agg.get("buy_near_trough_rate")
    head = agg.get("median_headroom_vs_trough_pct")

    cov = agg.get("coverage_mt")
    n_mt_sig = int(agg.get("n_mt_with_signal") or 0)
    # Definition failure: champion never fires on meaningful-trough RED days
    definable = bool(n_mt_sig >= 5 and cov is not None and float(cov) >= 0.40)

    # Manual SOP: need sample + FPR + (head OR near) + not contradicting case/5m badly
    # + champion must actually fire on a meaningful fraction of MT days
    manual_ok = bool(definable and a1 and a3 and a4 and a6 and (a2 or a5))
    # Strategy advisory: all six gates + definable
    strategy_ok = bool(definable and gate_eval.get("all_pass"))
    # Order auto: strategy + stricter FPR + near mass
    order_ok = bool(
        strategy_ok
        and fpr is not None
        and float(fpr) <= GATE_ORDER_FPR
        and near is not None
        and float(near) >= GATE_ORDER_NEAR
    )
    if not definable:
        reasons_pre = [
            f"definition_failure: champion n_mt_with_signal={n_mt_sig} cov={cov} "
            f"(need ≥5 hits and cov≥0.40 on 1h) — TF-3/cooldown poorly resolved hourly"
        ]
    else:
        reasons_pre = []

    reasons_fail: list[str] = []
    for gid, g in gates.items():
        if not g.get("pass"):
            reasons_fail.append(f"{gid}: {g.get('detail')}")

    coarseness = (
        "1h panel cannot resolve true ≥30m cooldown or 5m TF-3 poll cadence; "
        "K60 ≈ coarsest proxy for 5m D_cool_imp_K30. If gates fail mainly on FPR/near, "
        "need local 5m accumulate before revisiting 採納 — do not treat 1h pass as "
        "operational 5m SOP freeze."
    )

    return {
        "A_manual_sop": {
            "decision": "採納" if manual_ok else "不採納",
            "ok": manual_ok,
            "note": (
                "Human hint-only SOP may stay as research note even if formal gate fails; "
                "採納 here means analyst-grade written SOP for desk use."
                if not manual_ok
                else "Analyst-grade manual rebuy SOP acceptable on long sample."
            ),
        },
        "B_strategy_advisory": {
            "decision": "採納" if strategy_ok else "不採納",
            "ok": strategy_ok,
            "note": (
                "Would require dual-register in strategy.yaml + strategies.yaml; "
                "advisory only (no Order buy)."
            ),
        },
        "C_order_auto_rebuy": {
            "decision": "採納" if order_ok else "不採納",
            "ok": order_ok,
            "note": "Order auto-rebuy stays false; needs FPR≤0.25 and near-trough mass.",
        },
        "failing_gates": reasons_pre + reasons_fail,
        "coarseness_caveat": coarseness,
        "definable_on_1h": definable,
        "summary_metrics": {
            "fpr_no_mt": fpr,
            "median_headroom_pct": head,
            "near_trough_rate": near,
            "n_red": agg.get("n_event_days"),
            "n_mt": agg.get("n_meaningful_trough"),
            "n_mt_with_signal": n_mt_sig,
            "coverage_mt": cov,
        },
    }


def pick_champion_1h(aggs: list[dict[str, Any]]) -> str:
    """Always prefer 1h proxy of 5m champion when catalogued — even if MT cov=0.

    Zero MT coverage is itself an adoption-blocking finding (definition failure on 1h).
    """
    preferred = "D_cool_imp_K60_d0.1"
    by_id = {a["variant_id"]: a for a in aggs}
    if preferred in by_id:
        return preferred
    usable = [a for a in aggs if (a.get("n_mt_with_signal") or 0) >= 1]
    if not usable:
        usable = aggs

    def _key(a: dict[str, Any]) -> tuple:
        fpr = a.get("fpr_sig_on_no_mt")
        head = a.get("median_headroom_vs_trough_pct")
        return (
            float(fpr) if fpr is not None else 9.0,
            float(head) if head is not None else 9.0,
            -(a.get("n_mt_with_signal") or 0),
        )

    return sorted(usable, key=_key)[0]["variant_id"]


def run_panel(
    polls: dict[str, pd.DataFrame],
    *,
    spec: SellSpec,
    variants: list[VariantSpec],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    day_rows = [
        analyze_day_variants(polls[d], spec=spec, variants=variants) for d in sorted(polls)
    ]
    aggs = [aggregate_variant(day_rows, v.id) for v in variants]
    for a, v in zip(aggs, variants):
        a["family"] = v.family
        a["label"] = v.label
    case = None
    if CASE_DAY in polls:
        case = analyze_day_variants(polls[CASE_DAY], spec=spec, variants=variants)
    return day_rows, aggs, case


def render_markdown(payload: dict[str, Any]) -> str:
    meta = payload.get("meta") or {}
    gates_doc = payload.get("adoption_gates_definition") or {}
    p1 = payload.get("panel_1h") or {}
    p5 = payload.get("panel_5m") or {}
    gate_eval = payload.get("gate_evaluation") or {}
    verdict = payload.get("three_way_verdict") or {}
    champ = gate_eval.get("champion_id")
    aggs_1h = {a["variant_id"]: a for a in (p1.get("variant_aggs") or [])}
    champ_agg = aggs_1h.get(champ or "", {})
    is_oos = p1.get("is_oos") or {}

    lines = [
        "# Detach Gate rebuy · long-sample adoption stress",
        "",
        f"- Topic: `detach-gate-trough-find` · long-history 採納 evidence",
        f"- Layer: Research only · **no** Order-layer auto-rebuy (`rebuy: false`)",
        f"- Primary: Yahoo `^TWII` × `NQ=F` **1h** · {meta.get('window_1h')} · "
        f"n={meta.get('n_days_1h')}",
        f"- Secondary: local/Yahoo **5m** · {meta.get('window_5m')} · n={meta.get('n_days_5m')}"
        f" · `{meta.get('tw_source_5m') or 'IX0001'}` × `{meta.get('nq_source_5m') or 'NQ=F'}` · "
        f"TW choice `{meta.get('tw_proxy') or 'IX0001 (^TWII)'}`",
        f"- RED 1h proxy: `{meta.get('frozen_spec_1h')}` "
        f"(same nums as 5m frozen; spread = last hour ≈60m — not rolling 30m)",
        f"- RED 5m frozen: `{meta.get('frozen_spec_5m')}`",
        f"- Label: post-event trough = argmin `tw_from_open` ≥ event",
        f"- Meaningful trough DD ≥ {MEANINGFUL_TROUGH_DD}% · near band ≤ {NEAR_TROUGH_BP} pp",
        f"- 1h mapping: IMPROVE = Δ(hour-spread) ≥ {GAP_EPS}; TF-3 = hour-bar both↑ + δ; "
        f"cooldown ≥30m → **≥60m (≥1 bar)** (cannot pretend 5m polls exist)",
        "",
        "## Analyst adoption gates (pre-registered)",
        "",
        "| gate | rule |",
        "|------|------|",
    ]
    for gid, rule in gates_doc.items():
        lines.append(f"| `{gid}` | {rule} |")

    lines.extend(
        [
            "",
            "## Panel sizes",
            "",
            f"| panel | n days | n RED | n MT |",
            f"|-------|--------|-------|------|",
            f"| 1h | {meta.get('n_days_1h')} | {champ_agg.get('n_event_days')} | "
            f"{champ_agg.get('n_meaningful_trough')} |",
            f"| 1h wide (sens) | {meta.get('n_days_1h')} | "
            f"{((payload.get('panel_1h_wide_sensitivity') or {}).get('n_event_days'))} | "
            f"{((payload.get('panel_1h_wide_sensitivity') or {}).get('n_meaningful_trough'))} |",
            f"| 5m | {meta.get('n_days_5m')} | "
            f"{((p5.get('champion_agg') or {}).get('n_event_days'))} | "
            f"{((p5.get('champion_agg') or {}).get('n_meaningful_trough'))} |",
            "",
            "Wide sensitivity does **not** override primary gates; if even wide fails A1 or "
            "champion MT cov stays ~0, finer history is required before 採納.",
            "",
            f"## Champion · `{champ}` (1h proxy of 5m `D_cool_imp_K30_d0.1`)",
            "",
            f"| metric | full | IS | OOS |",
            f"|--------|------|----|-----|",
            f"| cut date | — | < `{is_oos.get('cut_date')}` | ≥ `{is_oos.get('cut_date')}` |",
            f"| n RED | {champ_agg.get('n_event_days')} | {is_oos.get('n_is')} | {is_oos.get('n_oos')} |",
            f"| FPR(no-MT) | {champ_agg.get('fpr_sig_on_no_mt')} | "
            f"{(is_oos.get('is') or {}).get('fpr_sig_on_no_mt')} | "
            f"{(is_oos.get('oos') or {}).get('fpr_sig_on_no_mt')} |",
            f"| med headroom% | {champ_agg.get('median_headroom_vs_trough_pct')} | "
            f"{(is_oos.get('is') or {}).get('median_headroom_vs_trough_pct')} | "
            f"{(is_oos.get('oos') or {}).get('median_headroom_vs_trough_pct')} |",
            f"| % before trough | {champ_agg.get('fraction_before_trough')} | "
            f"{(is_oos.get('is') or {}).get('fraction_before_trough')} | "
            f"{(is_oos.get('oos') or {}).get('fraction_before_trough')} |",
            f"| near-trough | {champ_agg.get('buy_near_trough_rate')} | "
            f"{(is_oos.get('is') or {}).get('buy_near_trough_rate')} | "
            f"{(is_oos.get('oos') or {}).get('buy_near_trough_rate')} |",
            f"| cov MT | {champ_agg.get('coverage_mt')} | — | — |",
            f"| n MT with signal | {champ_agg.get('n_mt_with_signal')} | — | — |",
            "",
            "### Wide RED sensitivity (same champion)",
            "",
            f"- Spec: `{(payload.get('panel_1h_wide_sensitivity') or {}).get('frozen_spec_id')}`",
            f"- n RED / MT: {(payload.get('panel_1h_wide_sensitivity') or {}).get('n_event_days')} / "
            f"{(payload.get('panel_1h_wide_sensitivity') or {}).get('n_meaningful_trough')}",
            f"- FPR / head / near / cov: "
            f"{((payload.get('panel_1h_wide_sensitivity') or {}).get('champion_agg') or {}).get('fpr_sig_on_no_mt')} / "
            f"{((payload.get('panel_1h_wide_sensitivity') or {}).get('champion_agg') or {}).get('median_headroom_vs_trough_pct')} / "
            f"{((payload.get('panel_1h_wide_sensitivity') or {}).get('champion_agg') or {}).get('buy_near_trough_rate')} / "
            f"{((payload.get('panel_1h_wide_sensitivity') or {}).get('champion_agg') or {}).get('coverage_mt')}",
            "",
            "## Gate pass/fail",
            "",
            "| gate | pass | detail |",
            "|------|------|--------|",
        ]
    )
    for gid, g in (gate_eval.get("gates") or {}).items():
        mark = "PASS" if g.get("pass") else "FAIL"
        lines.append(f"| `{gid}` | **{mark}** | {g.get('detail')} |")
    lines.append(
        f"| **score** | **{gate_eval.get('n_pass')}/{gate_eval.get('n_gates')}** | "
        f"all_pass={gate_eval.get('all_pass')} |"
    )

    lines.extend(
        [
            "",
            "## 1h variant table (headline)",
            "",
            "| variant | n MT sig | % before | med headroom% | near | FPR(no-MT) | cov |",
            "|---------|----------|----------|---------------|------|------------|-----|",
        ]
    )
    for a in p1.get("variant_aggs") or []:
        lines.append(
            f"| `{a.get('variant_id')}` | {a.get('n_mt_with_signal')} | "
            f"{a.get('fraction_before_trough')} | {a.get('median_headroom_vs_trough_pct')} | "
            f"{a.get('buy_near_trough_rate')} | {a.get('fpr_sig_on_no_mt')} | "
            f"{a.get('coverage_mt')} |"
        )

    if p5.get("variant_aggs"):
        lines.extend(
            [
                "",
                "## 5m high-res check",
                "",
                "| variant | n MT sig | % before | med headroom% | near | FPR(no-MT) |",
                "|---------|----------|----------|---------------|------|------------|",
            ]
        )
        for a in p5.get("variant_aggs") or []:
            lines.append(
                f"| `{a.get('variant_id')}` | {a.get('n_mt_with_signal')} | "
                f"{a.get('fraction_before_trough')} | {a.get('median_headroom_vs_trough_pct')} | "
                f"{a.get('buy_near_trough_rate')} | {a.get('fpr_sig_on_no_mt')} |"
            )

    case = p1.get("case_20260714") or {}
    lines.extend(["", f"## Case · {CASE_DAY} (1h)", ""])
    if not case or not case.get("has_event"):
        lines.append(f"- No RED / not in 1h panel for {CASE_DAY}.")
    else:
        sig = (case.get("signals") or {}).get(champ or "") or {}
        lines.extend(
            [
                f"- Event RED @ **{case.get('event_poll')}** · TW {case.get('event_tw_from_open')}% · "
                f"spread {case.get('event_spread_30m')}",
                f"- Post-RED trough @ **{case.get('post_event_trough_poll')}** · "
                f"TW {case.get('post_event_trough_pct')}% · DD {case.get('post_event_dd_pct')}%",
                f"- IMPROVE arm @ **{case.get('improve_poll')}**",
                f"- Champion `{champ}` @ **{sig.get('poll')}** · before={sig.get('before_trough')} · "
                f"headroom={sig.get('headroom_vs_trough_pct')}%",
            ]
        )

    a_v = verdict.get("A_manual_sop") or {}
    b_v = verdict.get("B_strategy_advisory") or {}
    c_v = verdict.get("C_order_auto_rebuy") or {}
    lines.extend(
        [
            "",
            "## Three-way 採納 verdict",
            "",
            f"| track | decision | note |",
            f"|-------|----------|------|",
            f"| (A) human manual rebuy SOP | **{a_v.get('decision')}** | {a_v.get('note')} |",
            f"| (B) Strategy layer advisory | **{b_v.get('decision')}** | {b_v.get('note')} |",
            f"| (C) Order auto-rebuy | **{c_v.get('decision')}** | {c_v.get('note')} |",
            "",
            "### Failing gates",
            "",
        ]
    )
    fails = verdict.get("failing_gates") or []
    if fails:
        for f in fails:
            lines.append(f"- {f}")
    else:
        lines.append("- (none)")

    lines.extend(
        [
            "",
            "## Caveats",
            "",
            f"- {verdict.get('coarseness_caveat')}",
            f"- {meta.get('local_5m_note') or ''}".rstrip(),
            "- Yahoo 1h hard-cap ~730 calendar days; hourly TW bars coarsen arm clock "
            "(often first eligible ~10:00).",
            "- Post-event trough on close-of-hour ≠ true session low path.",
            "- Prior 5m short panel remains the operational resolution for live Detach Gate polls.",
            "- Research hypothesis only until Strategy dual-register; Order `rebuy` stays false.",
            "",
        ]
    )
    return "\n".join(lines)


def run_study(
    *,
    date_end: str | date | None = None,
    lookback_days: int = LOOKBACK_1H_DAYS,
    out_dir: Path | None = None,
    include_5m: bool = True,
) -> dict[str, Any]:
    polls_1h = load_day_polls_1h(date_end=date_end, lookback_days=lookback_days)
    spec_1h = frozen_red_spec_1h()
    variants_1h = variant_catalog_1h()
    day_1h, aggs_1h, case_1h = run_panel(polls_1h, spec=spec_1h, variants=variants_1h)
    champ = pick_champion_1h(aggs_1h)
    is_oos = _split_is_oos(day_1h, champ)
    champ_agg = next(a for a in aggs_1h if a["variant_id"] == champ)

    # Wider RED sensitivity — expand n for sample-floor stress (not the primary gates)
    spec_1h_wide = frozen_red_spec_1h_wide()
    day_1h_wide, aggs_1h_wide, _case_w = run_panel(
        polls_1h, spec=spec_1h_wide, variants=variants_1h
    )
    champ_wide_agg = next(a for a in aggs_1h_wide if a["variant_id"] == champ)
    is_oos_wide = _split_is_oos(day_1h_wide, champ)
    panel_1h_wide = {
        "frozen_spec_id": spec_1h_wide.strategy_id,
        "champion_id": champ,
        "champion_agg": champ_wide_agg,
        "variant_aggs": aggs_1h_wide,
        "is_oos": is_oos_wide,
        "n_event_days": champ_wide_agg.get("n_event_days"),
        "n_meaningful_trough": champ_wide_agg.get("n_meaningful_trough"),
        "note": (
            "Sensitivity only: thr=-0.5 · no tw_from_open down filter. "
            "Does not replace primary frozen-proxy gates."
        ),
    }

    panel_5m: dict[str, Any] = {}
    champ_5m_agg = None
    if include_5m:
        try:
            polls_5m = load_day_polls_5m(date_end=date_end)
            panel_meta_5m = getattr(polls_5m, "_detach_panel_meta", {}) or {}
            spec_5m = frozen_sell_gate_spec()
            variants_5m = variant_catalog_5m_check()
            day_5m, aggs_5m, case_5m = run_panel(polls_5m, spec=spec_5m, variants=variants_5m)
            # map: prefer exact 5m champion id
            id_5m = "D_cool_imp_K30_d0.1"
            champ_5m_agg = next((a for a in aggs_5m if a["variant_id"] == id_5m), aggs_5m[0] if aggs_5m else None)
            panel_5m = {
                "variant_aggs": aggs_5m,
                "case_20260714": case_5m,
                "champion_id": id_5m,
                "champion_agg": champ_5m_agg,
                "n_days": len(polls_5m),
                "window": f"{min(polls_5m)} → {max(polls_5m)}" if polls_5m else None,
                "n_event_rows": sum(1 for r in day_5m if r.get("has_event")),
                **panel_meta_5m,
            }
        except Exception as exc:  # noqa: BLE001 — secondary panel best-effort
            panel_5m = {"error": str(exc), "variant_aggs": []}

    gate_eval = evaluate_gates(
        champion_id=champ,
        agg=champ_agg,
        is_oos=is_oos,
        day_rows=day_1h,
        case_row=case_1h,
        panel_5m_agg=champ_5m_agg,
    )
    verdict = three_way_verdict(gate_eval, champ_agg)

    adoption_gates_definition = {
        "A1_sample_floor": f"n_RED ≥ {GATE_N_RED} and n_MT ≥ {GATE_N_MT}",
        "A2_is_oos_stability": (
            f"IS & OOS FPR ≤ {GATE_FPR}, or both ≤ {GATE_FPR_SOFT_CAP} with "
            f"|IS−OOS| ≤ {GATE_IS_OOS_ABS}"
        ),
        "A3_fpr_no_mt": f"full-sample FPR(no-MT) ≤ {GATE_FPR}",
        "A4_headroom_or_near": (
            f"median headroom ≤ {GATE_MED_HEADROOM}% OR near-trough rate > 0 with Wilson CI lo > 0"
        ),
        "A5_case_consistency": f"on {CASE_DAY}, champion fires before post-event trough when RED present",
        "A6_5m_pool_consistency": (
            "5m D_cool_imp_K30 FPR/headroom not > prior short-panel (0.75 / 0.82) by +0.15 / +0.5pp"
        ),
    }

    meta = {
        "n_days_1h": len(polls_1h),
        "window_1h": f"{min(polls_1h)} → {max(polls_1h)}" if polls_1h else None,
        "n_days_5m": panel_5m.get("n_days"),
        "window_5m": panel_5m.get("window"),
        "tw_source_5m": panel_5m.get("tw_source"),
        "nq_source_5m": panel_5m.get("nq_source"),
        "tw_proxy": panel_5m.get("tw_proxy")
        or "IX0001 (^TWII) — FinMind index K N/A; 0050 FinMind longer liquid proxy but not auto-swapped",
        "frozen_spec_1h": spec_1h.strategy_id,
        "frozen_spec_5m": frozen_sell_gate_spec().strategy_id,
        "lookback_days_1h": lookback_days,
        "gap_eps": GAP_EPS,
        "meaningful_trough_dd_pct": MEANINGFUL_TROUGH_DD,
        "near_trough_bp": NEAR_TROUGH_BP,
        "topic": "detach-gate-trough-find",
        "hypothesis": "TF-2∩TF-3 long-sample rebuy adoption",
        "layer": "research",
        "order_rebuy": False,
        "primary_case_day": CASE_DAY,
        "1h_distortion": {
            "spread_window": "last completed hour ≈60m (not rolling 30m)",
            "cooldown_ge30m_proxy": "≥60m (≥1 hourly bar)",
            "tf3_resolution": "hour-bar ROC, not 5m poll-to-poll",
        },
        "local_5m_note": (
            "0050 FinMind stock_kbar_5m now through latest session (~600d); "
            "NQ=F still Yahoo ~60d hard-cap — calendar intersection grows only via daily NQ upsert"
        ),
    }

    payload: dict[str, Any] = {
        "meta": meta,
        "adoption_gates_definition": adoption_gates_definition,
        "panel_1h": {
            "variant_aggs": aggs_1h,
            "is_oos": is_oos,
            "case_20260714": case_1h,
            "champion_id": champ,
            "champion_agg": champ_agg,
            "n_event_days": champ_agg.get("n_event_days"),
            "days_with_event": [r for r in day_1h if r.get("has_event")],
        },
        "panel_1h_wide_sensitivity": panel_1h_wide,
        "panel_5m": panel_5m,
        "gate_evaluation": gate_eval,
        "three_way_verdict": verdict,
    }
    markdown = render_markdown(payload)

    if out_dir is not None:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "rebuy_adoption_summary.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(markdown, encoding="utf-8")
        pd.DataFrame(aggs_1h).to_csv(out_dir / "variant_aggs_1h.csv", index=False)
        if panel_5m.get("variant_aggs"):
            pd.DataFrame(panel_5m["variant_aggs"]).to_csv(
                out_dir / "variant_aggs_5m.csv", index=False
            )

    return {"payload": payload, "markdown": markdown, "polls_1h": polls_1h}
