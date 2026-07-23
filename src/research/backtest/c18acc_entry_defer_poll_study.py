"""C18acc · defer entry poll when W3 MV/RV both flat/down · counterfactual study."""

from __future__ import annotations

import sqlite3
from typing import Any

from market_benchmark import load_benchmark_close
from research.backtest.archive.c18acc_triple_wma_extreme_profile import _frame_idx_for_minute
from research.backtest.c18acc_entry_momentum_gate_study import (
    _cohort_summary,
    _point_momentum,
    _prev_same_day_idx,
    collect_c18acc_entry_momentum_legs,
)
from research.backtest.dual_wma_signal_backtest import _price_at
from research.backtest.finpilot_local_backtest import load_price_panels


def _ret_from_prices(entry_px: float, exit_px: float) -> float | None:
    if entry_px <= 0 or exit_px <= 0:
        return None
    return round((exit_px / entry_px - 1.0) * 100.0, 4)


def _defer_row(
    leg: dict[str, Any],
    *,
    policy: str,
    entry_px: float,
    entry_minute: str,
    entry_i: int,
    entry_frame_idx: int,
    mom: dict[str, Any],
) -> dict[str, Any]:
    bench = leg.get("bench_return_pct")
    ret = _ret_from_prices(entry_px, float(leg["exit_px"]))
    if ret is None:
        return {}
    excess = round(ret - float(bench), 4) if bench is not None else None
    return {
        "policy": policy,
        "leg_id": leg["leg_id"],
        "stock_id": leg["stock_id"],
        "entry_date": leg["entry_date"],
        "baseline_minute": leg.get("entry_poll_minute"),
        "defer_minute": entry_minute,
        "defer_polls": max(0, entry_i - entry_frame_idx),
        "entry_px": round(entry_px, 4),
        "return_pct": ret,
        "excess_pct": excess,
        "baseline_return_pct": leg["return_pct"],
        "baseline_excess_pct": leg.get("excess_pct"),
        "delta_return_pp": round(ret - float(leg["return_pct"]), 4),
        "delta_excess_pp": round(float(excess or 0) - float(leg.get("excess_pct") or 0), 4)
        if excess is not None and leg.get("excess_pct") is not None
        else None,
        "w3_mv_rising_poll": mom.get("w3_mv_rising_poll"),
        "w3_rv_rising_poll": mom.get("w3_rv_rising_poll"),
        "w3_both_falling_poll": mom.get("w3_both_falling_poll"),
        "w3_mv": mom.get("w3_mv"),
        "w3_rv": mom.get("w3_rv"),
    }


