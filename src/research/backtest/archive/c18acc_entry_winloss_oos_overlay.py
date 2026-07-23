"""C18acc S2 legs · entry win/loss overlay OOS holdout · post-hoc filter backtest."""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

from research.backtest.archive.c18acc_entry_winloss_profile import collect_c18acc_wma_legs
from research.backtest.c18acc_drs_extension_overlay_sweep import (
    IS_END_DEFAULT,
    OOS_START_DEFAULT,
)
from research.backtest.w3_rv_hl_winner_profile import _mean

OVERLAY_VARIANTS: list[tuple[str, str, Callable[[dict[str, Any]], bool]]] = [
    ("baseline", "baseline · 全進場", lambda r: True),
    ("avoid_mixed", "avoid spread_mixed", lambda r: str(r.get("spread_class") or "") != "mixed"),
    (
        "prefer_w5_weakening",
        "prefer w5_weakening",
        lambda r: str(r.get("w5_quadrant") or "") == "weakening",
    ),
]

WINDOWS = ("full", "is", "oos")


def _summarize_legs(legs: list[dict[str, Any]]) -> dict[str, Any]:
    if not legs:
        return {
            "n": 0,
            "mean_return_pct": None,
            "mean_excess_pct": None,
            "win_rate_pct": None,
        }
    rets = [float(l["return_pct"]) for l in legs]
    exc = [float(l["excess_pct"]) for l in legs if l.get("excess_pct") is not None]
    return {
        "n": len(legs),
        "mean_return_pct": _mean(rets),
        "mean_excess_pct": _mean(exc) if exc else None,
        "win_rate_pct": round(100.0 * sum(1 for r in rets if r > 0) / len(rets), 2),
    }


