"""C18acc · structural pyramid add study (RP-2 port · H-C18-PYRAMID-1).

Ports ABC v3+F1 RP-2 ``underwater_rebound`` pyramid-add research to C18acc
executed legs with KinematicSnap 30m timelines from the kinematic cache.

Differences vs ABC
--------------------
- Sample: C18acc sim executed legs (n99 slots, POOL1), not raw ABC first-trigger.
- leg1 exit: strategy sim dynamic exit (``return_pct`` on each leg), not fixed hold_5d.
- Primary exit convention: sync_exit at leg1's sim exit (same blended math as ABC).
- Secondary robustness: independent exit uses the *median* ``hold_days`` across legs.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from research.backtest.abc_v3_f1_entry_structure_sweep import _split_dates_is_oos
from research.backtest.abc_v3_f1_structural_pyramid_study import (
    BASELINE_B_ID,
    IndependentExitCtx,
    Leg,
    PyramidCondition,
    _fmt_stats,
    _strip_rows,
    baseline_b_condition,
    default_pyramid_conditions,
    evaluate_condition,
)
from research.backtest.archive.c18acc_kinematic_exit_sweep import load_kinematic_cache
from research.backtest.finpilot_local_backtest import load_price_panels
from research_config import load_research_splits

SCHEMA = "c18acc_structural_pyramid-v1"
HYPOTHESIS = "H-C18-PYRAMID-1"


def legs_from_cache(cache: dict[str, Any]) -> list[Leg]:
    legs = cache.get("legs") or []
    if not legs:
        raise ValueError("cache has no legs")
    return legs


def _median_hold_days(legs: list[Leg]) -> int:
    xs = sorted(int(l["hold_days"]) for l in legs if l.get("hold_days") is not None)
    return xs[len(xs) // 2] if xs else 5


def run_c18acc_structural_pyramid_study(
    conn: sqlite3.Connection,
    cache: dict[str, Any],
    *,
    min_trigger_n: int = 30,
    min_oos_trigger_n: int = 15,
    conditions: tuple[PyramidCondition, ...] | None = None,
) -> dict[str, Any]:
    """IS pick / OOS verify on pre-built C18acc kinematic timeline legs."""
    legs = legs_from_cache(cache)
    date_start = str(cache.get("date_start") or legs[0].get("entry_date"))
    date_end = str(cache.get("date_end") or legs[-1].get("entry_date"))
    hold_days = _median_hold_days(legs)
    n_slots = int(cache.get("n_slots") or 99)

    conditions = conditions or default_pyramid_conditions()
    b_cond = baseline_b_condition()

    close, _, _ = load_price_panels(conn)
    ind_ctx = IndependentExitCtx(conn=conn, close=close, hold_days=hold_days)

    split = load_research_splits().get("intraday_is_oos_70_30")
    ratio = float(split.ratio) if split and split.ratio is not None else 0.7
    full_dates = close.index.astype(str).tolist()
    sample_dates = [d for d in full_dates if date_start <= d <= date_end]
    is_dates, oos_dates = _split_dates_is_oos(sample_dates, ratio=ratio)
    is_set = set(is_dates)
    is_legs = [l for l in legs if str(l.get("entry_date")) in is_set]
    oos_legs = [l for l in legs if str(l.get("entry_date")) not in is_set]

    def _eval_all(seg_legs: list[Leg]) -> dict[str, dict[str, Any]]:
        out = {c.id: evaluate_condition(seg_legs, c, ind_ctx=ind_ctx) for c in conditions}
        out[b_cond.id] = evaluate_condition(seg_legs, b_cond, ind_ctx=ind_ctx)
        return out

    is_results = _eval_all(is_legs)
    oos_results = _eval_all(oos_legs)
    full_results = _eval_all(legs)

    eligible: list[tuple[float, str]] = []
    for c in conditions:
        r = is_results[c.id]
        if int(r["n_triggered"]) >= min_trigger_n:
            d = r["delta_vs_A_pp"].get("mean")
            if d is not None:
                eligible.append((float(d), c.id))
    eligible.sort(reverse=True)
    winner_id = eligible[0][1] if eligible else None

    criteria: list[dict[str, Any]] = []
    verdict = "rejected"
    if winner_id is None:
        criteria.append(
            {
                "criterion": f"IS trigger n ≥ {min_trigger_n} for at least one candidate",
                "passed": False,
                "detail": "no candidate reached the trigger floor in IS",
            }
        )
    else:
        w_is = is_results[winner_id]
        w_oos = oos_results[winner_id]
        b_is = is_results[b_cond.id]
        b_oos = oos_results[b_cond.id]

        c1_n = int(w_is["n_triggered"]) >= min_trigger_n
        criteria.append(
            {
                "criterion": f"trigger scale: IS n ≥ {min_trigger_n}",
                "passed": c1_n,
                "detail": f"IS n_triggered={w_is['n_triggered']} · OOS n_triggered={w_oos['n_triggered']}",
            }
        )

        da = w_is["delta_vs_A_pp"]
        _p_a = da.get("p_approx")
        c2_a = bool(da.get("mean") is not None and da["mean"] > 0 and _p_a is not None and _p_a < 0.05)
        criteria.append(
            {
                "criterion": "IS: blended significantly beats Baseline A (paired, p<0.05)",
                "passed": c2_a,
                "detail": f"IS Δ vs A = {da.get('mean')}pp · t={da.get('t')} · p≈{da.get('p_approx')}",
            }
        )

        db = w_is["delta_vs_B_same_legs_pp"]
        b_delta_is = b_is["delta_vs_A_pp"]
        _p_b = db.get("p_approx")
        c2_b = bool(
            db.get("mean") is not None
            and db["mean"] > 0
            and _p_b is not None
            and _p_b < 0.05
        )
        criteria.append(
            {
                "criterion": "IS: blended significantly beats Baseline B on the same legs (paired, p<0.05)",
                "passed": c2_b,
                "detail": (
                    f"IS Δ vs B(same legs) = {db.get('mean')}pp · t={db.get('t')} · "
                    f"p≈{db.get('p_approx')} · global B Δ vs A = {b_delta_is.get('mean')}pp"
                ),
            }
        )

        da_oos = w_oos["delta_vs_A_pp"]
        c3_dir = bool(
            int(w_oos["n_triggered"]) >= min_oos_trigger_n
            and da.get("mean") is not None
            and da_oos.get("mean") is not None
            and (da["mean"] > 0) == (da_oos["mean"] > 0)
        )
        criteria.append(
            {
                "criterion": f"OOS: same direction as IS (Δ vs A sign match, OOS n ≥ {min_oos_trigger_n})",
                "passed": c3_dir,
                "detail": (
                    f"IS Δ vs A = {da.get('mean')}pp · OOS Δ vs A = {da_oos.get('mean')}pp · "
                    f"OOS n={w_oos['n_triggered']} · OOS B Δ vs A = {b_oos['delta_vs_A_pp'].get('mean')}pp"
                ),
            }
        )

        n_pass = sum(1 for c in criteria if c["passed"])
        if n_pass == len(criteria):
            verdict = "passed"
        elif n_pass == 0 or not c1_n:
            verdict = "rejected"
        else:
            verdict = "mixed"

    sim_summary = cache.get("sim_summary") or {}
    leg1_only_mean = None
    if legs:
        leg1_only_mean = round(
            sum(float(l.get("return_pct") or 0) for l in legs) / len(legs), 3
        )

    return {
        "schema": SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "hypothesis": HYPOTHESIS,
        "parent_strategy": "rrg-mono-swap-accel",
        "date_start": date_start,
        "date_end": date_end,
        "n_slots": n_slots,
        "hold_days_median": hold_days,
        "exit_convention_primary": "sync_exit (both units exit at leg1 sim dynamic exit)",
        "exit_convention_secondary": (
            f"independent_{hold_days}d (leg2 exits at daily close hold_days from add; "
            "median hold across legs)"
        ),
        "split_spec": "intraday_is_oos_70_30",
        "split_ratio": ratio,
        "n_is_dates": len(is_dates),
        "n_oos_dates": len(oos_dates),
        "oos_date_start": oos_dates[0] if oos_dates else None,
        "n_legs": len(legs),
        "n_is_legs": len(is_legs),
        "n_oos_legs": len(oos_legs),
        "leg1_only_portfolio_mean_pct": leg1_only_mean,
        "sim_summary_mean_ret_pct": sim_summary.get("mean_ret_pct"),
        "min_trigger_n": min_trigger_n,
        "min_oos_trigger_n": min_oos_trigger_n,
        "conditions": [c.spec() for c in conditions],
        "baseline_b": b_cond.spec(),
        "is_results": {k: _strip_rows(v) for k, v in is_results.items()},
        "oos_results": {k: _strip_rows(v) for k, v in oos_results.items()},
        "full_results": {k: _strip_rows(v) for k, v in full_results.items()},
        "winner_id": winner_id,
        "criteria": criteria,
        "verdict": verdict,
        "abc_rp2_reference": {
            "hypothesis": "H-ENTRY-PYRAMID-1",
            "winner_id": "underwater_rebound",
            "verdict": "passed",
            "note": "ABC IS Δ vs A ≈ +0.944pp · OOS ≈ +0.983pp on raw F1 hold_5d legs",
        },
    }


def render_c18acc_structural_pyramid_md(payload: dict[str, Any]) -> str:
    winner_id = payload.get("winner_id")
    verdict = payload.get("verdict")
    hold_days = payload.get("hold_days_median")
    lines = [
        "# C18acc · 結構加碼（structural pyramid add）· RP-2 port / H-C18-PYRAMID-1",
        "",
        f"> Verdict: **{verdict}** · winner: `{winner_id or '—'}` · "
        f"窗口 **{payload.get('date_start')} .. {payload.get('date_end')}** · "
        f"n_slots **{payload.get('n_slots')}** · legs **{payload.get('n_legs')}**"
        f"（IS {payload.get('n_is_legs')} / OOS {payload.get('n_oos_legs')}，"
        f"OOS 自 {payload.get('oos_date_start')} 起）",
        "",
        f"> leg1-only portfolio mean（sim hold）**{payload.get('leg1_only_portfolio_mean_pct')}%** · "
        f"median hold **{hold_days}d**",
        "",
        "## 研究問題",
        "",
        "將 ABC RP-2 的 **underwater_rebound** 結構加碼假設移植到 C18acc：持倉中帳面虧損"
        "（`ret_from_entry_pct<0`）但 W3 RV 自持倉 trough 反彈 ≥0.3 時，等權重加碼第二筆的"
        "**混合報酬**（0.5·leg1 + 0.5·leg2）是否優於：",
        "",
        "- **Baseline A**：不加碼（單筆 sim 動態出場）",
        "- **Baseline B**：無條件逢低攤平（第一個 underwater poll 就加碼）",
        "",
        "## 設計",
        "",
        "- 樣本：C18acc sim executed legs（POOL1 · n99 槽），KinematicSnap 30m timeline cache",
        "- leg1 出場：沿用 sim 動態出場（非固定 hold_5d）；與 ABC 不同處",
        "- 加碼點：候選條件**第一次**成立的 poll（1 ≤ i ≤ len−2）；running W3-RV trough 僅回看 → 無 look-ahead",
        f"- **出場慣例（主）**：sync_exit —— 兩筆同在 leg1 sim 出場點出場",
        f"- **出場慣例（副）**：independent_{hold_days}d —— leg2 自加碼日獨立持有 median hold 個交易日",
        "- ABC RP-2 參考：`underwater_rebound` passed（IS Δ vs A ≈ +0.944pp）",
        "",
    ]

    def _table(seg_key: str, title: str) -> None:
        res = payload.get(seg_key) or {}
        lines.extend(
            [
                f"## {title}",
                "",
                "| 條件 | 觸發 n | 觸發率% | leg1-only 均%(A) | blended 均%(sync) | Δ vs A (paired) | Δ vs B 同 legs (paired) | blended 均%(ind) |",
                "|------|-------:|--------:|-----------------:|------------------:|-----------------|--------------------------|------------------:|",
            ]
        )
        order = [c["id"] for c in (payload.get("conditions") or [])] + [BASELINE_B_ID]
        for cid in order:
            r = res.get(cid)
            if not r:
                continue
            mark = " **⭐**" if cid == winner_id else ""
            lines.append(
                f"| `{cid}`{mark} | {r.get('n_triggered')} | {r.get('trigger_rate_pct')} | "
                f"{r.get('leg1_only_mean_pct')} | {r.get('blended_sync_mean_pct')} | "
                f"{_fmt_stats(r.get('delta_vs_A_pp') or {})} | "
                f"{_fmt_stats(r.get('delta_vs_B_same_legs_pp') or {})} | "
                f"{r.get('blended_ind_mean_pct')} (n={r.get('n_ind')}) |"
            )
        lines.append("")

    _table("is_results", "IS（挑選段）")
    _table("oos_results", "OOS（驗證段）")
    _table("full_results", "全窗口（參考）")

    lines.extend(["## 成功標準逐條判定（RP-2 §5）", ""])
    for c in payload.get("criteria") or []:
        lines.append(f"- {'✅' if c.get('passed') else '❌'} {c.get('criterion')} — {c.get('detail')}")
    lines.extend(["", f"## 結論：**{verdict}**", ""])
    if winner_id:
        w_is = (payload.get("is_results") or {}).get(winner_id) or {}
        w_oos = (payload.get("oos_results") or {}).get(winner_id) or {}
        lines.extend(
            [
                f"- IS winner `{winner_id}`：blended {w_is.get('blended_sync_mean_pct')}% vs "
                f"A {w_is.get('leg1_only_mean_pct')}%（Δ {_fmt_stats(w_is.get('delta_vs_A_pp') or {})}）",
                f"- OOS：blended {w_oos.get('blended_sync_mean_pct')}% vs "
                f"A {w_oos.get('leg1_only_mean_pct')}%（Δ {_fmt_stats(w_oos.get('delta_vs_A_pp') or {})}）",
                "",
            ]
        )
    lines.extend(
        [
            "---",
            "",
            "模組：`src/research/backtest/archive/c18acc_structural_pyramid_study.py` · "
            "runner：`scripts/research/archive/run_c18acc_structural_pyramid.py`",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "HYPOTHESIS",
    "SCHEMA",
    "legs_from_cache",
    "load_kinematic_cache",
    "render_c18acc_structural_pyramid_md",
    "run_c18acc_structural_pyramid_study",
]
