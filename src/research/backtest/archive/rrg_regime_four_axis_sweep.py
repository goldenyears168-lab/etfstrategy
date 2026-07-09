"""RRG × Regime four-axis · Phase 1 gate sweep (lifecycle event filters)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from research.backtest.archive.rrg_regime_four_axis_annot import (
    IS_END,
    IS_START,
    OOS_START,
    _finite_floats,
    _mean,
    _win_rate,
    run_rrg_regime_four_axis_annot,
)

BURST_PCT = 5.0
MIN_CAPTURE_RATE = 0.25
OOS_MIN_N = 20
OOS_MAX_DELTA_EXCESS_PP = -0.2


@dataclass(frozen=True)
class RegimeGateSpec:
    gate_id: str
    label: str
    cohort: str
    predicate: Callable[[dict[str, Any]], bool]
    preregistered_hypothesis: str | None = None


def _cohort_filter(cohort: str) -> Callable[[dict[str, Any]], bool]:
    if cohort == "S0":
        return lambda e: bool(e.get("s0_fresh_flip"))
    if cohort == "S2":
        return lambda e: bool(e.get("s2_setup_core"))
    if cohort == "S3":
        return lambda e: bool(e.get("s3_burst"))
    if cohort == "IMPROVING":
        return lambda e: str(e.get("end_q") or "") == "improving"
    raise ValueError(f"unknown cohort {cohort}")


def _always(_: dict[str, Any]) -> bool:
    return True


def _bench_b0(e: dict[str, Any]) -> bool:
    return bool(e.get("bench_b0"))


def _bench_b1(e: dict[str, Any]) -> bool:
    v = e.get("bench_today_ret")
    return v is not None and v == v and float(v) >= 1.0


def _bench_b2(e: dict[str, Any]) -> bool:
    return e.get("bench_bucket") == "b2"


def _breadth_ns(e: dict[str, Any]) -> bool:
    return bool(e.get("breadth_neutral_strong"))


def _breadth_not_ob(e: dict[str, Any]) -> bool:
    return not bool(e.get("breadth_overbought"))


def _stage2_high(e: dict[str, Any]) -> bool:
    return e.get("stage2_bucket") == "high"


def _weinstein_2(e: dict[str, Any]) -> bool:
    return bool(e.get("weinstein_stage2"))


def _universe_imp40(e: dict[str, Any]) -> bool:
    return float(e.get("universe_improving_pct") or 0) >= 40.0


def _universe_rh50(e: dict[str, Any]) -> bool:
    return float(e.get("universe_rotation_health_pct") or 0) >= 50.0


def _c1(e: dict[str, Any]) -> bool:
    return _bench_b0(e) and _breadth_ns(e)


def _c2(e: dict[str, Any]) -> bool:
    return _bench_b0(e) and _stage2_high(e)


def _c3(e: dict[str, Any]) -> bool:
    return _bench_b0(e) and _weinstein_2(e)


def _c4(e: dict[str, Any]) -> bool:
    return _breadth_ns(e) and _stage2_high(e)


def phase1_gate_specs() -> list[RegimeGateSpec]:
    return [
        RegimeGateSpec("R0", "baseline · no gate", "S2", _always),
        RegimeGateSpec("G-B0", "signal-day bench≥0", "S2", _bench_b0, "H-RA-2"),
        RegimeGateSpec("G-B1", "signal-day bench≥1%", "S2", _bench_b1),
        RegimeGateSpec("G-B2", "signal-day bench≥2%", "S2", _bench_b2),
        RegimeGateSpec("G-BREADTH-NS", "T-1 breadth neutral/strong", "S2", _breadth_ns, "H-RB-1"),
        RegimeGateSpec("G-BREADTH-NOT-OB", "T-1 breadth not overbought", "S2", _breadth_not_ob),
        RegimeGateSpec("G-STAGE2-HIGH", "T-1 stage2 participation high", "S2", _stage2_high, "H-RC-1"),
        RegimeGateSpec("G-WEINSTEIN-2", "T-1 Weinstein stage 2", "S2", _weinstein_2),
        RegimeGateSpec("R0", "baseline · no gate", "S0", _always),
        RegimeGateSpec("G-B0", "signal-day bench≥0", "S0", _bench_b0),
        RegimeGateSpec("G-B2", "signal-day bench≥2%", "S0", _bench_b2),
        RegimeGateSpec("G-WEINSTEIN-2", "T-1 Weinstein stage 2", "S0", _weinstein_2, "H-RA-1"),
        RegimeGateSpec("G-UNIVERSE-IMP40", "T-1 universe improving≥40%", "S0", _universe_imp40, "H-RD-1"),
        RegimeGateSpec("G-UNIVERSE-RH50", "T-1 rotation health≥50%", "S0", _universe_rh50),
        RegimeGateSpec("R0", "baseline · no gate", "S3", _always),
        RegimeGateSpec("G-B0", "signal-day bench≥0 (not bench_nxt)", "S3", _bench_b0),
        RegimeGateSpec("G-B2", "signal-day bench≥2%", "S3", _bench_b2),
        RegimeGateSpec("C1", "S2 · bench≥0 ∧ breadth NS", "S2", _c1),
        RegimeGateSpec("C2", "S2 · bench≥0 ∧ stage2 high", "S2", _c2),
        RegimeGateSpec("C3", "S2 · bench≥0 ∧ Weinstein 2", "S2", _c3),
        RegimeGateSpec("C4", "S2 · breadth NS ∧ stage2 high", "S2", _c4),
    ]


def _apply_gate(
    events: list[dict[str, Any]],
    *,
    cohort: str,
    predicate: Callable[[dict[str, Any]], bool],
) -> list[dict[str, Any]]:
    cohort_fn = _cohort_filter(cohort)
    return [e for e in events if cohort_fn(e) and predicate(e)]


def _event_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    nxt = [e.get("nxt_ret") for e in events]
    ex = [e.get("excess_nxt") for e in events]
    burst_hits = sum(1 for v in _finite_floats(nxt) if v >= BURST_PCT)
    n = len(events)
    return {
        "n_events": n,
        "n_signal_days": len({e["date"] for e in events}),
        "mean_nxt_ret_pct": _mean(nxt),
        "mean_excess_nxt_pct": _mean(ex),
        "win_rate": _win_rate(nxt),
        "p_nxt_ge_burst": round(burst_hits / n, 4) if n else None,
    }


def _delta_pp(a: Any, b: Any) -> float | None:
    if a is None or b is None:
        return None
    return round(float(a) - float(b), 4)


def _pass_verdict(
    row: dict[str, Any],
    *,
    baseline_n: int,
) -> dict[str, Any]:
    oos = row.get("OOS") or {}
    full = row.get("FULL") or {}
    capture = row.get("capture_rate_full")
    oos_n = int(oos.get("n_events") or 0)
    delta_ex = oos.get("delta_excess_vs_r0_pp")
    reasons: list[str] = []
    passed = True
    if row.get("gate_id") == "R0":
        return {"passed": None, "reasons": ["baseline"]}
    if capture is not None and capture < MIN_CAPTURE_RATE:
        passed = False
        reasons.append(f"capture {capture} < {MIN_CAPTURE_RATE}")
    if oos_n < OOS_MIN_N:
        passed = False
        reasons.append(f"OOS n={oos_n} < {OOS_MIN_N}")
    if delta_ex is not None and float(delta_ex) < OOS_MAX_DELTA_EXCESS_PP:
        passed = False
        reasons.append(f"OOS Δexcess {delta_ex}pp < {OOS_MAX_DELTA_EXCESS_PP}pp")
    if int(full.get("n_events") or 0) < max(20, baseline_n // 10):
        passed = False
        reasons.append("FULL n too small")
    if passed:
        reasons.append("OOS Δexcess within band · capture ok")
    return {"passed": passed, "reasons": reasons}


def _sweep_one(
    events: list[dict[str, Any]],
    spec: RegimeGateSpec,
    *,
    splits: dict[str, tuple[str | None, str | None]],
) -> dict[str, Any]:
    r0_by_split: dict[str, dict[str, Any]] = {}
    gated_by_split: dict[str, dict[str, Any]] = {}
    for label, (start, end) in splits.items():
        if start is None and end is None:
            sub = events
        elif end is None:
            sub = [e for e in events if e["date"] >= start]  # type: ignore[operator]
        else:
            sub = [e for e in events if start <= e["date"] <= end]  # type: ignore[operator]
        r0 = _apply_gate(sub, cohort=spec.cohort, predicate=_always)
        gated = _apply_gate(sub, cohort=spec.cohort, predicate=spec.predicate)
        r0_by_split[label] = _event_metrics(r0)
        gated_by_split[label] = _event_metrics(gated)

    full_r0 = r0_by_split["FULL"]
    full_g = gated_by_split["FULL"]
    capture = round(full_g["n_events"] / full_r0["n_events"], 4) if full_r0["n_events"] else None

    row: dict[str, Any] = {
        "gate_id": spec.gate_id,
        "label": spec.label,
        "cohort": spec.cohort,
        "preregistered_hypothesis": spec.preregistered_hypothesis,
        "capture_rate_full": capture,
        "FULL": {
            **full_g,
            "delta_nxt_vs_r0_pp": _delta_pp(full_g.get("mean_nxt_ret_pct"), full_r0.get("mean_nxt_ret_pct")),
            "delta_excess_vs_r0_pp": _delta_pp(
                full_g.get("mean_excess_nxt_pct"), full_r0.get("mean_excess_nxt_pct")
            ),
        },
        "IS": {
            **gated_by_split["IS"],
            "delta_nxt_vs_r0_pp": _delta_pp(
                gated_by_split["IS"].get("mean_nxt_ret_pct"),
                r0_by_split["IS"].get("mean_nxt_ret_pct"),
            ),
            "delta_excess_vs_r0_pp": _delta_pp(
                gated_by_split["IS"].get("mean_excess_nxt_pct"),
                r0_by_split["IS"].get("mean_excess_nxt_pct"),
            ),
        },
        "OOS": {
            **gated_by_split["OOS"],
            "delta_nxt_vs_r0_pp": _delta_pp(
                gated_by_split["OOS"].get("mean_nxt_ret_pct"),
                r0_by_split["OOS"].get("mean_nxt_ret_pct"),
            ),
            "delta_excess_vs_r0_pp": _delta_pp(
                gated_by_split["OOS"].get("mean_excess_nxt_pct"),
                r0_by_split["OOS"].get("mean_excess_nxt_pct"),
            ),
        },
        "r0_n_full": full_r0["n_events"],
    }
    row["verdict"] = _pass_verdict(row, baseline_n=full_r0["n_events"])
    return row


def run_rrg_regime_four_axis_sweep(
    conn: sqlite3.Connection | None = None,
    *,
    events: list[dict[str, Any]] | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    gate_specs: list[RegimeGateSpec] | None = None,
) -> dict[str, Any]:
    if events is None:
        if conn is None:
            raise ValueError("conn or events required")
        payload = run_rrg_regime_four_axis_annot(conn, date_start=date_start, date_end=date_end)
        events = payload["events"]
        annot_meta = {
            "date_start": payload["date_start"],
            "date_end": payload["date_end"],
            "n_events": payload["n_events"],
        }
    else:
        annot_meta = {
            "date_start": date_start or (events[0]["date"] if events else None),
            "date_end": date_end or (events[-1]["date"] if events else None),
            "n_events": len(events),
        }

    end = str(annot_meta["date_end"])
    splits = {
        "FULL": (None, None),
        "IS": (IS_START, IS_END),
        "OOS": (OOS_START, None),
    }
    specs = gate_specs or phase1_gate_specs()
    rows = [_sweep_one(events, spec, splits=splits) for spec in specs]
    winners = [
        r
        for r in rows
        if r.get("gate_id") != "R0" and (r.get("verdict") or {}).get("passed") is True
    ]
    winners.sort(
        key=lambda r: (
            float((r.get("OOS") or {}).get("delta_excess_vs_r0_pp") or -999),
            float((r.get("FULL") or {}).get("mean_excess_nxt_pct") or -999),
        ),
        reverse=True,
    )
    return {
        "schema": "rrg_regime_four_axis_sweep-v1",
        "topic": "rrg-regime-four-axis-annot",
        "phase": "phase1_sweep",
        "parent_research": "rrg-improving-lifecycle",
        "annot_meta": annot_meta,
        "pass_rules": {
            "min_capture_rate": MIN_CAPTURE_RATE,
            "oos_min_n": OOS_MIN_N,
            "oos_max_delta_excess_pp": OOS_MAX_DELTA_EXCESS_PP,
        },
        "n_variants": len(rows),
        "n_passed": len(winners),
        "winners": [
            {
                "gate_id": w["gate_id"],
                "cohort": w["cohort"],
                "label": w["label"],
                "OOS_delta_excess_pp": (w.get("OOS") or {}).get("delta_excess_vs_r0_pp"),
                "capture_rate_full": w.get("capture_rate_full"),
            }
            for w in winners[:10]
        ],
        "variants": rows,
    }


def load_annot_events(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("events") or [])


def render_rrg_regime_four_axis_sweep_md(payload: dict[str, Any]) -> str:
    meta = payload.get("annot_meta") or {}
    rules = payload.get("pass_rules") or {}
    lines = [
        "# RRG × Regime four-axis · Phase 1 gate sweep",
        "",
        f"區間：**{meta.get('date_start')}** → **{meta.get('date_end')}** · "
        f"events **{meta.get('n_events')}** · variants **{payload.get('n_variants')}** · "
        f"passed **{payload.get('n_passed')}**",
        "",
        "## Pass 規則",
        "",
        f"- capture_rate ≥ **{rules.get('min_capture_rate')}**",
        f"- OOS n ≥ **{rules.get('oos_min_n')}**",
        f"- OOS Δexcess vs R0 ≥ **{rules.get('oos_max_delta_excess_pp')}** pp",
        "",
        "## Winners（OOS pass）",
        "",
    ]
    winners = payload.get("winners") or []
    if winners:
        lines += [
            "| gate | cohort | capture | OOS Δexcess |",
            "|------|--------|---------|-------------|",
        ]
        for w in winners:
            lines.append(
                f"| {w['gate_id']} | {w['cohort']} | {w.get('capture_rate_full')} | "
                f"{w.get('OOS_delta_excess_pp')} |"
            )
    else:
        lines.append("_本輪無 variant 通過 OOS pass 規則_")

    lines += [
        "",
        "## 全變體 · FULL",
        "",
        "| gate | cohort | n | mean_nxt | mean_excess | Δexcess vs R0 | capture | pass |",
        "|------|--------|---|----------|-------------|---------------|---------|------|",
    ]
    for row in payload.get("variants") or []:
        if row.get("gate_id") == "R0":
            continue
        full = row.get("FULL") or {}
        verdict = row.get("verdict") or {}
        lines.append(
            f"| {row['gate_id']} | {row['cohort']} | {full.get('n_events')} | "
            f"{full.get('mean_nxt_ret_pct')} | {full.get('mean_excess_nxt_pct')} | "
            f"{full.get('delta_excess_vs_r0_pp')} | {row.get('capture_rate_full')} | "
            f"{verdict.get('passed')} |"
        )

    lines += [
        "",
        "## Baseline R0",
        "",
        "| cohort | FULL n | mean_nxt | mean_excess | OOS mean_excess |",
        "|--------|--------|----------|-------------|-----------------|",
    ]
    for row in payload.get("variants") or []:
        if row.get("gate_id") != "R0":
            continue
        full = row.get("FULL") or {}
        oos = row.get("OOS") or {}
        lines.append(
            f"| {row['cohort']} | {full.get('n_events')} | {full.get('mean_nxt_ret_pct')} | "
            f"{full.get('mean_excess_nxt_pct')} | {oos.get('mean_excess_nxt_pct')} |"
        )

    lines += [
        "",
        "## 解讀備註",
        "",
        "- Phase 1 為 **lifecycle 事件 filter** · 非 slot 組合回測。",
        "- `bench_nxt_ret` **未**用於任何 gate（僅 Phase 0 H-S3-BENCH eval）。",
        "- Phase 2：將贏家 gate 接到 `rrg_mono` / pre-release scorer（若 OOS 穩定）。",
        "",
    ]
    return "\n".join(lines)