def _slice_legs(
    legs: list[dict[str, Any]],
    *,
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for leg in legs:
        entry = str(leg.get("entry_date") or "")
        if start and entry < start:
            continue
        if end and entry > end:
            continue
        out.append(leg)
    return out


def _window_slices(
    legs: list[dict[str, Any]],
    *,
    date_start: str,
    date_end: str,
    is_end: str,
    oos_start: str | None,
) -> dict[str, list[dict[str, Any]]]:
    return {
        "full": _slice_legs(legs, start=date_start, end=date_end),
        "is": _slice_legs(legs, start=date_start, end=is_end),
        "oos": _slice_legs(legs, start=oos_start, end=date_end) if oos_start else [],
    }


def _overlay_results(
    legs: list[dict[str, Any]],
    *,
    date_start: str,
    date_end: str,
    is_end: str,
    oos_start: str | None,
) -> list[dict[str, Any]]:
    slices = _window_slices(
        legs,
        date_start=date_start,
        date_end=date_end,
        is_end=is_end,
        oos_start=oos_start,
    )
    base_oos_ex = None
    rows: list[dict[str, Any]] = []
    for vid, label, pred in OVERLAY_VARIANTS:
        filtered = [l for l in legs if pred(l)]
        row: dict[str, Any] = {
            "variant_id": vid,
            "label": label,
            "pass_rate_pct": round(100.0 * len(filtered) / len(legs), 2) if legs else 0.0,
            "slices": {},
        }
        for win in WINDOWS:
            sub = [l for l in slices[win] if pred(l)]
            sm = _summarize_legs(sub)
            row["slices"][win] = sm
        if vid == "baseline":
            base_oos_ex = (row["slices"].get("oos") or {}).get("mean_excess_pct")
        rows.append(row)

    base_ex = base_oos_ex if base_oos_ex is not None else 0.0
    for row in rows:
        oos = (row["slices"].get("oos") or {}).get("mean_excess_pct")
        row["delta_oos_excess_vs_baseline_pp"] = (
            round(float(oos or 0) - float(base_ex), 4) if oos is not None else None
        )
    return rows


def _adoption_verdict(
    variants: list[dict[str, Any]],
    *,
    min_oos_n: int = 8,
    min_oos_lift_pp: float = 0.5,
) -> dict[str, Any]:
    baseline = next(v for v in variants if v["variant_id"] == "baseline")
    base_oos = baseline["slices"].get("oos") or {}
    base_is = baseline["slices"].get("is") or {}
    candidates: list[dict[str, Any]] = []
    for v in variants:
        if v["variant_id"] == "baseline":
            continue
        oos = v["slices"].get("oos") or {}
        is_ = v["slices"].get("is") or {}
        n_oos = int(oos.get("n") or 0)
        delta = v.get("delta_oos_excess_vs_baseline_pp")
        is_delta = None
        is_ex = is_.get("mean_excess_pct")
        base_is_ex = base_is.get("mean_excess_pct")
        if is_ex is not None and base_is_ex is not None:
            is_delta = round(float(is_ex) - float(base_is_ex), 4)
        adopt = bool(
            n_oos >= min_oos_n
            and delta is not None
            and float(delta) >= min_oos_lift_pp
            and (is_delta is None or float(is_delta) >= -0.3)
        )
        candidates.append(
            {
                "variant_id": v["variant_id"],
                "label": v["label"],
                "oos_n": n_oos,
                "oos_mean_excess_pct": oos.get("mean_excess_pct"),
                "delta_oos_excess_pp": delta,
                "is_delta_excess_pp": is_delta,
                "recommend_adopt": adopt,
            }
        )

    adopted = [c for c in candidates if c["recommend_adopt"]]
    if adopted:
        best = max(adopted, key=lambda c: float(c.get("delta_oos_excess_pp") or 0))
        summary = (
            f"OOS adopt: {best['variant_id']} Δexcess={best['delta_oos_excess_pp']}pp "
            f"n={best['oos_n']}"
        )
    else:
        best = max(candidates, key=lambda c: float(c.get("delta_oos_excess_pp") or -999))
        summary = (
            f"no overlay passes OOS gate (need n≥{min_oos_n}, Δexcess≥{min_oos_lift_pp}pp); "
            f"best={best['variant_id']} Δ={best.get('delta_oos_excess_pp')}pp n={best.get('oos_n')}"
        )
    return {
        "min_oos_n": min_oos_n,
        "min_oos_lift_pp": min_oos_lift_pp,
        "baseline_oos": base_oos,
        "candidates": candidates,
        "recommend_adopt_any": bool(adopted),
        "best_candidate": best,
        "summary": summary,
    }


def run_c18acc_entry_winloss_oos_overlay(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    n_slots: int = 3,
    is_end: str = IS_END_DEFAULT,
    oos_start: str = OOS_START_DEFAULT,
) -> dict[str, Any]:
    """Collect C18acc S2 legs · apply entry overlays · IS/OOS holdout."""
    legs, meta = collect_c18acc_wma_legs(
        conn,
        date_start=date_start,
        date_end=date_end,
        n_slots=n_slots,
    )
    end = meta["date_end"]
    trade_oos_start = next((str(l["entry_date"]) for l in legs if str(l["entry_date"]) >= oos_start), None)
    effective_oos_start = trade_oos_start if trade_oos_start else None

    variants = _overlay_results(
        legs,
        date_start=meta["date_start"],
        date_end=end,
        is_end=is_end,
        oos_start=effective_oos_start,
    )
    verdict = _adoption_verdict(variants)
    return {
        "schema": "c18acc_entry_winloss_oos_overlay-v1",
        "topic": "c18acc-entry-winloss-oos-overlay",
        "date_start": meta["date_start"],
        "date_end": end,
        "is_end": is_end,
        "oos_start": effective_oos_start,
        "n_slots": n_slots,
        "n_legs": len(legs),
        "sim_summary": meta.get("sim_summary"),
        "trajectory_meta": meta.get("trajectory_meta"),
        "source_study": "c18acc_entry_winloss_profile",
        "overlays": [
            {"variant_id": vid, "label": label}
            for vid, label, _ in OVERLAY_VARIANTS
        ],
        "variants": variants,
        "verdict": verdict,
        "note": (
            "Post-hoc leg subset · not slot re-sim. "
            "Filters would skip entries; portfolio path not re-run."
        ),
    }


def render_c18acc_entry_winloss_oos_overlay_md(payload: dict[str, Any]) -> str:
    lines = [
        "# C18acc · entry win/loss overlay · OOS holdout",
        "",
        f"窗口：**{payload['date_start']}** .. **{payload['date_end']}** · "
        f"{payload.get('n_slots')} 槽 · **{payload.get('n_legs')}** executed legs",
        "",
        f"IS ≤ **{payload.get('is_end')}** · OOS ≥ **{payload.get('oos_start') or '—'}**",
        "",
        "來源：全母體 win/loss profile · 30m poll triple WMA · C18acc S2 sim",
        "",
        payload.get("note", ""),
        "",
        "## 變體 × 窗口",
        "",
        "| variant | window | n | mean_ret% | mean_excess% | win_rate% | Δ OOS excess |",
        "|---------|--------|--:|----------:|-------------:|----------:|-------------:|",
    ]
    for v in payload.get("variants") or []:
        delta = v.get("delta_oos_excess_vs_baseline_pp")
        delta_s = f"{delta}pp" if v["variant_id"] != "baseline" else "—"
        for win in WINDOWS:
            sm = (v.get("slices") or {}).get(win) or {}
            lines.append(
                f"| {v.get('label')} | {win.upper()} | {sm.get('n')} | "
                f"{sm.get('mean_return_pct')} | {sm.get('mean_excess_pct')} | "
                f"{sm.get('win_rate_pct')} | {delta_s} |"
            )

    v = payload.get("verdict") or {}
    lines.extend(
        [
            "",
            "## 採納建議",
            "",
            f"- Gate：OOS n≥{v.get('min_oos_n')} · Δexcess≥{v.get('min_oos_lift_pp')}pp · IS 不逆勢",
            f"- **建議採納任一 overlay**：{'是' if v.get('recommend_adopt_any') else '否'}",
            f"- {v.get('summary', '')}",
            "",
            "| overlay | OOS n | OOS excess% | Δ OOS | IS Δ | adopt |",
            "|---------|------:|-------------:|------:|-----:|:-----:|",
        ]
    )
    for c in v.get("candidates") or []:
        lines.append(
            f"| {c.get('label')} | {c.get('oos_n')} | {c.get('oos_mean_excess_pct')} | "
            f"{c.get('delta_oos_excess_pp')}pp | {c.get('is_delta_excess_pp')}pp | "
            f"{'✓' if c.get('recommend_adopt') else '—'} |"
        )

    lines.extend(
        [
            "",
            "模組：`src/research/backtest/archive/c18acc_entry_winloss_oos_overlay.py`",
        ]
    )
    return "\n".join(lines)
