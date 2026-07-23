"""ABC v3+F1 · lead_firm family scan (residual clue after PB-MORPH-1).

Variants around W20 path-into-entry firmness:
  - lead_firm / lead_rise
  - relative cushion: d_w20 > d_w5 (and vs W3 / both)
  - light combos with px_dip / not-deepening

Also reports Pearson(d_w20, ret), terciles, and 3 time segments for stability.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Callable

from research.backtest.abc_v3_f1_pullback_morphology_study import (
    annotate_legs_pullback_morphology,
)

SCHEMA = "abc_v3_f1_lead_firm_family-v1"

VARIANT_SPECS: tuple[tuple[str, str, Callable[[dict[str, Any]], bool]], ...] = (
    ("lead_firm", "W20 MV 不降（d_w20≥0）", lambda r: bool(r.get("lead_firm"))),
    ("lead_rise", "W20 MV 上升（d_w20>0）", lambda r: bool(r.get("lead_rise"))),
    ("w20_gt_w5", "相對：d_w20 > d_w5", lambda r: bool(r.get("w20_gt_w5"))),
    ("w20_ge_w5", "相對：d_w20 ≥ d_w5", lambda r: bool(r.get("w20_ge_w5"))),
    ("w20_gt_w3", "相對：d_w20 > d_w3", lambda r: bool(r.get("w20_gt_w3"))),
    ("w20_gt_both", "相對：d_w20 > d_w5 且 > d_w3", lambda r: bool(r.get("w20_gt_both"))),
    ("lead_firm_px", "lead_firm ∧ 價<開盤", lambda r: bool(r.get("lead_firm_px"))),
    (
        "lead_firm_not_deep",
        "lead_firm ∧ 未加深",
        lambda r: bool(r.get("lead_firm_not_deep")),
    ),
    (
        "firm_and_cushion",
        "lead_firm ∧ d_w20>d_w5",
        lambda r: bool(r.get("lead_firm")) and bool(r.get("w20_gt_w5")),
    ),
)

SEGMENTS: tuple[tuple[str, str, str], ...] = (
    ("seg1", "2024-01-02", "2024-10-31"),
    ("seg2", "2024-11-01", "2025-08-31"),
    ("seg3", "2025-09-01", "2026-07-02"),
)


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 3) if xs else None


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return round(s[mid], 3)
    return round((s[mid - 1] + s[mid]) / 2.0, 3)


def _win_rate(xs: list[float]) -> float | None:
    if not xs:
        return None
    return round(100.0 * sum(1 for x in xs if x > 0) / len(xs), 1)


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx < 1e-12 or vy < 1e-12:
        return None
    return round(cov / (vx * vy) ** 0.5, 4)


def _summarize(rets: list[float], *, baseline: float | None) -> dict[str, Any]:
    mean = _mean(rets)
    return {
        "n": len(rets),
        "mean_pct": mean,
        "median_pct": _median(rets),
        "win_rate_pct": _win_rate(rets),
        "delta_vs_baseline_pp": (
            round(mean - baseline, 3) if mean is not None and baseline is not None else None
        ),
    }


def _variant_row(
    rows: list[dict[str, Any]],
    *,
    vid: str,
    label: str,
    pred: Callable[[dict[str, Any]], bool],
    baseline: float | None,
    min_n: int,
    lift_pp: float,
) -> dict[str, Any]:
    sub = [float(r["tp_only_ret_pct"]) for r in rows if pred(r)]
    comp = [float(r["tp_only_ret_pct"]) for r in rows if not pred(r)]
    mean = _mean(sub)
    return {
        "id": vid,
        "label": label,
        **_summarize(sub, baseline=baseline),
        "complement_n": len(comp),
        "complement_mean_pct": _mean(comp),
        "passes_gate": bool(
            len(sub) >= min_n
            and mean is not None
            and baseline is not None
            and (mean - baseline) >= lift_pp
        ),
    }


def _terciles_d_w20(rows: list[dict[str, Any]], *, baseline: float | None) -> list[dict[str, Any]]:
    usable = [r for r in rows if r.get("d_w20_mv") is not None]
    if len(usable) < 9:
        return []
    ordered = sorted(usable, key=lambda r: float(r["d_w20_mv"]))
    n = len(ordered)
    t = n // 3
    bands = (
        ("bot", ordered[:t]),
        ("mid", ordered[t : n - t]),
        ("top", ordered[n - t :]),
    )
    out: list[dict[str, Any]] = []
    for bid, chunk in bands:
        rets = [float(r["tp_only_ret_pct"]) for r in chunk]
        dvals = [float(r["d_w20_mv"]) for r in chunk]
        out.append(
            {
                "id": bid,
                "d_w20_min": round(min(dvals), 4),
                "d_w20_max": round(max(dvals), 4),
                **_summarize(rets, baseline=baseline),
            }
        )
    return out


def run_lead_firm_family_study(
    conn: sqlite3.Connection,
    legs: list[dict[str, Any]],
    *,
    legs_source: str = "",
    min_bucket_n: int = 30,
    lift_pp_gate: float = 2.0,
    annotated: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rows = annotated if annotated is not None else annotate_legs_pullback_morphology(conn, legs)
    rets = [float(r["tp_only_ret_pct"]) for r in rows]
    baseline = _mean(rets)

    variants = [
        _variant_row(
            rows,
            vid=vid,
            label=label,
            pred=pred,
            baseline=baseline,
            min_n=min_bucket_n,
            lift_pp=lift_pp_gate,
        )
        for vid, label, pred in VARIANT_SPECS
    ]

    # Continuous: d_w20 and relative cushion score.
    pairs_w20 = [
        (float(r["d_w20_mv"]), float(r["tp_only_ret_pct"]))
        for r in rows
        if r.get("d_w20_mv") is not None
    ]
    pairs_rel = [
        (float(r["d_w20_mv"]) - float(r["d_w5_mv"]), float(r["tp_only_ret_pct"]))
        for r in rows
        if r.get("d_w20_mv") is not None and r.get("d_w5_mv") is not None
    ]

    segment_blocks: list[dict[str, Any]] = []
    for seg_id, d0, d1 in SEGMENTS:
        sub = [r for r in rows if d0 <= str(r.get("entry_date")) <= d1]
        seg_base = _mean([float(r["tp_only_ret_pct"]) for r in sub])
        focus = (
            ("lead_firm", "W20 MV 不降", lambda r: bool(r.get("lead_firm"))),
            ("w20_gt_w5", "d_w20 > d_w5", lambda r: bool(r.get("w20_gt_w5"))),
            ("w20_gt_both", "d_w20 > both", lambda r: bool(r.get("w20_gt_both"))),
        )
        segment_blocks.append(
            {
                "id": seg_id,
                "date_start": d0,
                "date_end": d1,
                "n": len(sub),
                "baseline_mean_tp_pct": seg_base,
                "variants": [
                    _variant_row(
                        sub,
                        vid=vid,
                        label=label,
                        pred=pred,
                        baseline=seg_base,
                        min_n=10,
                        lift_pp=lift_pp_gate,
                    )
                    for vid, label, pred in focus
                ],
            }
        )

    # Stability: positive Δ in ≥2/3 segments with n≥10.
    def _seg_positive(vid: str) -> dict[str, Any]:
        hits = 0
        detail: list[dict[str, Any]] = []
        for block in segment_blocks:
            row = next((v for v in block["variants"] if v["id"] == vid), None)
            if not row:
                continue
            ok = (
                int(row.get("n") or 0) >= 10
                and row.get("delta_vs_baseline_pp") is not None
                and float(row["delta_vs_baseline_pp"]) > 0
            )
            if ok:
                hits += 1
            detail.append(
                {
                    "seg": block["id"],
                    "n": row.get("n"),
                    "delta_pp": row.get("delta_vs_baseline_pp"),
                    "positive": ok,
                }
            )
        return {"variant_id": vid, "positive_segments": hits, "segments": detail, "stable": hits >= 2}

    stability = [_seg_positive(vid) for vid in ("lead_firm", "w20_gt_w5", "w20_gt_both")]
    best = max(
        variants,
        key=lambda v: (
            v.get("delta_vs_baseline_pp") is not None,
            v.get("delta_vs_baseline_pp") or -999,
            int(v.get("n") or 0),
        ),
        default=None,
    )
    dates = sorted({str(r["entry_date"]) for r in rows})
    any_pass = any(bool(v.get("passes_gate")) for v in variants)
    any_stable = any(bool(s.get("stable")) for s in stability)

    return {
        "schema": SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "legs_source": legs_source,
        "n_input_legs": len(legs),
        "n_annotated": len(rows),
        "n_stocks": len({str(r["stock_id"]) for r in rows}),
        "date_start": dates[0] if dates else None,
        "date_end": dates[-1] if dates else None,
        "exit": "champion TP-only · NEVER_SL · hold_5d cap",
        "baseline_mean_tp_pct": baseline,
        "min_bucket_n": min_bucket_n,
        "lift_pp_gate": lift_pp_gate,
        "corr_d_w20_vs_ret": _pearson([a for a, _ in pairs_w20], [b for _, b in pairs_w20]),
        "corr_rel_cushion_vs_ret": _pearson([a for a, _ in pairs_rel], [b for _, b in pairs_rel]),
        "d_w20_terciles": _terciles_d_w20(rows, baseline=baseline),
        "variants": variants,
        "segments": segment_blocks,
        "stability": stability,
        "best_variant": best,
        "any_variant_passes_gate": any_pass,
        "any_variant_stable_2of3": any_stable,
        "verdict": (
            "passed_candidate"
            if any_pass and any_stable
            else "rejected"
            if not any_pass
            else "mixed"
        ),
        # Compact feature rows for reuse (no heavy timelines).
        "feature_rows": [
            {
                "stock_id": r.get("stock_id"),
                "entry_date": r.get("entry_date"),
                "tp_only_ret_pct": r.get("tp_only_ret_pct"),
                "d_w20_mv": r.get("d_w20_mv"),
                "d_w5_mv": r.get("d_w5_mv"),
                "d_w3_mv": r.get("d_w3_mv"),
                "lead_firm": r.get("lead_firm"),
                "w20_gt_w5": r.get("w20_gt_w5"),
                "w20_gt_both": r.get("w20_gt_both"),
                "entry_minute": r.get("entry_minute"),
            }
            for r in rows
        ],
    }


def render_lead_firm_family_md(payload: dict[str, Any]) -> str:
    lines = [
        "# ABC v3+F1 · lead_firm 族掃描（W20 進場路徑穩健）",
        "",
        f"> **Verdict: {payload.get('verdict')}** · legs **{payload.get('n_annotated')}** · "
        f"窗口 **{payload.get('date_start')}** .. **{payload.get('date_end')}**",
        f"> baseline TP-only **{payload.get('baseline_mean_tp_pct')}%** · "
        f"出場：{payload.get('exit')}",
        "",
        "## 研究問題",
        "",
        "在 PB-MORPH-1 殘留線索上：`lead_firm`（W20 MV 進場前不降）或相對版 "
        "`d_w20 > d_w5`（W20 跌幅小於 W5）能否穩定抬高報酬？",
        "",
        f"- Pearson(d_w20, ret)={payload.get('corr_d_w20_vs_ret')}",
        f"- Pearson(d_w20−d_w5, ret)={payload.get('corr_rel_cushion_vs_ret')}",
        f"- 全樣本過門檻（n≥{payload.get('min_bucket_n')} · lift≥{payload.get('lift_pp_gate')}pp）："
        f"**{payload.get('any_variant_passes_gate')}**",
        f"- 切段穩定性（≥2/3 段 Δ>0 且 n≥10）：**{payload.get('any_variant_stable_2of3')}**",
        "",
        "## 變體全表",
        "",
        "| 變體 | n | 均% | 中位% | 勝率% | Δ pp | 補集均% | 過門檻 |",
        "|------|--:|----:|------:|------:|-----:|--------:|:------:|",
    ]
    for row in payload.get("variants") or []:
        lines.append(
            f"| {row.get('label')} | {row.get('n')} | {row.get('mean_pct')} | "
            f"{row.get('median_pct')} | {row.get('win_rate_pct')} | "
            f"{row.get('delta_vs_baseline_pp')} | {row.get('complement_mean_pct')} | "
            f"{'✔' if row.get('passes_gate') else '—'} |"
        )

    lines.extend(
        [
            "",
            "## d_w20 三分位",
            "",
            "| 桶 | d_w20 範圍 | n | 均% | Δ pp |",
            "|----|-----------|--:|----:|-----:|",
        ]
    )
    for row in payload.get("d_w20_terciles") or []:
        lines.append(
            f"| {row.get('id')} | [{row.get('d_w20_min')}, {row.get('d_w20_max')}] | "
            f"{row.get('n')} | {row.get('mean_pct')} | {row.get('delta_vs_baseline_pp')} |"
        )

    lines.extend(["", "## 時間三切段", ""])
    for block in payload.get("segments") or []:
        lines.append(
            f"### {block.get('id')} · {block.get('date_start')}..{block.get('date_end')} · "
            f"n={block.get('n')} · base={block.get('baseline_mean_tp_pct')}%"
        )
        lines.append("")
        lines.append("| 變體 | n | 均% | Δ pp |")
        lines.append("|------|--:|----:|-----:|")
        for row in block.get("variants") or []:
            lines.append(
                f"| {row.get('label')} | {row.get('n')} | {row.get('mean_pct')} | "
                f"{row.get('delta_vs_baseline_pp')} |"
            )
        lines.append("")

    lines.extend(
        [
            "## 切段穩定性摘要",
            "",
            "| 變體 | 正向段數 | 穩定（≥2/3） |",
            "|------|--------:|:------------:|",
        ]
    )
    for s in payload.get("stability") or []:
        lines.append(
            f"| {s.get('variant_id')} | {s.get('positive_segments')}/3 | "
            f"{'✔' if s.get('stable') else '—'} |"
        )

    best = payload.get("best_variant") or {}
    lines.extend(
        [
            "",
            "## 結案",
            "",
            f"- 最佳變體：`{best.get('id')}` · n={best.get('n')} · "
            f"Δ={best.get('delta_vs_baseline_pp')}pp · pass={best.get('passes_gate')}",
            f"- Verdict：**{payload.get('verdict')}**",
            "- 若 rejected：lead_firm 族不足以開 overlay；最多當弱觀察特徵。",
            "",
            "模組：`src/research/backtest/abc_v3_f1_lead_firm_family_study.py`",
            "",
        ]
    )
    return "\n".join(lines)