def _scan_defer_options(
    conn: sqlite3.Connection,
    bench,
    leg: dict[str, Any],
    *,
    frames: list[dict[str, str]],
    points: list[dict[str, Any] | None],
    entry_i: int,
    entry_pt: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    sid = str(leg["stock_id"])
    n1 = entry_i + 1
    if n1 < len(frames) and frames[n1]["date"] == frames[entry_i]["date"]:
        px, _ = _price_at(conn, bench, sid, frames[n1]["date"], frames[n1]["minute"])
        pt1 = points[n1] if n1 < len(points) else None
        if px and px > 0 and pt1:
            mom = _point_momentum(pt1, entry_pt)
            row = _defer_row(
                leg,
                policy="defer_1_poll",
                entry_px=float(px),
                entry_minute=frames[n1]["minute"],
                entry_i=n1,
                entry_frame_idx=entry_i,
                mom=mom,
            )
            if row:
                out["defer_1_poll"] = row

    prev_pt = entry_pt
    for step in range(1, 4):
        j = entry_i + step
        if j >= len(frames) or frames[j]["date"] != frames[entry_i]["date"]:
            break
        pt = points[j] if j < len(points) else None
        if not pt:
            continue
        prev_j = _prev_same_day_idx(frames, j)
        prev = points[prev_j] if prev_j is not None and prev_j < len(points) else prev_pt
        mom = _point_momentum(pt, prev)
        if mom.get("w3_mv_rising_poll") or mom.get("w3_rv_rising_poll"):
            px, _ = _price_at(conn, bench, sid, frames[j]["date"], frames[j]["minute"])
            if px and px > 0:
                row = _defer_row(
                    leg,
                    policy="wait_until_rise",
                    entry_px=float(px),
                    entry_minute=frames[j]["minute"],
                    entry_i=j,
                    entry_frame_idx=entry_i,
                    mom=mom,
                )
                if row:
                    out["wait_until_rise"] = row
            break
    return out


def _apply_portfolio_policy(
    legs: list[dict[str, Any]],
    defer_by_leg: dict[str, dict[str, dict[str, Any]]],
    *,
    policy_id: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for leg in legs:
        both = bool(leg.get("w3_both_falling_poll"))
        opts = defer_by_leg.get(leg["leg_id"]) or {}
        if policy_id == "baseline":
            rows.append(leg)
        elif policy_id == "skip_both_falling":
            if not both:
                rows.append(leg)
        elif policy_id == "defer_1_if_both_falling":
            if both and "defer_1_poll" in opts:
                d = dict(leg)
                d["return_pct"] = opts["defer_1_poll"]["return_pct"]
                d["excess_pct"] = opts["defer_1_poll"]["excess_pct"]
                rows.append(d)
            else:
                rows.append(leg)
        elif policy_id == "wait_rise_if_both_falling":
            if both and "wait_until_rise" in opts:
                d = dict(leg)
                d["return_pct"] = opts["wait_until_rise"]["return_pct"]
                d["excess_pct"] = opts["wait_until_rise"]["excess_pct"]
                rows.append(d)
            elif both:
                continue
            else:
                rows.append(leg)
        elif policy_id == "only_if_rising":
            if leg.get("w3_mv_rising_poll") or leg.get("w3_rv_rising_poll"):
                rows.append(leg)
    return rows


def run_entry_defer_poll_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-02",
    date_end: str | None = None,
    confirm_bars: int = 2,
    is_end: str = "2025-12-31",
) -> dict[str, Any]:
    from collections import defaultdict

    from research.backtest.dual_wma_signal_backtest import _build_stock_frame_maps

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    legs, meta, day_cache = collect_c18acc_entry_momentum_legs(
        conn,
        date_start=date_start,
        date_end=date_end,
        confirm_bars=confirm_bars,
    )

    defer_by_leg: dict[str, dict[str, dict[str, Any]]] = {}
    both_defer_rows: list[dict[str, Any]] = []
    for leg in legs:
        if not leg.get("exit_px") or not leg.get("entry_px"):
            continue
        cached = day_cache.get((leg["entry_date"], leg["stock_id"]))
        if not cached:
            continue
        frames, points = cached
        entry_i = _frame_idx_for_minute(
            frames, leg["entry_date"], leg.get("entry_poll_minute")
        )
        entry_pt = points[entry_i] if entry_i < len(points) else None
        if not entry_pt:
            continue
        opts = _scan_defer_options(
            conn, bench, leg, frames=frames, points=points, entry_i=entry_i, entry_pt=entry_pt
        )
        if opts:
            defer_by_leg[leg["leg_id"]] = opts
        if leg.get("w3_both_falling_poll") and "defer_1_poll" in opts:
            both_defer_rows.append(opts["defer_1_poll"])

    policies = [
        ("baseline", "現行（不延後）"),
        ("defer_1_if_both_falling", "雙向未升 → 延後 1 poll"),
        ("wait_rise_if_both_falling", "雙向未升 → 等到 MV/RV 回升（≤3 poll）"),
        ("skip_both_falling", "雙向未升 → 略過不進"),
        ("only_if_rising", "僅 MV 或 RV 較上一格上升才進"),
    ]
    policy_stats: list[dict[str, Any]] = []
    base_ex = _cohort_summary(legs).get("mean_excess_pct")
    for pid, label in policies:
        rows = _apply_portfolio_policy(legs, defer_by_leg, policy_id=pid)
        s = _cohort_summary(rows)
        s["policy_id"] = pid
        s["label"] = label
        s["share_of_baseline_n_pct"] = round(100.0 * s["n"] / len(legs), 1) if legs else 0
        if base_ex is not None and s.get("mean_excess_pct") is not None:
            s["delta_vs_baseline_pp"] = round(float(s["mean_excess_pct"]) - float(base_ex), 4)
        policy_stats.append(s)

    bf = [r for r in legs if r.get("w3_both_falling_poll")]
    bf_base = _cohort_summary(bf)
    bf_defer = _cohort_summary(both_defer_rows) if both_defer_rows else {"n": 0}
    bf_lift = None
    if bf_base.get("mean_excess_pct") is not None and bf_defer.get("mean_excess_pct") is not None:
        bf_lift = round(float(bf_defer["mean_excess_pct"]) - float(bf_base["mean_excess_pct"]), 4)

    verdict = {
        "recommend_defer_1_poll": bool(
            (policy_stats[1].get("delta_vs_baseline_pp") or 0) > 0.2
        ),
        "recommend_wait_rise": bool(
            (policy_stats[2].get("delta_vs_baseline_pp") or 0) > 0.2
        ),
        "summary": (
            f"both_falling n={bf_base.get('n')} baseline excess {bf_base.get('mean_excess_pct')}% · "
            f"defer+1 excess {bf_defer.get('mean_excess_pct')}% Δ{bf_lift}pp · "
            f"portfolio defer_1 Δ{policy_stats[1].get('delta_vs_baseline_pp')}pp · "
            f"skip_both_falling Δ{policy_stats[3].get('delta_vs_baseline_pp')}pp"
        ),
    }
    return {
        "schema": "c18acc_entry_defer_poll-v1",
        **meta,
        "both_falling_baseline": bf_base,
        "both_falling_defer_1": bf_defer,
        "both_falling_defer_1_lift_pp": bf_lift,
        "policies": policy_stats,
        "n_defer_options": len(defer_by_leg),
        "verdict": verdict,
    }


def render_entry_defer_poll_md(payload: dict[str, Any]) -> str:
    lines = [
        "# C18acc · 進場延後 poll 研究（雙向未升時怎麼買）",
        "",
        f"窗口：**{payload['date_start']}** .. **{payload['date_end']}** · "
        f"confirm={payload.get('confirm_bars')} · 5m poll",
        "",
        "研究問題：進場當下 W3 MV/RV 雙向未升時，**延後 1 poll** 或 **等到回升** 是否優於現行？",
        "",
        "## 雙向未升子樣本 · 同 exit 反事實",
        "",
    ]
    bf = payload.get("both_falling_baseline") or {}
    d1 = payload.get("both_falling_defer_1") or {}
    lines.append(
        f"- 現行進場：n={bf.get('n')} · mean_excess **{bf.get('mean_excess_pct')}%**"
    )
    lines.append(
        f"- 延後 1 poll（同 sim exit）：n={d1.get('n')} · mean_excess **{d1.get('mean_excess_pct')}%** · "
        f"Δ **{payload.get('both_falling_defer_1_lift_pp')}pp**"
    )
    lines.extend(
        [
            "",
            "## 組合政策（post-hoc · 近似 slot）",
            "",
            "| 政策 | n | share% | mean_excess% | Δ vs baseline |",
            "|------|--:|-------:|-------------:|--------------:|",
        ]
    )
    for p in payload.get("policies") or []:
        lines.append(
            f"| {p.get('label')} | {p.get('n')} | {p.get('share_of_baseline_n_pct')} | "
            f"{p.get('mean_excess_pct')} | {p.get('delta_vs_baseline_pp')}pp |"
        )
    v = payload.get("verdict") or {}
    lines.extend(
        [
            "",
            "## 結論",
            "",
            f"- {v.get('summary', '')}",
            f"- 建議延後 1 poll：**{'是' if v.get('recommend_defer_1_poll') else '否'}**",
            f"- 建議等到回升：**{'是' if v.get('recommend_wait_rise') else '否'}**",
            "",
            "備註：post-hoc 不重演 slot；`avoid spread_mixed` slot re-sim 為結構面替代 gate。",
            "",
            "模組：`src/research/backtest/c18acc_entry_defer_poll_study.py`",
        ]
    )
    return "\n".join(lines) + "\n"
