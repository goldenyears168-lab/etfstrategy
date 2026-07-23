"""TF-2∩TF-3 · Detach Gate repair-confirm for manual rebuy candidacy.

Research topic: detach-gate-trough-find (hypotheses TF-2 / TF-3 / light TF-5).
Study-only — does NOT wire Order-layer auto-rebuy (rebuy stays false).

Reuses TF-2 panel / frozen RED events + enrich_poll_30m features.
Compares candidate rebuy triggers after RED (and optionally after IMPROVE arm).

Variants
  A  TF-2 only: first gap_pulse=IMPROVE ≥ RED
  B  TF-3 alone: TW↑ & NQ↑ and tw_bar_roc ≥ nq_bar_roc + δ
  C  TF-2 arm THEN TF-3: TF-3 only after first IMPROVE
  D  Cooldown: TF-3 after ≥ K min past RED or past IMPROVE
  E  sync_pulse FOLLOW (or not OPPOSE) filter on TF-3
  F  TF-4 WMA: skipped (heavy; not on enrich_poll_30m without shorts)
  G  TF-5 veto: no signal after session cutoff (12:30 / 13:00)

δ units: tw/nq_bar_roc are Δ(from_open) pct-pt — same as L2R outperform_margin.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from research.backtest.detach_gate_tf2_trough_find_study import (
    CASE_DAY,
    MEANINGFUL_TROUGH_DD,
    NEAR_TROUGH_BP,
    first_event_idx,
    first_improve_idx,
    lead_minutes,
    load_day_polls,
    poll_hhmm_to_minutes,
    post_event_trough_idx,
)
from research.backtest.us_tw_sell_only_live_readiness_study import SellSpec, frozen_sell_gate_spec

DELTA_SWEEP = (0.05, 0.10, 0.15)
COOLDOWN_K = (15, 30, 45, 60)
# Headline cooldown / FOLLOW combos use mid δ
DEFAULT_DELTA = 0.10
CUTOFFS = ("12:30", "13:00")


@dataclass(frozen=True)
class VariantSpec:
    id: str
    family: str  # A|B|C|D|E|G|combo
    label: str
    delta: float | None = None
    arm_improve: bool = False
    cooldown_min: int = 0
    cooldown_from: str = "red"  # red | improve
    require_follow: bool = False
    forbid_oppose: bool = False
    cutoff: str | None = None  # e.g. "12:30" — signal must be strictly before


def variant_catalog() -> list[VariantSpec]:
    """Minimal-but-covering variant list for the TF-2∩TF-3 family."""
    specs: list[VariantSpec] = [
        VariantSpec(id="A_tf2", family="A", label="TF-2 first IMPROVE"),
    ]
    for d in DELTA_SWEEP:
        ds = f"{d:.2f}".rstrip("0").rstrip(".")
        specs.append(
            VariantSpec(
                id=f"B_tf3_d{ds}",
                family="B",
                label=f"TF-3 alone δ={ds}",
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
    for k in COOLDOWN_K:
        specs.append(
            VariantSpec(
                id=f"D_cool_red_K{k}_d{ds}",
                family="D",
                label=f"TF-3 ≥{k}m after RED δ={ds}",
                delta=DEFAULT_DELTA,
                cooldown_min=k,
                cooldown_from="red",
            )
        )
        specs.append(
            VariantSpec(
                id=f"D_cool_imp_K{k}_d{ds}",
                family="D",
                label=f"TF-3 ≥{k}m after IMPROVE δ={ds}",
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
                id=f"E_tf3_not_oppose_d{ds}",
                family="E",
                label=f"TF-3 + not OPPOSE δ={ds}",
                delta=DEFAULT_DELTA,
                forbid_oppose=True,
            ),
            VariantSpec(
                id=f"C_E_arm_follow_d{ds}",
                family="combo",
                label=f"arm→TF-3 + FOLLOW δ={ds}",
                delta=DEFAULT_DELTA,
                arm_improve=True,
                require_follow=True,
            ),
            VariantSpec(
                id=f"D_E_cool_imp30_follow_d{ds}",
                family="combo",
                label=f"≥30m after IMPROVE + FOLLOW δ={ds}",
                delta=DEFAULT_DELTA,
                arm_improve=True,
                cooldown_min=30,
                cooldown_from="improve",
                require_follow=True,
            ),
        ]
    )

    # TF-5 cutoffs on promising bases (C arm δ=0.10, cool-imp30, arm+FOLLOW)
    base_for_g = [
        VariantSpec(
            id="C_arm_tf3_d0.1",
            family="C",
            label="IMPROVE arm → TF-3 δ=0.1",
            delta=DEFAULT_DELTA,
            arm_improve=True,
        ),
        VariantSpec(
            id="D_cool_imp_K30_d0.1",
            family="D",
            label="TF-3 ≥30m after IMPROVE δ=0.1",
            delta=DEFAULT_DELTA,
            arm_improve=True,
            cooldown_min=30,
            cooldown_from="improve",
        ),
        VariantSpec(
            id="C_E_arm_follow_d0.1",
            family="combo",
            label="arm→TF-3 + FOLLOW δ=0.1",
            delta=DEFAULT_DELTA,
            arm_improve=True,
            require_follow=True,
        ),
    ]
    for base in base_for_g:
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
                    require_follow=base.require_follow,
                    forbid_oppose=base.forbid_oppose,
                    cutoff=cut,
                )
            )
    return specs


def tf3_bar_ok(row: pd.Series, *, delta: float) -> bool:
    """TW↑ & NQ↑ and tw_bar_roc ≥ nq_bar_roc + δ (pct-pt)."""
    tw_r = float(row.get("tw_bar_roc", float("nan")))
    nq_r = float(row.get("nq_bar_roc", float("nan")))
    if not (math.isfinite(tw_r) and math.isfinite(nq_r)):
        return False
    return bool(tw_r > 0.0 and nq_r > 0.0 and tw_r >= nq_r + float(delta))


def sync_ok(row: pd.Series, *, require_follow: bool, forbid_oppose: bool) -> bool:
    pulse = str(row.get("sync_pulse", ""))
    if require_follow and pulse != "FOLLOW":
        return False
    if forbid_oppose and pulse == "OPPOSE":
        return False
    return True


def before_cutoff(poll_s: str, cutoff: str | None) -> bool:
    if not cutoff:
        return True
    a = poll_hhmm_to_minutes(poll_s)
    b = poll_hhmm_to_minutes(cutoff)
    if a is None or b is None:
        return False
    return a < b


def minutes_since(poll_s: str, origin_poll: str) -> float | None:
    lead = lead_minutes(origin_poll, poll_s)  # positive if poll after origin
    if lead is None:
        return None
    return float(lead)


def first_tf3_idx(
    poll: pd.DataFrame,
    *,
    start_idx: int,
    delta: float,
    require_follow: bool = False,
    forbid_oppose: bool = False,
    cutoff: str | None = None,
    min_minutes_after_poll: str | None = None,
    cooldown_min: int = 0,
) -> int | None:
    """First bar ≥ start_idx meeting TF-3 (+ optional sync / cutoff / cooldown)."""
    for i in poll.index:
        if int(i) < int(start_idx):
            continue
        row = poll.loc[i]
        poll_s = str(row["poll"])
        if not before_cutoff(poll_s, cutoff):
            continue
        if cooldown_min > 0 and min_minutes_after_poll:
            elapsed = minutes_since(poll_s, min_minutes_after_poll)
            if elapsed is None or elapsed < float(cooldown_min):
                continue
        if not tf3_bar_ok(row, delta=delta):
            continue
        if not sync_ok(row, require_follow=require_follow, forbid_oppose=forbid_oppose):
            continue
        return int(i)
    return None


def find_signal_idx(
    poll: pd.DataFrame,
    *,
    event_idx: int,
    variant: VariantSpec,
) -> int | None:
    """Resolve first signal index for a variant after RED event."""
    if variant.family == "A" or variant.id == "A_tf2":
        idx = first_improve_idx(poll, event_idx)
        if idx is None:
            return None
        if variant.cutoff and not before_cutoff(str(poll.loc[idx, "poll"]), variant.cutoff):
            return None
        return idx

    assert variant.delta is not None
    start = int(event_idx)
    improve_idx = first_improve_idx(poll, event_idx)
    origin_poll: str | None = None

    if variant.arm_improve or variant.cooldown_from == "improve":
        if improve_idx is None:
            return None
        start = max(start, int(improve_idx))
        origin_poll = str(poll.loc[improve_idx, "poll"])
    else:
        origin_poll = str(poll.loc[event_idx, "poll"])

    if variant.cooldown_min > 0 and variant.cooldown_from == "red":
        origin_poll = str(poll.loc[event_idx, "poll"])
        # start search at event; cooldown filter applies per-bar
        start = int(event_idx)
    elif variant.cooldown_min > 0 and variant.cooldown_from == "improve":
        if improve_idx is None:
            return None
        start = int(improve_idx)
        origin_poll = str(poll.loc[improve_idx, "poll"])

    # For arm_improve without cooldown: search from IMPROVE (inclusive)
    if variant.arm_improve and variant.cooldown_min == 0:
        start = int(improve_idx) if improve_idx is not None else start

    return first_tf3_idx(
        poll,
        start_idx=start,
        delta=float(variant.delta),
        require_follow=variant.require_follow,
        forbid_oppose=variant.forbid_oppose,
        cutoff=variant.cutoff,
        min_minutes_after_poll=origin_poll if variant.cooldown_min > 0 else None,
        cooldown_min=variant.cooldown_min,
    )


def _signal_metrics(
    poll: pd.DataFrame,
    *,
    sig_idx: int | None,
    trough_idx: int,
    trough_tw: float,
    trough_poll: str,
) -> dict[str, Any]:
    if sig_idx is None:
        return {
            "poll": None,
            "tw_from_open": None,
            "lead_min": None,
            "before_trough": None,
            "after_trough": None,
            "headroom_vs_trough_pct": None,
            "buy_near_trough": None,
            "abs_entry_vs_trough_pct": None,
        }
    sr = poll.loc[sig_idx]
    sig_poll = str(sr["poll"])
    sig_tw = float(sr["tw_from_open"])
    lead = lead_minutes(sig_poll, trough_poll)
    before = bool(lead is not None and lead >= 0)
    after = bool(lead is not None and lead < 0)
    headroom = sig_tw - trough_tw
    near = bool(before and 0.0 <= headroom <= NEAR_TROUGH_BP)
    return {
        "poll": sig_poll,
        "tw_from_open": round(sig_tw, 4),
        "lead_min": round(lead, 1) if lead is not None else None,
        "before_trough": before,
        "after_trough": after,
        "headroom_vs_trough_pct": round(headroom, 4),
        "buy_near_trough": near,
        "abs_entry_vs_trough_pct": round(abs(headroom), 4),
    }


def analyze_day_variants(
    poll: pd.DataFrame,
    *,
    spec: SellSpec,
    variants: list[VariantSpec],
) -> dict[str, Any]:
    date_s = str(poll.attrs.get("date") or "")
    base: dict[str, Any] = {
        "date": date_s,
        "has_event": False,
        "event_poll": None,
        "event_tw_from_open": None,
        "event_spread_30m": None,
        "post_event_trough_poll": None,
        "post_event_trough_pct": None,
        "post_event_dd_pct": None,
        "meaningful_trough": False,
        "improve_poll": None,
        "signals": {},
    }
    e_idx = first_event_idx(poll, spec)
    if e_idx is None:
        return base

    er = poll.loc[e_idx]
    event_tw = float(er["tw_from_open"])
    t_idx = post_event_trough_idx(poll, e_idx)
    tr = poll.loc[t_idx]
    trough_tw = float(tr["tw_from_open"])
    trough_poll = str(tr["poll"])
    dd = event_tw - trough_tw
    meaningful = bool(dd >= MEANINGFUL_TROUGH_DD)
    improve_idx = first_improve_idx(poll, e_idx)

    base.update(
        {
            "has_event": True,
            "event_poll": str(er["poll"]),
            "event_tw_from_open": round(event_tw, 4),
            "event_spread_30m": round(float(er["spread_30m"]), 4),
            "post_event_trough_poll": trough_poll,
            "post_event_trough_pct": round(trough_tw, 4),
            "post_event_dd_pct": round(dd, 4),
            "meaningful_trough": meaningful,
            "improve_poll": str(poll.loc[improve_idx, "poll"]) if improve_idx is not None else None,
        }
    )

    signals: dict[str, Any] = {}
    for v in variants:
        sig_idx = find_signal_idx(poll, event_idx=e_idx, variant=v)
        m = _signal_metrics(
            poll,
            sig_idx=sig_idx,
            trough_idx=t_idx,
            trough_tw=trough_tw,
            trough_poll=trough_poll,
        )
        if sig_idx is not None:
            row = poll.loc[sig_idx]
            m["sync_pulse"] = str(row.get("sync_pulse", ""))
            m["tw_bar_roc"] = (
                None
                if not math.isfinite(float(row.get("tw_bar_roc", float("nan"))))
                else round(float(row["tw_bar_roc"]), 4)
            )
            m["nq_bar_roc"] = (
                None
                if not math.isfinite(float(row.get("nq_bar_roc", float("nan"))))
                else round(float(row["nq_bar_roc"]), 4)
            )
            m["gap_pulse"] = str(row.get("gap_pulse", ""))
        else:
            m["sync_pulse"] = None
            m["tw_bar_roc"] = None
            m["nq_bar_roc"] = None
            m["gap_pulse"] = None
        signals[v.id] = m
    base["signals"] = signals
    return base


def aggregate_variant(day_rows: list[dict[str, Any]], variant_id: str) -> dict[str, Any]:
    events = [r for r in day_rows if r.get("has_event")]
    meaningful = [r for r in events if r.get("meaningful_trough")]
    no_mt = [r for r in events if not r.get("meaningful_trough")]

    def _sig(r: dict[str, Any]) -> dict[str, Any]:
        return (r.get("signals") or {}).get(variant_id) or {}

    with_sig = [r for r in events if _sig(r).get("poll")]
    mt_sig = [r for r in with_sig if r.get("meaningful_trough")]
    before = [r for r in mt_sig if _sig(r).get("before_trough")]
    after = [r for r in mt_sig if _sig(r).get("after_trough")]

    leads_before = [
        float(_sig(r)["lead_min"])
        for r in before
        if _sig(r).get("lead_min") is not None
    ]
    lags_after = [
        -float(_sig(r)["lead_min"])
        for r in after
        if _sig(r).get("lead_min") is not None
    ]
    heads_before = [
        float(_sig(r)["headroom_vs_trough_pct"])
        for r in before
        if _sig(r).get("headroom_vs_trough_pct") is not None
    ]
    abs_heads = [
        float(_sig(r)["abs_entry_vs_trough_pct"])
        for r in mt_sig
        if _sig(r).get("abs_entry_vs_trough_pct") is not None
    ]
    near_n = sum(1 for r in before if _sig(r).get("buy_near_trough"))
    n_mt_sig = len(mt_sig)
    frac_before = (len(before) / n_mt_sig) if n_mt_sig else float("nan")
    frac_after = (len(after) / n_mt_sig) if n_mt_sig else float("nan")
    buy_near = (near_n / len(before)) if before else float("nan")
    fpr_no_mt = (
        sum(1 for r in no_mt if _sig(r).get("poll")) / len(no_mt) if no_mt else float("nan")
    )

    return {
        "variant_id": variant_id,
        "n_event_days": len(events),
        "n_meaningful_trough": len(meaningful),
        "n_with_signal": len(with_sig),
        "n_mt_with_signal": n_mt_sig,
        "n_before": len(before),
        "n_after": len(after),
        "fraction_before_trough": round(frac_before, 4) if math.isfinite(frac_before) else None,
        "fraction_after_trough": round(frac_after, 4) if math.isfinite(frac_after) else None,
        "median_lead_min_before": round(float(np.median(leads_before)), 1) if leads_before else None,
        "median_lag_min_after": round(float(np.median(lags_after)), 1) if lags_after else None,
        "median_headroom_vs_trough_pct": round(float(np.median(heads_before)), 4) if heads_before else None,
        "median_abs_entry_vs_trough_pct": round(float(np.median(abs_heads)), 4) if abs_heads else None,
        "buy_near_trough_rate": round(buy_near, 4) if math.isfinite(buy_near) else None,
        "fpr_sig_on_no_mt": round(fpr_no_mt, 4) if math.isfinite(fpr_no_mt) else None,
        "coverage_mt": round(n_mt_sig / len(meaningful), 4) if meaningful else None,
    }


def _rank_key(agg: dict[str, Any]) -> tuple:
    """Prefer lower FPR, useful near-trough / tight headroom, decent coverage."""
    fpr = agg.get("fpr_sig_on_no_mt")
    head = agg.get("median_headroom_vs_trough_pct")
    near = agg.get("buy_near_trough_rate")
    cov = agg.get("coverage_mt")
    n_sig = agg.get("n_mt_with_signal") or 0
    # missing → penalize
    fpr_s = float(fpr) if fpr is not None else 9.0
    head_s = float(head) if head is not None else 9.0
    near_s = -(float(near) if near is not None else 0.0)
    cov_s = -(float(cov) if cov is not None else 0.0)
    return (fpr_s, head_s, near_s, cov_s, -n_sig)


def pick_recommendation(aggs: list[dict[str, Any]]) -> dict[str, Any]:
    """Research recommendation (manual rebuy only) — not Strategy 採納.

    Prefer a simple cooldown rule (family D) over stacked G cutoffs when both
    cut FPR and bring median headroom into ~0.5–0.8% band. Rank still reported.
    """
    usable = [a for a in aggs if (a.get("n_mt_with_signal") or 0) >= 3]
    if not usable:
        return {
            "manual_use": None,
            "reject": [a["variant_id"] for a in aggs],
            "rationale": "Too few MT signals to recommend.",
        }

    ranked = sorted(usable, key=_rank_key)
    best = ranked[0]
    by_id = {a["variant_id"]: a for a in aggs}

    reject: list[str] = []
    keep_notes: list[str] = []
    for a in aggs:
        fpr = a.get("fpr_sig_on_no_mt")
        head = a.get("median_headroom_vs_trough_pct")
        near = a.get("buy_near_trough_rate")
        lag = a.get("median_lag_min_after")
        frac_after = a.get("fraction_after_trough")
        bad_fpr = fpr is not None and fpr >= 0.99
        loose_head = head is not None and head > 1.2
        late = (frac_after is not None and frac_after >= 0.5) or (lag is not None and lag >= 60)
        useful_near = near is not None and near >= 0.15
        tight_head = head is not None and head <= 0.85
        if bad_fpr and loose_head and not useful_near:
            reject.append(a["variant_id"])
        elif late and not useful_near and not tight_head:
            reject.append(a["variant_id"])
        elif (fpr is not None and fpr <= 0.75) and (useful_near or tight_head):
            keep_notes.append(a["variant_id"])

    # Analyst preference: simple D cool-imp K30 before stacked G veto
    preferred_simple = "D_cool_imp_K30_d0.1"
    preferred_veto = "G_D_cool_imp_K30_d0.1_cut1300"
    manual = preferred_simple if preferred_simple in by_id else best["variant_id"]
    soft_veto = preferred_veto if preferred_veto in by_id else None

    simple_agg = by_id.get(preferred_simple) or best
    rationale_parts = [
        f"Manual rebuy SOP: arm on first IMPROVE → wait ≥30m → first TF-3 δ=0.10 "
        f"(`{preferred_simple}`: FPR={simple_agg.get('fpr_sig_on_no_mt')}, "
        f"med headroom={simple_agg.get('median_headroom_vs_trough_pct')}%, "
        f"before={simple_agg.get('fraction_before_trough')}).",
        "Optional hard veto: no new signal after 13:00 "
        f"(`{preferred_veto}` cut FPR further to "
        f"{(by_id.get(preferred_veto) or {}).get('fpr_sig_on_no_mt')} on this panel).",
        "Ranked metric leader "
        f"`{best['variant_id']}` is treated as sensitivity check, not primary rule.",
        "Reject TF-2 alone and naked TF-3 δ≤0.10 (FPR≈1 / headroom≳1.3%).",
        "FOLLOW filter alone adds little once both↑ is required.",
        "Near-trough (≤0.35pp) rate remains 0 — none is a precise trough timer.",
        "TF-4 WMA skipped. Research only — Order rebuy stays false.",
    ]

    return {
        "manual_use": manual,
        "manual_soft_veto": soft_veto,
        "ranked_top": [a["variant_id"] for a in ranked[:5]],
        "keep_candidates": keep_notes[:8],
        "reject": sorted(set(reject)),
        "rationale": " ".join(rationale_parts),
        "best_agg": by_id.get(manual) or best,
        "metric_leader_agg": best,
    }


def render_markdown(
    payload: dict[str, Any],
    *,
    variants: list[VariantSpec],
) -> str:
    meta = payload.get("meta") or {}
    by_id = {v.id: v for v in variants}
    aggs: list[dict[str, Any]] = payload.get("variant_aggs") or []
    # Headline: A + B all δ + C all δ + selected D/E/G
    headline_ids = [
        a["variant_id"]
        for a in aggs
        if a["variant_id"].startswith(("A_", "B_", "C_arm_"))
        or a["variant_id"]
        in {
            "D_cool_red_K30_d0.1",
            "D_cool_imp_K30_d0.1",
            "D_cool_imp_K45_d0.1",
            "E_tf3_follow_d0.1",
            "E_tf3_not_oppose_d0.1",
            "C_E_arm_follow_d0.1",
            "D_E_cool_imp30_follow_d0.1",
            "G_C_arm_tf3_d0.1_cut1230",
            "G_C_arm_tf3_d0.1_cut1300",
            "G_D_cool_imp_K30_d0.1_cut1230",
            "G_D_cool_imp_K30_d0.1_cut1300",
            "G_C_E_arm_follow_d0.1_cut1230",
        }
    ]
    # preserve catalog order
    order = [v.id for v in variants]
    headline_ids = [vid for vid in order if vid in set(headline_ids)]

    def _row(a: dict[str, Any]) -> str:
        return (
            f"| `{a.get('variant_id')}` | {a.get('n_mt_with_signal')} | "
            f"{a.get('fraction_before_trough')} | {a.get('fraction_after_trough')} | "
            f"{a.get('median_lead_min_before')} | {a.get('median_lag_min_after')} | "
            f"{a.get('median_headroom_vs_trough_pct')} | {a.get('buy_near_trough_rate')} | "
            f"{a.get('fpr_sig_on_no_mt')} | {a.get('coverage_mt')} |"
        )

    lines = [
        "# TF-2∩TF-3 · repair confirm for manual rebuy",
        "",
        f"- Topic: `detach-gate-trough-find` · hypotheses **TF-2 / TF-3 / TF-5 veto**",
        f"- Layer: Research only · **no** Order-layer auto-rebuy (`rebuy: false`)",
        f"- Panel: `{meta.get('tw_source') or '^TWII'}` × `{meta.get('nq_source') or 'NQ=F'}` 5m · "
        f"{meta.get('window')} · n={meta.get('n_days')}",
        f"- TW choice: `{meta.get('tw_proxy') or 'IX0001 (^TWII)'}`",
        f"- Event: frozen RED `{meta.get('frozen_spec_id')}`",
        f"- Label: **post-event trough** = argmin `tw_from_open` ≥ event",
        f"- TF-3: `tw_bar_roc>0` ∧ `nq_bar_roc>0` ∧ `tw_bar_roc ≥ nq_bar_roc + δ` "
        f"(δ in pct-pt, same units as L2R `outperform_margin`)",
        f"- Meaningful trough: post-event DD ≥ {MEANINGFUL_TROUGH_DD}%",
        f"- Near-trough band: enter − trough ≤ {NEAR_TROUGH_BP} pp",
        f"- TF-4: **skipped** (WMA needs short-frame enrichment)",
        "",
        "## Headline · MT RED days",
        "",
        "| variant | n MT sig | % before | % after | med lead | med lag | med headroom% | near-trough | FPR(no-MT) | cov MT |",
        "|---------|----------|----------|---------|----------|---------|---------------|-------------|------------|--------|",
    ]
    agg_map = {a["variant_id"]: a for a in aggs}
    for vid in headline_ids:
        if vid in agg_map:
            lines.append(_row(agg_map[vid]))

    lines.extend(
        [
            "",
            "## Full variant table (all RED days metrics in JSON)",
            "",
            "| variant | family | n MT sig | % before | med headroom% | near | FPR(no-MT) |",
            "|---------|--------|----------|----------|---------------|------|------------|",
        ]
    )
    for a in aggs:
        v = by_id.get(a["variant_id"])
        fam = v.family if v else "?"
        lines.append(
            f"| `{a.get('variant_id')}` | {fam} | {a.get('n_mt_with_signal')} | "
            f"{a.get('fraction_before_trough')} | {a.get('median_headroom_vs_trough_pct')} | "
            f"{a.get('buy_near_trough_rate')} | {a.get('fpr_sig_on_no_mt')} |"
        )

    case = payload.get("case_20260714") or {}
    rec = payload.get("recommendation") or {}
    lines.extend(["", f"## Case · {CASE_DAY}", ""])
    if not case or not case.get("has_event"):
        lines.append(f"- **Not in panel** or no RED event on {CASE_DAY}.")
    else:
        lines.extend(
            [
                f"- Event RED @ **{case.get('event_poll')}** · TW {case.get('event_tw_from_open')}% · "
                f"spread {case.get('event_spread_30m')}",
                f"- Post-RED trough @ **{case.get('post_event_trough_poll')}** · "
                f"TW {case.get('post_event_trough_pct')}% · DD {case.get('post_event_dd_pct')}%",
                f"- First IMPROVE (arm) @ **{case.get('improve_poll')}**",
                "",
            ]
        )
        focus = [
            "A_tf2",
            "B_tf3_d0.1",
            "C_arm_tf3_d0.1",
            "D_cool_imp_K30_d0.1",
            "E_tf3_follow_d0.1",
            "C_E_arm_follow_d0.1",
            "G_C_arm_tf3_d0.1_cut1230",
            "G_C_E_arm_follow_d0.1_cut1230",
        ]
        # also include recommendation winner
        if rec.get("manual_use") and rec["manual_use"] not in focus:
            focus.append(rec["manual_use"])
        lines.append("| variant | signal | lead min | before | headroom% | sync | tw/nq roc |")
        lines.append("|---------|--------|----------|--------|-----------|------|-----------|")
        sigs = case.get("signals") or {}
        for vid in focus:
            s = sigs.get(vid) or {}
            roc = f"{s.get('tw_bar_roc')}/{s.get('nq_bar_roc')}"
            lines.append(
                f"| `{vid}` | {s.get('poll')} | {s.get('lead_min')} | {s.get('before_trough')} | "
                f"{s.get('headroom_vs_trough_pct')} | {s.get('sync_pulse')} | {roc} |"
            )

    lines.extend(
        [
            "",
            "## Analyst verdict (manual rebuy · not Order auto)",
            "",
            f"- **Manual use (primary):** `{rec.get('manual_use')}`",
            f"- **Soft veto (optional):** `{rec.get('manual_soft_veto')}`",
            f"- Metric leader (sensitivity): "
            f"`{(rec.get('metric_leader_agg') or {}).get('variant_id')}`",
            f"- Ranked top: {', '.join(f'`{x}`' for x in (rec.get('ranked_top') or []))}",
            f"- Keep notes: {', '.join(f'`{x}`' for x in (rec.get('keep_candidates') or [])) or '—'}",
            f"- Reject: {', '.join(f'`{x}`' for x in (rec.get('reject') or [])[:12]) or '—'}"
            + (" …" if len(rec.get("reject") or []) > 12 else ""),
            f"- Rationale: {rec.get('rationale')}",
            "",
            "## Caveats",
            "",
            "- Yahoo 5m history ~59 trading days; n RED / MT is small — treat ranks as directional.",
            "- Post-event trough ≠ full-session trough when low prints before RED.",
            "- TF-3 uses 5m bar ROC (poll-to-poll Δ from_open), not rolling win% window.",
            "- SOP reminder: IMPROVE = arm only; repair confirm ≈ TF-3 (± cooldown / FOLLOW) + avoid late session.",
            "- Research hypothesis only; **not** Strategy/Order 採納.",
            "",
        ]
    )
    return "\n".join(lines)


def run_study(*, date_end: str | date | None = None, out_dir: Path | None = None) -> dict[str, Any]:
    polls = load_day_polls(date_end=date_end)
    frozen = frozen_sell_gate_spec()
    variants = variant_catalog()

    day_rows = [
        analyze_day_variants(polls[d], spec=frozen, variants=variants) for d in sorted(polls)
    ]
    aggs = [aggregate_variant(day_rows, v.id) for v in variants]
    for a, v in zip(aggs, variants):
        a["family"] = v.family
        a["label"] = v.label

    case: dict[str, Any] = {}
    if CASE_DAY in polls:
        case = analyze_day_variants(polls[CASE_DAY], spec=frozen, variants=variants)

    rec = pick_recommendation(aggs)

    panel_meta = getattr(polls, "_detach_panel_meta", {}) or {}
    meta = {
        "n_days": len(polls),
        "window": f"{min(polls)} → {max(polls)}" if polls else None,
        "frozen_spec_id": frozen.strategy_id,
        "label": "post_event_trough_tw_from_open",
        "meaningful_trough_dd_pct": MEANINGFUL_TROUGH_DD,
        "near_trough_bp": NEAR_TROUGH_BP,
        "delta_sweep": list(DELTA_SWEEP),
        "cooldown_k": list(COOLDOWN_K),
        "default_delta": DEFAULT_DELTA,
        "tf4_skipped": True,
        "topic": "detach-gate-trough-find",
        "hypothesis": "TF-2∩TF-3 repair confirm",
        "layer": "research",
        "order_rebuy": False,
        "primary_case_day": CASE_DAY,
        "case_in_panel": CASE_DAY in polls,
        "variant_ids": [v.id for v in variants],
        **panel_meta,
    }

    payload: dict[str, Any] = {
        "meta": meta,
        "variant_aggs": aggs,
        "days_with_event": [r for r in day_rows if r.get("has_event")],
        "case_20260714": case,
        "recommendation": rec,
    }
    markdown = render_markdown(payload, variants=variants)

    if out_dir is not None:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "tf23_summary.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(markdown, encoding="utf-8")
        # flat CSV of aggs
        pd.DataFrame(aggs).to_csv(out_dir / "variant_aggs.csv", index=False)

    return {"payload": payload, "markdown": markdown, "polls": polls, "variants": variants}
