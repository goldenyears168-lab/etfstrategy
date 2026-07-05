"""C18acc+S2 · Phase W-stack · earliest-exit overlay variants (leg-level).

Stacks dual-WMA intraday triggers with S2 baseline exits.
Leg-level counterfactual · does not re-simulate slot recycling.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research.backtest.c18acc_s2_dual_wma_annot import S2_MIN_HOLD_DAYS, load_c18acc_s2_periods
from research.backtest.c18acc_s2_dual_wma_exit_save import (
    _cohort_label,
    _period_key,
    compute_w5_counterfactual,
    load_annot_payload,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_intraday_exit import _trading_days_between


PROFIT_RULES = frozenset({"exit_spread_aligned", "exit_w5_extension", "exit_w20_leading"})
ROTATION_RULES = frozenset({"w5_deep_pullback", "exit_w20_weakening", "w5_lagging"})


@dataclass(frozen=True)
class StackSpec:
    variant_id: str
    label: str
    allowed_rules: frozenset[str]
    loss_gate_pct: float | None = None
    profit_gate_pct: float | None = None
    conservative_excess: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "label": self.label,
            "allowed_rules": sorted(self.allowed_rules),
            "loss_gate_pct": self.loss_gate_pct,
            "profit_gate_pct": self.profit_gate_pct,
            "conservative_excess": self.conservative_excess,
        }


STACK_SPECS: tuple[StackSpec, ...] = (
    StackSpec("B0", "C18acc+S2 實際出場", frozenset()),
    StackSpec(
        "W-S-ROT-L5",
        "rotation · deep_pullback|w20_weak · 虧損≥5%",
        ROTATION_RULES,
        loss_gate_pct=-5.0,
    ),
    StackSpec(
        "W-S-ROT-STRICT",
        "rotation · deep_pullback|w20_weak only · 虧損≥5%",
        frozenset({"w5_deep_pullback", "exit_w20_weakening"}),
        loss_gate_pct=-5.0,
    ),
    StackSpec(
        "W-S-PROFIT",
        "獲利 · spread_aligned|extension · 浮盈>0%",
        PROFIT_RULES,
        profit_gate_pct=0.0,
    ),
    StackSpec(
        "W-S-FULL",
        "earliest · 獲利>0 OR rotation≤−5%",
        PROFIT_RULES | ROTATION_RULES,
        loss_gate_pct=-5.0,
        profit_gate_pct=0.0,
    ),
    StackSpec(
        "W-S-FULL-CONS",
        "W-S-FULL · 若 excess Δ<0 則保留實際出場",
        PROFIT_RULES | ROTATION_RULES,
        loss_gate_pct=-5.0,
        profit_gate_pct=0.0,
        conservative_excess=True,
    ),
)


def _min_hold_date(full_dates: list[str], entry: str, min_hold: int = S2_MIN_HOLD_DAYS) -> str:
    if entry not in full_dates:
        return entry
    ei = full_dates.index(entry)
    return full_dates[min(ei + min_hold, len(full_dates) - 1)]


def _mtm_pct(entry_px: float, px: float) -> float:
    return (px / entry_px - 1.0) * 100.0


def _trigger_sort_key(t: dict[str, Any]) -> tuple[str, str]:
    return (str(t["trade_date"]), str(t.get("minute") or "99:99"))


def pick_stack_trigger(
    conn: sqlite3.Connection,
    period: dict[str, Any],
    leg: dict[str, Any],
    spec: StackSpec,
    *,
    close=None,
) -> dict[str, Any] | None:
    if spec.variant_id == "B0":
        return None

    if close is None:
        close, _, _ = load_price_panels(conn)

    from research.backtest.c18acc_s2_dual_wma_exit_save import _entry_px, _w5_exit_px

    entry_px = _entry_px(conn, period, close)
    if entry_px is None:
        return None

    entry = str(period["entry_date"])
    exit_d = str(period["exit_date"])
    full_dates = close.index.astype(str).tolist()
    min_date = _min_hold_date(full_dates, entry)

    candidates: list[dict[str, Any]] = []
    for trig in leg.get("w5_triggers") or []:
        rid = str(trig.get("rule_id") or "")
        if rid not in spec.allowed_rules:
            continue
        td = str(trig["trade_date"])
        if td < min_date or td > exit_d:
            continue
        minute = str(trig.get("minute") or "13:30")
        px = _w5_exit_px(
            conn,
            stock_id=str(period["stock_id"]),
            trade_date=td,
            minute=minute,
            close=close,
        )
        if px is None:
            continue
        mtm = _mtm_pct(entry_px, px)
        if spec.profit_gate_pct is not None and rid in PROFIT_RULES:
            if mtm <= spec.profit_gate_pct:
                continue
        if spec.loss_gate_pct is not None and rid in ROTATION_RULES:
            if mtm > spec.loss_gate_pct:
                continue
        candidates.append({**trig, "mtm_pct": round(mtm, 4)})

    if not candidates:
        return None
    return min(candidates, key=_trigger_sort_key)


def _leg_stack_outcome(
    conn: sqlite3.Connection,
    period: dict[str, Any],
    leg: dict[str, Any],
    spec: StackSpec,
    *,
    close=None,
) -> dict[str, Any]:
    if close is None:
        close, _, _ = load_price_panels(conn)

    if spec.variant_id == "B0":
        bench_ex = period.get("excess_pct")
        ret = period.get("return_pct")
        return {
            "variant_id": spec.variant_id,
            "used_stack": False,
            "stack_rule": None,
            "exit_date": str(period["exit_date"]),
            "return_pct": ret,
            "excess_pct": bench_ex,
            "save_vs_orig_pct": 0.0,
            "excess_delta_pp": 0.0,
            "early_exit": False,
        }

    orig_ret = float(period.get("return_pct") or 0)
    orig_excess = period.get("excess_pct")
    orig_exit = str(period["exit_date"])
    trig = pick_stack_trigger(conn, period, leg, spec, close=close)
    if not trig:
        return {
            "variant_id": spec.variant_id,
            "used_stack": False,
            "stack_rule": None,
            "exit_date": orig_exit,
            "return_pct": orig_ret,
            "excess_pct": orig_excess,
            "save_vs_orig_pct": 0.0,
            "excess_delta_pp": 0.0,
            "early_exit": False,
        }

    stack_cf = compute_w5_counterfactual(conn, period, trig, close=close)
    if not stack_cf:
        return {
            "variant_id": spec.variant_id,
            "used_stack": False,
            "error": "stack_cf_failed",
        }

    save = round(orig_ret - float(stack_cf["w5_return_pct"]), 4)
    excess_delta = (
        round(float(stack_cf["w5_excess_pct"]) - float(orig_excess), 4)
        if stack_cf.get("w5_excess_pct") is not None and orig_excess is not None
        else None
    )
    early = stack_cf["w5_exit_date"] < orig_exit or (
        stack_cf["w5_exit_date"] == orig_exit
        and (stack_cf.get("w5_exit_minute") or "13:30") < "13:30"
    )

    if spec.conservative_excess and early and excess_delta is not None and excess_delta < 0:
        return {
            "variant_id": spec.variant_id,
            "used_stack": False,
            "stack_rule": trig.get("rule_id"),
            "rejected_conservative": True,
            "exit_date": orig_exit,
            "return_pct": orig_ret,
            "excess_pct": orig_excess,
            "save_vs_orig_pct": 0.0,
            "excess_delta_pp": 0.0,
            "early_exit": False,
        }

    if not early:
        return {
            "variant_id": spec.variant_id,
            "used_stack": False,
            "stack_rule": trig.get("rule_id"),
            "not_earlier_than_actual": True,
            "exit_date": orig_exit,
            "return_pct": orig_ret,
            "excess_pct": orig_excess,
            "save_vs_orig_pct": 0.0,
            "excess_delta_pp": 0.0,
            "early_exit": False,
        }

    return {
        "variant_id": spec.variant_id,
        "used_stack": True,
        "stack_rule": trig.get("rule_id"),
        "stack_mtm_pct": trig.get("mtm_pct"),
        "exit_date": stack_cf["w5_exit_date"],
        "exit_minute": stack_cf["w5_exit_minute"],
        "return_pct": stack_cf["w5_return_pct"],
        "excess_pct": stack_cf["w5_excess_pct"],
        "save_vs_orig_pct": save,
        "excess_delta_pp": excess_delta,
        "hold_days_saved": stack_cf["hold_days_saved"],
        "early_exit": True,
    }


def _summarize_variant(rows: list[dict[str, Any]], *, baseline_excess: float | None) -> dict[str, Any]:
    valid = [r for r in rows if r.get("return_pct") is not None and r.get("excess_pct") is not None]
    if not valid:
        return {"n": 0}
    rets = [float(r["return_pct"]) for r in valid]
    excess = [float(r["excess_pct"]) for r in valid]
    mean_excess = round(statistics.mean(excess), 4)
    out = {
        "n": len(valid),
        "mean_return_pct": round(statistics.mean(rets), 4),
        "mean_excess_pct": mean_excess,
        "median_excess_pct": round(statistics.median(excess), 4),
        "n_early_exit": sum(1 for r in valid if r.get("early_exit")),
        "n_used_stack": sum(1 for r in valid if r.get("used_stack")),
        "mean_save_vs_orig_pct": round(
            statistics.mean(float(r.get("save_vs_orig_pct") or 0) for r in valid), 4
        ),
        "pct_save_positive": round(
            sum(1 for r in valid if (r.get("save_vs_orig_pct") or 0) > 0) / len(valid) * 100, 2
        ),
    }
    if baseline_excess is not None:
        out["delta_mean_excess_pp"] = round(mean_excess - baseline_excess, 4)
    return out


def run_w_stack_analysis(
    conn: sqlite3.Connection,
    annot: dict[str, Any],
    *,
    periods: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if periods is None:
        periods, s2_summary = load_c18acc_s2_periods(
            conn,
            date_start=annot["date_start"],
            date_end=annot["date_end"],
            n_slots=int(annot.get("n_slots") or 3),
        )
    else:
        s2_summary = annot.get("s2_summary") or {}

    period_by_key = {_period_key(p): p for p in periods}
    close, _, _ = load_price_panels(conn)

    leg_base: list[dict[str, Any]] = []
    by_variant: dict[str, list[dict[str, Any]]] = {s.variant_id: [] for s in STACK_SPECS}
    by_variant_cohort: dict[str, dict[str, list[dict[str, Any]]]] = {
        s.variant_id: {"w5_only": [], "all": []} for s in STACK_SPECS
    }

    for leg in annot.get("legs") or []:
        pk = leg.get("leg_id") or f"{leg['entry_date']}|{leg['stock_id']}|0"
        period = period_by_key.get(pk)
        if not period:
            period = next(
                (
                    p
                    for p in periods
                    if str(p["entry_date"]) == leg["entry_date"]
                    and str(p["stock_id"]) == leg["stock_id"]
                ),
                None,
            )
        if not period:
            continue

        cohort = _cohort_label(leg)
        leg_row = {
            "leg_id": pk,
            "stock_id": leg["stock_id"],
            "cohort": cohort,
            "s2_force_exit": leg.get("s2_force_exit"),
            "variants": {},
        }
        for spec in STACK_SPECS:
            outcome = _leg_stack_outcome(conn, period, leg, spec, close=close)
            leg_row["variants"][spec.variant_id] = outcome
            by_variant[spec.variant_id].append(outcome)
            by_variant_cohort[spec.variant_id]["all"].append(outcome)
            if cohort == "w5_only":
                by_variant_cohort[spec.variant_id]["w5_only"].append(outcome)

        leg_base.append(leg_row)

    b0_excess = _summarize_variant(by_variant["B0"], baseline_excess=None).get("mean_excess_pct")
    summaries: dict[str, Any] = {}
    for spec in STACK_SPECS:
        summaries[spec.variant_id] = {
            "spec": spec.to_dict(),
            "all_legs": _summarize_variant(by_variant[spec.variant_id], baseline_excess=b0_excess),
            "w5_only": _summarize_variant(
                by_variant_cohort[spec.variant_id]["w5_only"], baseline_excess=b0_excess
            ),
        }

    best = max(
        (s for s in STACK_SPECS if s.variant_id != "B0"),
        key=lambda s: summaries[s.variant_id]["all_legs"].get("delta_mean_excess_pp") or -999,
    )

    hypotheses = {
        "H-W-S1": {
            "statement": "W-S-FULL Δ mean_excess vs B0 ≥ −0.2pp (all legs)",
            "pass": (summaries["W-S-FULL"]["all_legs"].get("delta_mean_excess_pp") or -999) >= -0.2,
            "value": summaries["W-S-FULL"]["all_legs"].get("delta_mean_excess_pp"),
        },
        "H-W-S2": {
            "statement": "W-S-ROT-STRICT w5_only Δ mean_excess ≥ 0",
            "pass": (summaries["W-S-ROT-STRICT"]["w5_only"].get("delta_mean_excess_pp") or -999) >= 0,
            "value": summaries["W-S-ROT-STRICT"]["w5_only"].get("delta_mean_excess_pp"),
        },
        "H-W-S3": {
            "statement": "W-S-FULL-CONS Δ mean_excess ≥ W-S-FULL (conservative not worse)",
            "pass": (
                summaries["W-S-FULL-CONS"]["all_legs"].get("delta_mean_excess_pp") or -999
            )
            >= (summaries["W-S-FULL"]["all_legs"].get("delta_mean_excess_pp") or -999),
            "value": summaries["W-S-FULL-CONS"]["all_legs"].get("delta_mean_excess_pp"),
        },
        "H-W-S4": {
            "statement": "W-S-PROFIT does not beat W-S-ROT-STRICT on w5_only excess",
            "pass": (
                summaries["W-S-PROFIT"]["w5_only"].get("mean_excess_pct") or -999
            )
            <= (summaries["W-S-ROT-STRICT"]["w5_only"].get("mean_excess_pct") or 999),
            "value": summaries["W-S-PROFIT"]["w5_only"].get("mean_excess_pct"),
        },
    }

    return {
        "schema": "c18acc_s2_dual_wma_stack-v1",
        "phase": "W-stack",
        "date_start": annot["date_start"],
        "date_end": annot["date_end"],
        "n_slots": annot.get("n_slots"),
        "s2_summary": s2_summary,
        "n_legs": len(leg_base),
        "stack_specs": [s.to_dict() for s in STACK_SPECS],
        "summaries": summaries,
        "best_variant_by_excess": best.variant_id,
        "hypotheses": hypotheses,
        "legs": leg_base,
        "notes": (
            "Leg-level earliest-exit · 未重模擬槽位 · "
            "W-S-FULL = min(獲利 spread/extension 且 mtm>0, rotation 且 mtm≤−5%, 實際出場)"
        ),
    }


def render_w_stack_md(payload: dict[str, Any]) -> str:
    lines = [
        "# C18acc+S2 · Phase W-stack（earliest-exit 疊加）",
        "",
        f"區間：**{payload['date_start']}** .. **{payload['date_end']}** · "
        f"{payload['n_slots']} 槽 · **{payload['n_legs']}** legs",
        "",
        payload.get("notes", ""),
        "",
        "## 變體摘要（全樣本）",
        "",
        "| variant | n_early | mean_excess% | Δ vs B0 | mean save% | save>0% |",
        "|---------|---------|--------------|---------|------------|---------|",
    ]
    b0_ex = (payload["summaries"].get("B0") or {}).get("all_legs", {}).get("mean_excess_pct")
    for spec in STACK_SPECS:
        sm = (payload["summaries"].get(spec.variant_id) or {}).get("all_legs") or {}
        if not sm.get("n"):
            continue
        delta = sm.get("delta_mean_excess_pp", "—")
        if spec.variant_id == "B0":
            delta = "—"
        lines.append(
            f"| {spec.variant_id} | {sm.get('n_early_exit', 0)} "
            f"| {sm.get('mean_excess_pct')} | {delta} "
            f"| {sm.get('mean_save_vs_orig_pct')} | {sm.get('pct_save_positive')} |"
        )

    lines += [
        "",
        f"**B0 基線 mean_excess** = {b0_ex}% · "
        f"最佳變體（Δexcess）= **{payload.get('best_variant_by_excess')}**",
        "",
        "## w5_only 子樣本",
        "",
        "| variant | n_early | mean_excess% | Δ vs B0 |",
        "|---------|---------|--------------|---------|",
    ]
    for spec in STACK_SPECS:
        sm = (payload["summaries"].get(spec.variant_id) or {}).get("w5_only") or {}
        if not sm.get("n"):
            continue
        lines.append(
            f"| {spec.variant_id} | {sm.get('n_early_exit', 0)} "
            f"| {sm.get('mean_excess_pct')} | {sm.get('delta_mean_excess_pp', '—')} |"
        )

    lines += ["", "## 變體定義", ""]
    for spec in STACK_SPECS:
        if spec.variant_id == "B0":
            continue
        lines.append(f"- **{spec.variant_id}**：{spec.label}")
        d = spec.to_dict()
        if d.get("loss_gate_pct") is not None:
            lines.append(f"  - loss gate: mtm ≤ {d['loss_gate_pct']}%")
        if d.get("profit_gate_pct") is not None:
            lines.append(f"  - profit gate: mtm > {d['profit_gate_pct']}%")
        if d.get("conservative_excess"):
            lines.append("  - conservative: excess Δ<0 時保留實際出場")

    lines += ["", "## 假說", ""]
    for hid, h in (payload.get("hypotheses") or {}).items():
        mark = "✓" if h.get("pass") else "✗"
        lines.append(f"- {mark} **{hid}**：{h.get('statement')} · value={h.get('value')}")

    lines += [
        "",
        "---",
        "模組：`scripts/run_c18acc_s2_dual_wma_stack.py`",
        "",
    ]
    return "\n".join(lines)
