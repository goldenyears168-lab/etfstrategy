"""C18acc · W6 parameter micro-tuning around daily RRG weakening stop + I36 stack."""

from __future__ import annotations

import sqlite3
from typing import Any

from research.backtest.c18acc_intraday_1m_hold import BLEED_SWEEP_CONTROLS
from research.backtest.c18acc_rrg_weakening_stop_sweep import (
    HYPOTHESES_RRG_WK,
    DailyQuadExitConfig,
    _run_window,
)
from research.backtest.rrg_mono_score_swap_c import close_champion_score_swap_c_config


def w6_tune_grid() -> list[DailyQuadExitConfig]:
    """Preregistered W6 neighborhood (~14 arms). Anchor = prior W6 spec."""
    wkg = ("weakening",)
    wkg_lag = ("weakening", "lagging")

    def _cfg(
        vid: str,
        label: str,
        *,
        consecutive_days: int = 2,
        min_hold: int = 5,
        mtm: float | None = None,
        quads: tuple[str, ...] = wkg,
        stack: bool = True,
    ) -> DailyQuadExitConfig:
        return DailyQuadExitConfig(
            vid,
            label,
            exit_quadrants=quads,
            consecutive_days=consecutive_days,
            min_hold_before_quad_exit=min_hold,
            mtm_threshold_pct=mtm,
            stack_extension=stack,
        )

    return [
        _cfg("T00", "anchor · W6 · 2d weakening · mh5 · I36 stack"),
        _cfg("T01", "quad-only · 2d weakening · mh5 · no I36 stack", stack=False),
        _cfg("T10", "1d weakening · mh5 · I36 stack", consecutive_days=1),
        _cfg("T12", "3d weakening · mh5 · I36 stack", consecutive_days=3),
        _cfg("T20", "2d weakening · mh0 · I36 stack", min_hold=0),
        _cfg("T21", "2d weakening · mh2 · I36 stack", min_hold=2),
        _cfg("T31", "2d weakening · mh5 · MTM≤−3% · I36 stack", mtm=-3.0),
        _cfg("T32", "2d weakening · mh5 · MTM≤−5% · I36 stack", mtm=-5.0),
        _cfg("T33", "2d weakening · mh5 · MTM≤−8% · I36 stack", mtm=-8.0),
        _cfg("T41", "2d weakening+lagging · mh5 · I36 stack", quads=wkg_lag),
        _cfg("T50", "1d weakening · mh5 · MTM≤−5% · I36 stack", consecutive_days=1, mtm=-5.0),
        _cfg(
            "T51",
            "2d weakening · mh2 · MTM≤−5% · I36 stack",
            min_hold=2,
            mtm=-5.0,
        ),
        _cfg("T52", "2d weakening · mh0 · MTM≤−5% · I36 stack", min_hold=0, mtm=-5.0),
        _cfg(
            "T53",
            "3d weakening · mh2 · I36 stack",
            consecutive_days=3,
            min_hold=2,
        ),
    ]


def _stacked_ids(grid: list[DailyQuadExitConfig]) -> set[str]:
    return {c.variant_id for c in grid if c.stack_extension}


def _patch_d1_for_stacked(
    full_result: dict[str, Any],
    stacked: set[str],
) -> None:
    """H-RRG-WK-D1 applies to any stacked arm in this tune sweep."""
    variants = ((full_result.get("hypothesis_results") or {}).get("variants") or {})
    for vid in stacked:
        vh = variants.get(vid)
        if not vh:
            continue
        hm = vh.setdefault("hypotheses", {})
        hm["H-RRG-WK-D1"] = bool(hm.get("H-RRG-WK-A2")) and bool(hm.get("H-RRG-WK-B1"))


def _rank_treatments(
    full_result: dict[str, Any],
    *,
    exclude: tuple[str, ...] = ("I0", "I36"),
) -> list[dict[str, Any]]:
    variants = ((full_result.get("hypothesis_results") or {}).get("variants") or {})
    return sorted(
        [s for s in full_result.get("summaries") or [] if s.get("variant_id") not in exclude],
        key=lambda s: (
            float(s.get("delta_vs_i36_pp") or -999),
            float(
                variants.get(s.get("variant_id"), {})
                .get("wk_metrics", {})
                .get("bleed_cohort_median_save_pct")
                or -999
            ),
        ),
        reverse=True,
    )


