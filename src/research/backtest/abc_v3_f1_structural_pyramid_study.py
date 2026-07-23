"""ABC v3+F1 · structural pyramid add study (RP-2 · H-ENTRY-PYRAMID-1).

Reframes the falsified H-ENTRY-RV3-1 "trough rebound" phenomenon from an
entry-timing question into a post-entry position-sizing question: when an
open ABC-V3-F1 leg goes underwater (ret_from_entry_pct < 0) but the RRG
structure has NOT deteriorated (W5 quadrant still leading/improving, dmv_w5
shallow, gap_mv_w20_w5 not converging), is adding an equal-weight second
unit better than (A) not adding at all and (B) adding unconditionally on
any drawdown?

Design notes
------------
- Sample: raw ABC-V3-F1 first-trigger stock-days (unconstrained universe,
  ``collect_abc_v3_f1_raw_legs``) with full KinematicSnap 30m-poll timelines.
- Add trigger: first poll i (1 <= i <= len(timeline)-2) where the candidate
  condition holds.  All condition inputs (w5_quad, dmv_w5, gap_mv_w20_w5,
  running W3-RV trough over the holding period so far) use only snaps
  0..i — no look-ahead.
- Position model (the one deliberate exception to the "no capital/slots"
  rule this round): equal-weight second unit,
  ``blended = 0.5 * leg1_ret + 0.5 * leg2_ret``.
- Exit convention (primary): *synchronized* — both units exit at leg1's
  original hold_5d exit, so ``leg2_ret`` is computable from the leg's own
  timeline: ``(1+r_exit)/(1+r_add) - 1``.  A secondary *independent* variant
  (leg2 holds its own 5 trading days from the add date, exit at daily close)
  is reported as robustness where daily data allows.
- Round-trip cost is ~neutral per unit of deployed capital (each unit pays
  the same RT cost whether deployed at entry or at the add), so gross
  returns are compared, consistent with the TP/SL and entry-gate studies.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from flow_returns import exit_close_date_from_entry
from research.backtest.abc_v3_f1_entry_structure_sweep import _split_dates_is_oos
from research.backtest.finpilot_local_backtest import load_price_panels
from research_config import load_research_splits

SCHEMA = "abc_v3_f1_structural_pyramid-v1"

Leg = dict[str, Any]

ConditionKind = Literal[
    "any_underwater",
    "underwater_stable",
    "underwater_rebound",
    "underwater_gap_intact",
    "underwater_stable_rebound",
]

BASELINE_B_ID = "baseline_b_any_underwater"


@dataclass(frozen=True)
class PyramidCondition:
    id: str
    label: str
    kind: ConditionKind
    dmv_w5_floor: float = -0.35
    rebound_min: float = 0.3
    gap_converge_max: float = 1.0

    def spec(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "dmv_w5_floor": self.dmv_w5_floor,
            "rebound_min": self.rebound_min,
            "gap_converge_max": self.gap_converge_max,
        }


def default_pyramid_conditions() -> tuple[PyramidCondition, ...]:
    """3-4 preregistered candidates (RP-2 §4.1) + unconditional baseline B."""
    return (
        PyramidCondition(
            id="underwater_stable",
            label="ret<0 · w5_quad∈{leading,improving} · dmv_w5>-0.35",
            kind="underwater_stable",
        ),
        PyramidCondition(
            id="underwater_rebound",
            label="ret<0 · W3 RV rebound from in-hold trough ≥0.3",
            kind="underwater_rebound",
        ),
        PyramidCondition(
            id="underwater_gap_intact",
            label="ret<0 · gap_mv_w20_w5 converged <1.0 vs entry",
            kind="underwater_gap_intact",
        ),
        PyramidCondition(
            id="underwater_stable_rebound",
            label="ret<0 · stable(w5 quad+dmv) AND rv3 rebound≥0.3",
            kind="underwater_stable_rebound",
        ),
    )


def baseline_b_condition() -> PyramidCondition:
    return PyramidCondition(
        id=BASELINE_B_ID,
        label="ret<0 · unconditional averaging-down (first underwater poll)",
        kind="any_underwater",
    )


def _stable_ok(snap: dict[str, Any], cond: PyramidCondition) -> bool:
    quad = str(snap.get("w5_quad") or "").lower()
    if quad not in ("leading", "improving"):
        return False
    dmv5 = snap.get("dmv_w5")
    return dmv5 is not None and float(dmv5) > cond.dmv_w5_floor + 1e-9


def _cond_ok(
    snap: dict[str, Any],
    cond: PyramidCondition,
    *,
    entry_gap_w20_w5: float | None,
    rv3_rebound: float | None,
) -> bool:
    if cond.kind == "any_underwater":
        return True
    if cond.kind == "underwater_stable":
        return _stable_ok(snap, cond)
    if cond.kind == "underwater_rebound":
        return rv3_rebound is not None and rv3_rebound >= cond.rebound_min - 1e-9
    if cond.kind == "underwater_gap_intact":
        gap = snap.get("gap_mv_w20_w5")
        if gap is None or entry_gap_w20_w5 is None:
            return False
        return float(gap) >= float(entry_gap_w20_w5) - cond.gap_converge_max - 1e-9
    if cond.kind == "underwater_stable_rebound":
        return (
            _stable_ok(snap, cond)
            and rv3_rebound is not None
            and rv3_rebound >= cond.rebound_min - 1e-9
        )
    return False


def find_add_idx(leg: Leg, cond: PyramidCondition) -> int | None:
    """First poll where ``cond`` holds while underwater.

    Only information available at poll i is used: ret_from_entry_pct,
    w5_quad, dmv_w5 (drawdown from *running* MV peak), gap_mv_w20_w5 vs the
    entry snapshot, and the running W3-RV trough over snaps 0..i.  The add
    must happen strictly after entry (i>=1) and strictly before the final
    exit snap (i<=len-2).
    """
    timeline = leg.get("timeline") or []
    if len(timeline) < 3:
        return None
    entry = leg.get("entry_t0") or timeline[0]
    entry_gap = entry.get("gap_mv_w20_w5")

    trough: float | None = None
    for i, snap in enumerate(timeline):
        rv3 = snap.get("w3_rv")
        rebound: float | None = None
        if rv3 is not None:
            rv3 = float(rv3)
            if trough is None or rv3 < trough:
                trough = rv3
            rebound = round(rv3 - trough, 4)
        if i == 0 or i > len(timeline) - 2:
            continue
        ret = snap.get("ret_from_entry_pct")
        if ret is None or float(ret) >= 0.0 - 1e-12:
            continue
        if _cond_ok(snap, cond, entry_gap_w20_w5=entry_gap, rv3_rebound=rebound):
            return i
    return None


@dataclass
class IndependentExitCtx:
    """Daily-close lookup for the independent leg2 exit variant."""

    conn: sqlite3.Connection
    close: Any  # pd.DataFrame date x stock_id
    hold_days: int

    def leg2_ret_pct(self, stock_id: str, add_date: str, add_px: float) -> float | None:
        exit_d = exit_close_date_from_entry(self.conn, add_date, self.hold_days)
        if not exit_d or add_px <= 0:
            return None
        try:
            px = self.close.at[exit_d, str(stock_id)]
        except KeyError:
            return None
        if px is None or (isinstance(px, float) and math.isnan(px)) or float(px) <= 0:
            return None
        return round((float(px) / add_px - 1.0) * 100.0, 3)


def simulate_add(
    leg: Leg,
    add_idx: int,
    *,
    ind_ctx: IndependentExitCtx | None = None,
) -> dict[str, Any]:
    timeline = leg["timeline"]
    snap = timeline[add_idx]
    leg1_ret = float(leg.get("return_pct") or 0.0)
    r_add = float(snap.get("ret_from_entry_pct") or 0.0)
    # leg2 (sync exit): rides from the add px to leg1's exit px.
    leg2_sync = ((1.0 + leg1_ret / 100.0) / (1.0 + r_add / 100.0) - 1.0) * 100.0
    row: dict[str, Any] = {
        "leg_id": leg.get("leg_id"),
        "stock_id": leg.get("stock_id"),
        "entry_date": leg.get("entry_date"),
        "add_idx": add_idx,
        "add_date": snap.get("trade_date"),
        "add_minute": snap.get("minute"),
        "add_ret_from_entry_pct": round(r_add, 3),
        "leg1_ret_pct": round(leg1_ret, 3),
        "leg2_sync_ret_pct": round(leg2_sync, 3),
        "blended_sync_ret_pct": round(0.5 * leg1_ret + 0.5 * leg2_sync, 3),
    }
    if ind_ctx is not None:
        leg2_ind = ind_ctx.leg2_ret_pct(
            str(leg.get("stock_id")), str(snap.get("trade_date")), float(snap.get("px") or 0)
        )
        row["leg2_ind_ret_pct"] = leg2_ind
        row["blended_ind_ret_pct"] = (
            round(0.5 * leg1_ret + 0.5 * leg2_ind, 3) if leg2_ind is not None else None
        )
    return row


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 3) if xs else None


def _paired_stats(deltas: list[float]) -> dict[str, Any]:
    """Mean / t-stat / two-sided normal-approx p for per-leg paired deltas."""
    n = len(deltas)
    if n < 2:
        return {"n": n, "mean": _mean(deltas), "t": None, "p_approx": None}
    m = sum(deltas) / n
    var = sum((x - m) ** 2 for x in deltas) / (n - 1)
    sd = math.sqrt(var)
    if sd < 1e-12:
        return {"n": n, "mean": round(m, 3), "t": None, "p_approx": None}
    t = m / (sd / math.sqrt(n))
    p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t) / math.sqrt(2.0))))
    return {"n": n, "mean": round(m, 3), "t": round(t, 3), "p_approx": round(p, 4)}


def evaluate_condition(
    legs: list[Leg],
    cond: PyramidCondition,
    *,
    ind_ctx: IndependentExitCtx | None = None,
) -> dict[str, Any]:
    """Evaluate one add condition on a leg set.

    Comparisons (all on the *triggered* subset, paired per leg):
      - blended (condition add) vs leg1-only            -> delta_vs_A
      - blended (condition add) vs blended (first-underwater add on the
        same legs)                                       -> delta_vs_B_same_legs
    Plus a portfolio view over ALL legs (non-triggered legs contribute
    leg1-only).
    """
    b_cond = baseline_b_condition()
    rows: list[dict[str, Any]] = []
    all_leg_rets: list[float] = []
    portfolio_rets: list[float] = []
    deltas_vs_a: list[float] = []
    deltas_vs_b_same: list[float] = []
    for leg in legs:
        leg1_ret = float(leg.get("return_pct") or 0.0)
        all_leg_rets.append(leg1_ret)
        idx = find_add_idx(leg, cond)
        if idx is None:
            portfolio_rets.append(leg1_ret)
            continue
        row = simulate_add(leg, idx, ind_ctx=ind_ctx)
        b_idx = find_add_idx(leg, b_cond)
        if b_idx is not None:
            b_row = simulate_add(leg, b_idx, ind_ctx=None)
            row["b_same_leg_blended_sync_ret_pct"] = b_row["blended_sync_ret_pct"]
            deltas_vs_b_same.append(
                float(row["blended_sync_ret_pct"]) - float(b_row["blended_sync_ret_pct"])
            )
        rows.append(row)
        portfolio_rets.append(float(row["blended_sync_ret_pct"]))
        deltas_vs_a.append(float(row["blended_sync_ret_pct"]) - leg1_ret)

    n_trig = len(rows)
    trig_leg1 = [float(r["leg1_ret_pct"]) for r in rows]
    trig_blended_sync = [float(r["blended_sync_ret_pct"]) for r in rows]
    ind_rows = [r for r in rows if r.get("blended_ind_ret_pct") is not None]
    out: dict[str, Any] = {
        "condition": cond.spec(),
        "n_legs": len(legs),
        "n_triggered": n_trig,
        "trigger_rate_pct": round(100.0 * n_trig / len(legs), 1) if legs else None,
        "win_rate_leg1_pct": (
            round(100.0 * sum(1 for x in trig_leg1 if x > 0) / n_trig, 1) if n_trig else None
        ),
        "win_rate_blended_pct": (
            round(100.0 * sum(1 for x in trig_blended_sync if x > 0) / n_trig, 1)
            if n_trig
            else None
        ),
        # triggered-subset means (sync-exit primary variant)
        "leg1_only_mean_pct": _mean(trig_leg1),
        "blended_sync_mean_pct": _mean(trig_blended_sync),
        "delta_vs_A_pp": _paired_stats(deltas_vs_a),
        "delta_vs_B_same_legs_pp": _paired_stats(deltas_vs_b_same),
        # independent-exit robustness variant
        "n_ind": len(ind_rows),
        "blended_ind_mean_pct": _mean([float(r["blended_ind_ret_pct"]) for r in ind_rows]),
        "leg1_only_mean_pct_ind_subset": _mean([float(r["leg1_ret_pct"]) for r in ind_rows]),
        # portfolio view over all legs
        "portfolio_mean_no_add_pct": _mean(all_leg_rets),
        "portfolio_mean_with_add_pct": _mean(portfolio_rets),
        "rows": rows,
    }
    return out


def _strip_rows(summary: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in summary.items() if k != "rows"}


def run_structural_pyramid_study(
    conn: sqlite3.Connection,
    legs: list[Leg],
    *,
    date_start: str,
    date_end: str,
    hold_days: int = 5,
    min_trigger_n: int = 30,
    min_oos_trigger_n: int = 15,
    conditions: tuple[PyramidCondition, ...] | None = None,
) -> dict[str, Any]:
    """IS pick / OOS verify on pre-collected raw ABC-V3-F1 legs."""
    if not legs:
        raise ValueError("no legs")
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

    # --- IS selection: best candidate by paired delta_vs_A among n>=min_trigger_n
    eligible: list[tuple[float, str]] = []
    for c in conditions:
        r = is_results[c.id]
        if int(r["n_triggered"]) >= min_trigger_n:
            d = r["delta_vs_A_pp"].get("mean")
            if d is not None:
                eligible.append((float(d), c.id))
    eligible.sort(reverse=True)
    winner_id = eligible[0][1] if eligible else None

    # --- verdict against RP-2 §5 success criteria
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

    return {
        "schema": SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "hypothesis": "H-ENTRY-PYRAMID-1",
        "date_start": date_start,
        "date_end": date_end,
        "hold_days": hold_days,
        "exit_convention_primary": "sync_exit (both units exit at leg1 hold_5d exit)",
        "exit_convention_secondary": "independent_5d (leg2 exits at daily close hold_5d from add)",
        "split_spec": "intraday_is_oos_70_30",
        "split_ratio": ratio,
        "n_is_dates": len(is_dates),
        "n_oos_dates": len(oos_dates),
        "oos_date_start": oos_dates[0] if oos_dates else None,
        "n_legs": len(legs),
        "n_is_legs": len(is_legs),
        "n_oos_legs": len(oos_legs),
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
    }


def _fmt_stats(s: dict[str, Any]) -> str:
    return f"{s.get('mean')}pp (t={s.get('t')}, p≈{s.get('p_approx')}, n={s.get('n')})"


def render_structural_pyramid_md(payload: dict[str, Any]) -> str:
    winner_id = payload.get("winner_id")
    verdict = payload.get("verdict")
    lines = [
        "# ABC v3+F1 · 結構加碼（structural pyramid add）· RP-2 / H-ENTRY-PYRAMID-1",
        "",
        f"> Verdict: **{verdict}** · winner: `{winner_id or '—'}` · "
        f"窗口 **{payload.get('date_start')} .. {payload.get('date_end')}** · "
        f"hold **{payload.get('hold_days')}d** · legs **{payload.get('n_legs')}**"
        f"（IS {payload.get('n_is_legs')} / OOS {payload.get('n_oos_legs')}，"
        f"OOS 自 {payload.get('oos_date_start')} 起）",
        "",
        "## 研究問題",
        "",
        "持倉中帳面虧損（`ret_from_entry_pct<0`）但 RRG 結構未惡化時，等權重加碼第二筆的"
        "**混合報酬**（0.5·leg1 + 0.5·leg2）是否優於：",
        "",
        "- **Baseline A**：不加碼（單筆 hold_5d）",
        "- **Baseline B**：無條件逢低攤平（第一個 underwater poll 就加碼，不看結構）",
        "",
        "## 設計",
        "",
        "- 樣本：raw ABC-V3-F1 first-trigger（無槽位/資金限制），KinematicSnap 30m timeline",
        "- 加碼點：候選條件**第一次**成立的 poll（1 ≤ i ≤ len−2）；判斷只用該 poll 當下已知資訊"
        "（running trough / running peak 皆只回看，不前看）→ 無 look-ahead",
        "- **出場慣例（主）**：sync_exit —— 兩筆同在 leg1 原 hold_5d 出場點出場，"
        "leg2 報酬 = (1+r_exit)/(1+r_add)−1（完全由 timeline 內資料計算）",
        "- **出場慣例（副，穩健性）**：independent_5d —— leg2 自加碼日獨立持有 5 個交易日、日線收盤出場",
        "- 成本：等權重下每單位資金的來回成本相同（加碼不增加單位成本），故沿用 gross 報酬比較",
        "- 本計畫是本輪唯一重新引入「部位大小」概念者（RP-2 §6 已聲明例外；採最簡等權重假設）",
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
    lines.extend(
        [
            "",
            f"## 結論：**{verdict}**",
            "",
        ]
    )
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
            "模組：`src/research/backtest/abc_v3_f1_structural_pyramid_study.py` · "
            "runner：`scripts/research/run_abc_v3_f1_structural_pyramid.py`",
        ]
    )
    return "\n".join(lines)
