"""C18acc · structural pyramid add · 3-slot portfolio resim (H-C18-PYRAMID-2).

Champion live stack (fresh · avoid_mixed · avg_accel entry · S2) vs the same
legs with post-hoc ``underwater_rebound`` equal-weight second unit (sync_exit).
Swap / S2 exits stay on the champion path; overlay only rewrites ``return_pct``.
"""

from __future__ import annotations

import copy
import sqlite3
from dataclasses import replace
from datetime import datetime
from typing import Any

from research.backtest.abc_v3_f1_structural_pyramid_study import (
    PyramidCondition,
    find_add_idx,
    simulate_add,
)
from research.backtest.archive.c18acc_hold3d_event_study import _prepare_c18acc_context
from research.backtest.archive.c18acc_kinematic_timeline_cache import (
    build_kinematic_legs_from_periods,
)
IS_END_DEFAULT = "2025-12-31"
OOS_START_DEFAULT = "2026-01-02"
from research.backtest.c18acc_entry_rank_study import _resolve_gate
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP
from research.backtest.rrg_mono_score_swap_c import (
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.slot_portfolio_metrics import portfolio_metrics_for_periods

SCHEMA = "c18acc_structural_pyramid_slot_resim-v1"
HYPOTHESIS = "H-C18-PYRAMID-2"
CONDITION_ID = "underwater_rebound"
PASS_OOS_PP = 0.3
MIN_OOS_N = 8
MIN_OOS_TRIGGER_N = 8
IS_NON_INFERIOR_PP = -0.3
DEFAULT_TOTAL_CAPITAL = 100_000.0


def underwater_rebound_condition(*, rebound_min: float = 0.3) -> PyramidCondition:
    return PyramidCondition(
        id=CONDITION_ID,
        label=f"ret<0 · W3 RV rebound from in-hold trough ≥{rebound_min}",
        kind="underwater_rebound",
        rebound_min=rebound_min,
    )


def _leg_key(period: dict[str, Any]) -> str:
    return (
        f"{period.get('entry_date')}|{period.get('stock_id')}|"
        f"{period.get('slot', 0)}"
    )


def _period_stats(periods: list[dict[str, Any]], summary: dict[str, Any] | None = None) -> dict[str, Any]:
    s = summarize_periods(periods)
    s["n_legs"] = len(periods)
    mr, mb = s.get("mean_return_pct"), s.get("mean_bench_pct")
    s["mean_excess_pct"] = (
        round(float(mr) - float(mb), 4) if mr is not None and mb is not None else None
    )
    if summary:
        s["swaps"] = summary.get("swaps_total")
        s["slot_util_pct"] = summary.get("slot_util_pct")
    return s


def _slice_periods(periods: list[dict[str, Any]], *, start: str, end: str) -> list[dict[str, Any]]:
    return [
        p
        for p in periods
        if start <= str(p.get("entry_date") or "") <= end
    ]


def overlay_pyramid_on_periods(
    periods: list[dict[str, Any]],
    legs: list[dict[str, Any]],
    cond: PyramidCondition,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rewrite ``return_pct`` to blended sync when underwater_rebound fires."""
    by_id = {str(leg.get("leg_id")): leg for leg in legs}
    out: list[dict[str, Any]] = []
    n_triggered = 0
    n_missing_timeline = 0
    for p in periods:
        row = copy.deepcopy(p)
        leg = by_id.get(_leg_key(p))
        if leg is None:
            n_missing_timeline += 1
            out.append(row)
            continue
        idx = find_add_idx(leg, cond)
        if idx is None:
            out.append(row)
            continue
        add = simulate_add(leg, idx)
        n_triggered += 1
        row["return_pct"] = float(add["blended_sync_ret_pct"])
        row["pyramid_triggered"] = True
        row["pyramid_add_date"] = add.get("add_date")
        row["pyramid_leg1_ret_pct"] = add.get("leg1_ret_pct")
        row["pyramid_leg2_sync_ret_pct"] = add.get("leg2_sync_ret_pct")
        row["pyramid_blended_sync_ret_pct"] = add.get("blended_sync_ret_pct")
        out.append(row)
    meta = {
        "n_periods": len(periods),
        "n_legs_with_timeline": len(periods) - n_missing_timeline,
        "n_missing_timeline": n_missing_timeline,
        "n_triggered": n_triggered,
        "trigger_rate_pct": round(100.0 * n_triggered / len(periods), 2) if periods else 0.0,
        "condition_id": cond.id,
        "rebound_min": cond.rebound_min,
    }
    return out, meta


def _window_row(
    periods: list[dict[str, Any]],
    *,
    start: str,
    end: str,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sub = _slice_periods(periods, start=start, end=end)
    stats = _period_stats(sub, summary)
    stats["n_triggered"] = sum(1 for p in sub if p.get("pyramid_triggered"))
    return stats


def _portfolio_slice(
    conn: sqlite3.Connection,
    periods: list[dict[str, Any]],
    trade_dates: list[str],
    *,
    start: str,
    end: str,
    n_slots: int,
    close,
    total_capital: float,
) -> dict[str, Any]:
    sub_dates = [d for d in trade_dates if start <= d <= end]
    if len(sub_dates) < 5:
        return {"n_trade_dates": len(sub_dates)}
    sub = _slice_periods(periods, start=start, end=end)
    port = portfolio_metrics_for_periods(
        conn,
        sub,
        sub_dates,
        total_capital=total_capital,
        n_slots=n_slots,
        close=close,
    )
    return {
        "n_trade_dates": len(sub_dates),
        "n_periods": len(sub),
        "total_return_pct": port.get("total_return_pct"),
        "cagr_pct": port.get("cagr_pct"),
        "max_drawdown_pct": port.get("max_drawdown_pct"),
        "avg_utilization_pct": port.get("avg_utilization_pct"),
    }


def evaluate_pyramid_slot_resim(
    baseline_slices: dict[str, dict[str, Any]],
    pyramid_slices: dict[str, dict[str, Any]],
    *,
    trigger_meta: dict[str, Any],
    pass_oos_pp: float = PASS_OOS_PP,
    min_oos_n: int = MIN_OOS_N,
    min_oos_trigger_n: int = MIN_OOS_TRIGGER_N,
    is_non_inferior_pp: float = IS_NON_INFERIOR_PP,
) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    for win in ("full", "is", "oos"):
        be = (baseline_slices.get(win) or {}).get("mean_excess_pct")
        pe = (pyramid_slices.get(win) or {}).get("mean_excess_pct")
        if be is not None and pe is not None:
            delta[f"delta_{win}_excess_pp"] = round(float(pe) - float(be), 4)
        delta[f"{win}_n"] = int((pyramid_slices.get(win) or {}).get("n_legs") or 0)
        delta[f"{win}_n_triggered"] = int((pyramid_slices.get(win) or {}).get("n_triggered") or 0)

    oos_d = delta.get("delta_oos_excess_pp")
    is_d = delta.get("delta_is_excess_pp")
    oos_n = delta.get("oos_n")
    oos_trig = delta.get("oos_n_triggered")
    pass_legs = bool(
        oos_d is not None
        and float(oos_d) >= pass_oos_pp
        and oos_n is not None
        and int(oos_n) >= min_oos_n
        and oos_trig is not None
        and int(oos_trig) >= min_oos_trigger_n
        and (is_d is None or float(is_d) >= is_non_inferior_pp)
    )
    return {
        **delta,
        "trigger_meta": trigger_meta,
        "pass_legs": pass_legs,
        "recommend_adopt": pass_legs,
        "gate": {
            "pass_oos_pp": pass_oos_pp,
            "min_oos_n": min_oos_n,
            "min_oos_trigger_n": min_oos_trigger_n,
            "is_non_inferior_pp": is_non_inferior_pp,
        },
        "summary": (
            f"OOS Δexcess={oos_d}pp n={oos_n} triggered={oos_trig} · "
            f"IS Δ={is_d}pp · {'ADOPT' if pass_legs else 'HOLD'}"
        ),
    }


def _simulate_champion_periods(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    n_slots: int,
    gate_cache_path: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any], Any]:
    """Return periods, summary, gate_meta, ctx, close."""
    ctx = _prepare_c18acc_context(conn, trade_dates)
    gate, gate_meta = _resolve_gate(
        conn,
        trade_dates,
        gate_cache_path=gate_cache_path,
        n_slots=n_slots,
    )
    cfg = replace(champion_score_swap_c_config(), n_slots=max(1, int(n_slots)))
    entry_c = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C3a")
    print(f"champion sim · n_slots={n_slots} …", flush=True)
    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=ctx["full_dates"],
        close=ctx["close"],
        bench=ctx["bench"],
        fresh_by_date=ctx["fresh_by_date"],
        zone_by_date=ctx["zone_by_date"],
        config=cfg,
        mono_by_date=ctx["mono_by_date"],
        kbar_cache=ctx["kbar_cache"],
        rs_mom=ctx["rs_mom"],
        rs_ratio=ctx["rs_ratio"],
        entry_c_config=entry_c,
        entry_gate_by_date=gate,
        swap_gate_by_date=gate,
    )
    return periods, summary, gate_meta, ctx, ctx["close"]


def run_c18acc_structural_pyramid_slot_resim(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    n_slots: int = 3,
    is_end: str = IS_END_DEFAULT,
    oos_start: str = OOS_START_DEFAULT,
    gate_cache_path: str | None = None,
    total_capital: float = DEFAULT_TOTAL_CAPITAL,
    rebound_min: float = 0.3,
    skip_pyramid: bool = False,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    if len(trade_dates) < 5:
        raise ValueError(f"insufficient trade dates: {len(trade_dates)}")

    print(
        f"pyramid-slot-resim: {trade_dates[0]}..{trade_dates[-1]} · n_slots={n_slots} …",
        flush=True,
    )
    periods, summary, gate_meta, ctx, close = _simulate_champion_periods(
        conn,
        trade_dates,
        n_slots=n_slots,
        gate_cache_path=gate_cache_path,
    )

    baseline_slices = {
        "full": _window_row(periods, start=trade_dates[0], end=trade_dates[-1], summary=summary),
        "is": _window_row(periods, start=trade_dates[0], end=is_end, summary=summary),
        "oos": _window_row(periods, start=oos_start, end=trade_dates[-1], summary=summary),
    }

    traj_meta: dict[str, Any] = {}
    trigger_meta: dict[str, Any] = {"skipped": True} if skip_pyramid else {}
    pyramid_slices = baseline_slices
    legs: list[dict[str, Any]] = []
    pyr_periods = periods
    verdict: dict[str, Any] = {
        "recommend_adopt": None,
        "summary": "champion-only (pyramid overlay skipped)",
        "pass_legs": None,
    }

    if not skip_pyramid:
        legs, traj_meta = build_kinematic_legs_from_periods(
            conn,
            periods,
            close=ctx["close"],
            full_dates=ctx["full_dates"],
        )
        cond = underwater_rebound_condition(rebound_min=rebound_min)
        pyr_periods, trigger_meta = overlay_pyramid_on_periods(periods, legs, cond)
        pyramid_slices = {
            "full": _window_row(pyr_periods, start=trade_dates[0], end=trade_dates[-1], summary=summary),
            "is": _window_row(pyr_periods, start=trade_dates[0], end=is_end, summary=summary),
            "oos": _window_row(pyr_periods, start=oos_start, end=trade_dates[-1], summary=summary),
        }
        verdict = evaluate_pyramid_slot_resim(
            baseline_slices,
            pyramid_slices,
            trigger_meta=trigger_meta,
        )

    nav = {
        "baseline": {
            "full": _portfolio_slice(
                conn, periods, trade_dates, start=trade_dates[0], end=trade_dates[-1],
                n_slots=n_slots, close=close, total_capital=total_capital,
            ),
            "oos": _portfolio_slice(
                conn, periods, trade_dates, start=oos_start, end=trade_dates[-1],
                n_slots=n_slots, close=close, total_capital=total_capital,
            ),
        },
        "pyramid": {
            "full": _portfolio_slice(
                conn, pyr_periods, trade_dates, start=trade_dates[0], end=trade_dates[-1],
                n_slots=n_slots, close=close, total_capital=total_capital,
            ),
            "oos": _portfolio_slice(
                conn, pyr_periods, trade_dates, start=oos_start, end=trade_dates[-1],
                n_slots=n_slots, close=close, total_capital=total_capital,
            ),
        },
        "note": (
            "NAV applies period return_pct on the slot book "
            "(pyramid blended return when overlay enabled; not a separate cash reserve model)."
        ),
    }

    return {
        "schema": SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "hypothesis": HYPOTHESIS,
        "parent_strategy": "rrg-mono-swap-accel",
        "condition_id": CONDITION_ID,
        "date_start": trade_dates[0],
        "date_end": trade_dates[-1],
        "is_end": is_end,
        "oos_start": oos_start,
        "n_slots": n_slots,
        "skip_pyramid": skip_pyramid,
        "n_periods": len(periods),
        "n_legs_timeline": len(legs),
        "sim_summary": summary,
        "gate_meta": gate_meta,
        "trajectory_meta": traj_meta,
        "baseline_slices": baseline_slices,
        "pyramid_slices": pyramid_slices,
        "trigger_meta": trigger_meta,
        "nav": nav,
        "verdict": verdict,
    }


def run_c18acc_slot_count_compare(
    conn: sqlite3.Connection,
    *,
    slot_counts: tuple[int, ...] = (3, 4),
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str = IS_END_DEFAULT,
    oos_start: str = OOS_START_DEFAULT,
    gate_cache_path: str | None = None,
    total_capital: float = DEFAULT_TOTAL_CAPITAL,
) -> dict[str, Any]:
    """Champion live stack · compare n_slots (pyramid overlay skipped)."""
    by_slots: dict[str, Any] = {}
    for n in slot_counts:
        payload = run_c18acc_structural_pyramid_slot_resim(
            conn,
            date_start=date_start,
            date_end=date_end,
            n_slots=int(n),
            is_end=is_end,
            oos_start=oos_start,
            gate_cache_path=gate_cache_path,
            total_capital=total_capital,
            skip_pyramid=True,
        )
        by_slots[str(n)] = {
            "n_slots": n,
            "n_periods": payload["n_periods"],
            "sim_summary": payload.get("sim_summary"),
            "baseline_slices": payload["baseline_slices"],
            "nav": payload["nav"]["baseline"],
            "date_start": payload["date_start"],
            "date_end": payload["date_end"],
        }

    keys = [str(n) for n in slot_counts]
    base_k, chal_k = keys[0], keys[1] if len(keys) > 1 else keys[0]
    delta: dict[str, Any] = {}
    for win in ("full", "is", "oos"):
        b = (by_slots[base_k]["baseline_slices"].get(win) or {})
        c = (by_slots[chal_k]["baseline_slices"].get(win) or {})
        be, ce = b.get("mean_excess_pct"), c.get("mean_excess_pct")
        if be is not None and ce is not None:
            delta[f"delta_{win}_excess_pp"] = round(float(ce) - float(be), 4)
        delta[f"delta_{win}_n"] = int(c.get("n_legs") or 0) - int(b.get("n_legs") or 0)
        br, cr = b.get("mean_return_pct"), c.get("mean_return_pct")
        if br is not None and cr is not None:
            delta[f"delta_{win}_ret_pp"] = round(float(cr) - float(br), 4)

    b_nav = (by_slots[base_k]["nav"].get("full") or {})
    c_nav = (by_slots[chal_k]["nav"].get("full") or {})
    if b_nav.get("total_return_pct") is not None and c_nav.get("total_return_pct") is not None:
        delta["delta_full_nav_total_pp"] = round(
            float(c_nav["total_return_pct"]) - float(b_nav["total_return_pct"]), 4
        )
    if b_nav.get("avg_utilization_pct") is not None and c_nav.get("avg_utilization_pct") is not None:
        delta["delta_full_util_pp"] = round(
            float(c_nav["avg_utilization_pct"]) - float(b_nav["avg_utilization_pct"]), 4
        )

    return {
        "schema": "c18acc_slot_count_compare-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "parent_strategy": "rrg-mono-swap-accel",
        "stack": "fresh · avoid_mixed · C3a avg_accel · S2 · no pyramid overlay",
        "total_capital": total_capital,
        "slot_counts": list(slot_counts),
        "by_slots": by_slots,
        "delta": delta,
        "note": (
            f"Δ = n_slots={chal_k} − n_slots={base_k}. "
            "Fixed total_capital → per-slot notional shrinks as n_slots rises."
        ),
    }


def render_c18acc_slot_count_compare_md(payload: dict[str, Any]) -> str:
    slots = payload.get("slot_counts") or []
    by = payload.get("by_slots") or {}
    d = payload.get("delta") or {}
    lines = [
        "# C18acc · 槽位數比較（champion live stack）",
        "",
        f"> {payload.get('note')}",
        "",
        f"Stack：{payload.get('stack')} · capital **{payload.get('total_capital'):,.0f}**",
        "",
        "## 腿級 mean_excess",
        "",
        "| n_slots | window | n legs | mean_ret% | mean_excess% | swaps | util% |",
        "|--------:|--------|-------:|----------:|-------------:|------:|------:|",
    ]
    for n in slots:
        row = by.get(str(n)) or {}
        summary = row.get("sim_summary") or {}
        for win in ("full", "is", "oos"):
            s = (row.get("baseline_slices") or {}).get(win) or {}
            swaps = summary.get("swaps_total") if win == "full" else "—"
            util = summary.get("slot_util_pct") if win == "full" else "—"
            lines.append(
                f"| {n} | {win} | {s.get('n_legs')} | {s.get('mean_return_pct')} | "
                f"{s.get('mean_excess_pct')} | {swaps} | {util} |"
            )
    lines.extend(
        [
            "",
            "## Δ（後 − 前）",
            "",
            f"- FULL excess: **{d.get('delta_full_excess_pp')}pp** · legs Δ **{d.get('delta_full_n')}**",
            f"- IS excess: **{d.get('delta_is_excess_pp')}pp** · legs Δ **{d.get('delta_is_n')}**",
            f"- OOS excess: **{d.get('delta_oos_excess_pp')}pp** · legs Δ **{d.get('delta_oos_n')}**",
            f"- FULL NAV total: **{d.get('delta_full_nav_total_pp')}pp** · util Δ **{d.get('delta_full_util_pp')}pp**",
            "",
            "## NAV（fixed capital）",
            "",
            "| n_slots | window | total_ret% | CAGR% | maxDD% | util% |",
            "|--------:|--------|-----------:|------:|-------:|------:|",
        ]
    )
    for n in slots:
        nav = (by.get(str(n)) or {}).get("nav") or {}
        for win in ("full", "oos"):
            s = nav.get(win) or {}
            lines.append(
                f"| {n} | {win} | {s.get('total_return_pct')} | {s.get('cagr_pct')} | "
                f"{s.get('max_drawdown_pct')} | {s.get('avg_utilization_pct')} |"
            )
    lines.extend(["", "模組：`c18acc_structural_pyramid_slot_resim.run_c18acc_slot_count_compare`", ""])
    return "\n".join(lines)


def render_c18acc_structural_pyramid_slot_resim_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    lines = [
        "# C18acc · 結構加碼 · 3-槽 slot re-sim / H-C18-PYRAMID-2",
        "",
        f"> Verdict: **{'ADOPT' if v.get('recommend_adopt') else 'HOLD'}** · "
        f"{v.get('summary')}",
        "",
        f"窗口：**{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"{payload.get('n_slots')} 槽 · IS ≤ **{payload.get('is_end')}** · "
        f"OOS ≥ **{payload.get('oos_start')}**",
        "",
        "Champion live stack（fresh · avoid_mixed · C3a avg_accel · S2）+ "
        f"`{payload.get('condition_id')}` post-hoc sync_exit overlay。",
        "",
        "## 觸發",
        "",
        f"- timeline legs: **{payload.get('n_legs_timeline')}** / periods "
        f"**{payload.get('n_periods')}**",
        f"- triggered: **{(payload.get('trigger_meta') or {}).get('n_triggered')}** "
        f"({(payload.get('trigger_meta') or {}).get('trigger_rate_pct')}%)",
        "",
        "## 變體 × 窗口（mean_excess）",
        "",
        "| variant | window | n | triggered | mean_ret% | mean_excess% | Δexcess |",
        "|---------|--------|--:|----------:|----------:|-------------:|--------:|",
    ]
    for win in ("full", "is", "oos"):
        b = (payload.get("baseline_slices") or {}).get(win) or {}
        p = (payload.get("pyramid_slices") or {}).get(win) or {}
        d = v.get(f"delta_{win}_excess_pp")
        lines.append(
            f"| champion | {win} | {b.get('n_legs')} | — | {b.get('mean_return_pct')} | "
            f"{b.get('mean_excess_pct')} | — |"
        )
        lines.append(
            f"| +underwater_rebound | {win} | {p.get('n_legs')} | {p.get('n_triggered')} | "
            f"{p.get('mean_return_pct')} | {p.get('mean_excess_pct')} | {d} |"
        )
    gate = v.get("gate") or {}
    lines.extend(
        [
            "",
            "## 採納閘門",
            "",
            f"- OOS n≥{gate.get('min_oos_n')} · triggered≥{gate.get('min_oos_trigger_n')} · "
            f"Δexcess≥{gate.get('pass_oos_pp')}pp · IS Δ≥{gate.get('is_non_inferior_pp')}pp",
            f"- **recommend_adopt**: `{v.get('recommend_adopt')}`",
            "",
            "模組：`src/research/backtest/c18acc_structural_pyramid_slot_resim.py` · "
            "runner：`scripts/research/run_c18acc_structural_pyramid_slot_resim.py`",
            "",
        ]
    )
    return "\n".join(lines)