def _recommendation(
    *,
    best_is: dict[str, Any] | None,
    best_oos: dict[str, Any] | None,
    best_full: dict[str, Any] | None,
    is_summ: dict[str, dict[str, Any]],
    oos_summ: dict[str, dict[str, Any]],
    is_hypo: dict[str, dict[str, Any]],
    oos_hypo: dict[str, dict[str, Any]],
    full_hypo: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """adopt / next / reject based on IS/OOS stability and H-RRG-WK-A2."""
    full_id = (best_full or {}).get("variant_id")
    is_id = (best_is or {}).get("variant_id")
    oos_id = (best_oos or {}).get("variant_id")

    full_a2 = bool((full_hypo.get(full_id or "", {}) or {}).get("hypotheses", {}).get("H-RRG-WK-A2"))
    is_a2 = bool((is_hypo.get(is_id or "", {}) or {}).get("hypotheses", {}).get("H-RRG-WK-A2"))
    oos_a2 = bool((oos_hypo.get(oos_id or "", {}) or {}).get("hypotheses", {}).get("H-RRG-WK-A2"))

    is_delta = float((is_summ.get(is_id or "", {}) or {}).get("delta_vs_i36_pp") or -999)
    oos_delta = float((oos_summ.get(oos_id or "", {}) or {}).get("delta_vs_i36_pp") or -999)
    overfit = is_id != oos_id and is_delta - oos_delta > 0.5

    if full_a2 and oos_a2 and not overfit:
        action = "adopt"
        note = f"{full_id} passes FULL+OOS A2 · IS={is_id} OOS={oos_id} stable."
    elif full_id == "T00" and float((best_full or {}).get("delta_vs_i36_pp") or -999) >= -0.15:
        action = "next"
        note = (
            "No tuned arm beats W6 anchor on stability · OOS A2 still fails · "
            "try rotation-exit Track B (B6) or bleed cohort filters."
        )
    elif (best_full or {}).get("delta_vs_i36_pp") is not None and float(
        best_full.get("delta_vs_i36_pp")
    ) > -0.11:
        action = "next"
        note = (
            f"{full_id} improves FULL Δ vs W6 (−0.11pp) but A2/OOS gate not met · "
            f"overfit={'yes' if overfit else 'no'}."
        )
    else:
        action = "reject"
        note = "Tuned grid does not improve W6 · keep I36-only or revisit rotation force-exit."

    return {
        "action": action,
        "note": note,
        "best_full_id": full_id,
        "best_is_id": is_id,
        "best_oos_id": oos_id,
        "overfit_flag": overfit,
        "full_a2": full_a2,
        "oos_a2": oos_a2,
        "is_a2": is_a2,
    }


def run_w6_tune_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-30",
    is_end: str = "2025-12-31",
    oos_start: str = "2026-01-01",
) -> dict[str, Any]:
    """W6 micro-tune · IS/OOS/FULL · same evaluation contract as weakening-stop sweep."""
    close_cfg = close_champion_score_swap_c_config()
    i36_cfg = next(c for c in BLEED_SWEEP_CONTROLS if c.variant_id == "I36")
    grid = w6_tune_grid()
    stacked = _stacked_ids(grid)

    is_result = _run_window(
        conn, date_start=date_start, date_end=is_end, label="IS", close_cfg=close_cfg, i36_cfg=i36_cfg, grid=grid
    )
    oos_result = _run_window(
        conn, date_start=oos_start, date_end=date_end, label="OOS", close_cfg=close_cfg, i36_cfg=i36_cfg, grid=grid
    )
    full_result = _run_window(
        conn,
        date_start=date_start,
        date_end=date_end,
        label="FULL",
        close_cfg=close_cfg,
        i36_cfg=i36_cfg,
        grid=grid,
    )

    for block in (is_result, oos_result, full_result):
        _patch_d1_for_stacked(block, stacked)

    ranked_full = _rank_treatments(full_result)
    ranked_is = _rank_treatments(is_result)
    ranked_oos = _rank_treatments(oos_result)

    is_summ = {s["variant_id"]: s for s in (is_result.get("summaries") or [])}
    oos_summ = {s["variant_id"]: s for s in (oos_result.get("summaries") or [])}
    is_hypo = ((is_result.get("hypothesis_results") or {}).get("variants") or {})
    oos_hypo = ((oos_result.get("hypothesis_results") or {}).get("variants") or {})
    full_hypo = ((full_result.get("hypothesis_results") or {}).get("variants") or {})

    rec = _recommendation(
        best_is=ranked_is[0] if ranked_is else None,
        best_oos=ranked_oos[0] if ranked_oos else None,
        best_full=ranked_full[0] if ranked_full else None,
        is_summ=is_summ,
        oos_summ=oos_summ,
        is_hypo=is_hypo,
        oos_hypo=oos_hypo,
        full_hypo=full_hypo,
    )

    top3 = []
    for s in ranked_full[:3]:
        vid = s.get("variant_id")
        vh = full_hypo.get(vid, {})
        hm = vh.get("hypotheses") or {}
        top3.append(
            {
                "variant_id": vid,
                "label": s.get("label"),
                "full_delta_i36_pp": s.get("delta_vs_i36_pp"),
                "is_delta_i36_pp": is_summ.get(vid, {}).get("delta_vs_i36_pp"),
                "oos_delta_i36_pp": oos_summ.get(vid, {}).get("delta_vs_i36_pp"),
                "full_a2": hm.get("H-RRG-WK-A2"),
                "oos_a2": (oos_hypo.get(vid, {}) or {}).get("hypotheses", {}).get("H-RRG-WK-A2"),
                "full_b1": hm.get("H-RRG-WK-B1"),
                "full_c2": hm.get("H-RRG-WK-C2"),
                "full_c3": hm.get("H-RRG-WK-C3"),
                "full_d1": hm.get("H-RRG-WK-D1"),
            }
        )

    return {
        "study": "c18acc_w6_tune",
        "parent_study": "c18acc_rrg_weakening_stop",
        "anchor": "T00 replicates prior W6 (2d weakening · mh5 · I36 stack)",
        "base_config": close_cfg.to_dict(),
        "i36_control": i36_cfg.to_dict(),
        "date_start": date_start,
        "date_end": date_end,
        "is_end": is_end,
        "oos_start": oos_start,
        "hypotheses": HYPOTHESES_RRG_WK,
        "treatments": [c.to_dict() for c in grid],
        "controls": [c.to_dict() for c in BLEED_SWEEP_CONTROLS],
        "is": is_result,
        "oos": oos_result,
        "full": full_result,
        "best_by_delta_i36_full": ranked_full[0] if ranked_full else None,
        "best_by_delta_i36_is": ranked_is[0] if ranked_is else None,
        "best_by_delta_i36_oos": ranked_oos[0] if ranked_oos else None,
        "top3_full": top3,
        "recommendation": rec,
        "note": (
            "W6 參數微調 · close champion + I36 combo_spike (spike≥4% fade≥1% hybrid) · "
            "日線 RRG quad stop 格點 · 對照 I0/I36 · 錨點 T00 = 原 W6。"
        ),
    }


def render_w6_tune_md(payload: dict[str, Any]) -> str:
    full = payload.get("full") or {}
    hypo_root = full.get("hypothesis_results") or {}
    variants = hypo_root.get("variants") or {}
    grid = w6_tune_grid()
    rec = payload.get("recommendation") or {}

    lines = [
        "# C18acc W6 Parameter Micro-Tune Sweep",
        "",
        payload.get("note", ""),
        "",
        f"**FULL**：{payload.get('date_start')} .. {payload.get('date_end')}  ",
        f"**IS**：{payload.get('date_start')} .. {payload.get('is_end')}  ",
        f"**OOS**：{payload.get('oos_start')} .. {payload.get('date_end')}",
        "",
        f"Anchor：**T00** = prior **W6** · C_bleed **{full.get('bleed_cohort_n')}** · "
        f"C_pure_drip **{full.get('pure_drip_cohort_n')}**",
        "",
        "## Recommendation",
        "",
        f"- **Action**: `{rec.get('action')}`",
        f"- {rec.get('note')}",
        f"- Best FULL: **{rec.get('best_full_id')}** · IS: **{rec.get('best_is_id')}** · "
        f"OOS: **{rec.get('best_oos_id')}** · overfit: **{rec.get('overfit_flag')}**",
        "",
        "## Top 3 arms (FULL Δ I36)",
        "",
        "| id | FULL Δ | IS Δ | OOS Δ | FULL A2 | OOS A2 | B1 | C2 | C3 | D1 |",
        "|----|--------|------|-------|---------|--------|----|----|----|-----|",
    ]
    for row in payload.get("top3_full") or []:
        lines.append(
            f"| {row.get('variant_id')} | {row.get('full_delta_i36_pp')} "
            f"| {row.get('is_delta_i36_pp')} | {row.get('oos_delta_i36_pp')} "
            f"| {'✓' if row.get('full_a2') else '✗'} "
            f"| {'✓' if row.get('oos_a2') else '✗'} "
            f"| {'✓' if row.get('full_b1') else '✗'} "
            f"| {'✓' if row.get('full_c2') else '✗'} "
            f"| {'✓' if row.get('full_c3') else '✗'} "
            f"| {'✓' if row.get('full_d1') else '✗'} |"
        )

    lines += [
        "",
        "## All treatments · FULL",
        "",
        "| id | stack | quad | ext | Δ I36 | A2 | B1 | C2 | C3 | bleed save |",
        "|----|-------|------|-----|-------|----|----|----|----|------------|",
    ]
    for s in full.get("summaries") or []:
        vid = s.get("variant_id")
        if vid in ("I0",):
            continue
        vh = variants.get(vid, {})
        hm = vh.get("hypotheses") or {}
        wm = vh.get("wk_metrics") or {}
        stack = "—" if vid == "I36" else ("✓" if s.get("exit_mode") == "quad+ext_stack" else "✗")
        lines.append(
            f"| {vid} | {stack} | {s.get('quad_exits', 0)} | {s.get('ext_exits', 0)} "
            f"| {s.get('delta_vs_i36_pp')} "
            f"| {'✓' if hm.get('H-RRG-WK-A2') else '✗'} "
            f"| {'✓' if hm.get('H-RRG-WK-B1') else '✗'} "
            f"| {'✓' if hm.get('H-RRG-WK-C2') else '✗'} "
            f"| {'✓' if hm.get('H-RRG-WK-C3') else '✗'} "
            f"| {wm.get('bleed_cohort_median_save_pct')} |"
        )

    lines += [
        "",
        "## IS / OOS · Δ I36",
        "",
        "| id | IS Δ | OOS Δ | IS A2 | OOS A2 | IS B1 | OOS B1 |",
        "|----|------|-------|-------|--------|-------|--------|",
    ]
    is_summ = {s["variant_id"]: s for s in (payload.get("is") or {}).get("summaries") or []}
    oos_summ = {s["variant_id"]: s for s in (payload.get("oos") or {}).get("summaries") or []}
    is_hypo = ((payload.get("is") or {}).get("hypothesis_results") or {}).get("variants") or {}
    oos_hypo = ((payload.get("oos") or {}).get("hypothesis_results") or {}).get("variants") or {}
    for cfg in grid:
        vid = cfg.variant_id
        ih = is_hypo.get(vid, {}).get("hypotheses") or {}
        oh = oos_hypo.get(vid, {}).get("hypotheses") or {}
        lines.append(
            f"| {vid} | {is_summ.get(vid, {}).get('delta_vs_i36_pp')} "
            f"| {oos_summ.get(vid, {}).get('delta_vs_i36_pp')} "
            f"| {'✓' if ih.get('H-RRG-WK-A2') else '✗'} "
            f"| {'✓' if oh.get('H-RRG-WK-A2') else '✗'} "
            f"| {'✓' if ih.get('H-RRG-WK-B1') else '✗'} "
            f"| {'✓' if oh.get('H-RRG-WK-B1') else '✗'} |"
        )
    lines.append(
        f"| I36 | {is_summ.get('I36', {}).get('delta_vs_i36_pp')} "
        f"| {oos_summ.get('I36', {}).get('delta_vs_i36_pp')} "
        f"| ✓ | ✓ | ✗ | ✗ |"
    )

    lines += [
        "",
        "## Tuning dimensions",
        "",
        "| dim | values scanned |",
        "|-----|----------------|",
        "| consecutive_weakening_days | 1, 2, 3 |",
        "| min_hold_before_quad_exit | 0, 2, 5 |",
        "| mtm_threshold_pct | None, −3, −5, −8 |",
        "| exit_quadrants | weakening · weakening+lagging |",
        "| stack_extension | True (most) · False (T01) |",
        "",
        "---",
        "模組：`scripts/run_c18acc_w6_tune_sweep.py` · 延伸 `c18acc_rrg_weakening_stop_sweep`",
        "",
    ]
    return "\n".join(lines)
