"""C18acc × ABC v3+f1 · dual-sleeve Phase0 overlap diagnostics (research only).

Does not modify strategy specs. Measures whether the two sleeves are
complementary enough to justify Phase1 capital-split NAV simulation.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from typing import Any, Iterable

from backtest_standard_config import comparison_notional_ntd, load_backtest_standard_config
from research.backtest.archive.abc_v3_slot_sweep import (
    ABC_V3_HOLD_DAYS,
    ABC_V3_SLOT_CHAMPION,
    _build_abc_v3_intraday_panel,
    apply_score_spec,
    collect_abc_v3_skip0900_period_candidates,
    order_periods_for_fill,
)
from research.backtest.archive.c18acc_abc_v3_f1_comparison import (
    C18ACC_SLOTS,
    _portfolio_block,
    select_executed_slot_periods,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_score_swap_c import build_c18acc_s2_executed_legs_for_timeline
from research.backtest.triple_wma_pullback_sweep import matches_w3_rv_hl_abc_v3_f1

# Phase0 preregistered gates · dual-sleeve Phase1 entry
PHASE0_MAX_ENTRY_JACCARD = 0.30
PHASE0_MAX_SAME_DAY_STOCK_RATE = 0.20
PHASE0_MAX_DAILY_RETURN_CORR = 0.50


def _entry_pairs(rows: Iterable[dict[str, Any]]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for r in rows:
        d = str(r.get("entry_date") or "")
        sid = str(r.get("stock_id") or "")
        if d and sid:
            out.add((d, sid))
    return out


def _entries_by_date(rows: Iterable[dict[str, Any]]) -> dict[str, set[str]]:
    by_date: dict[str, set[str]] = {}
    for r in rows:
        d = str(r.get("entry_date") or "")
        sid = str(r.get("stock_id") or "")
        if not d or not sid:
            continue
        by_date.setdefault(d, set()).add(sid)
    return by_date


def _jaccard(a: set[Any], b: set[Any]) -> float | None:
    if not a and not b:
        return None
    union = a | b
    if not union:
        return None
    return len(a & b) / len(union)


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = min(len(xs), len(ys))
    if n < 3:
        return None
    mx = sum(xs[:n]) / n
    my = sum(ys[:n]) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den_x = math.sqrt(sum((xs[i] - mx) ** 2 for i in range(n)))
    den_y = math.sqrt(sum((ys[i] - my) ** 2 for i in range(n)))
    if den_x <= 0 or den_y <= 0:
        return None
    return num / (den_x * den_y)


def compute_entry_overlap(
    c18_rows: list[dict[str, Any]],
    abc_rows: list[dict[str, Any]],
    *,
    abc_candidate_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Stock-day entry overlap · executed legs (+ optional ABC candidates)."""
    c18_pairs = _entry_pairs(c18_rows)
    abc_pairs = _entry_pairs(abc_rows)
    cand_pairs = _entry_pairs(abc_candidate_rows) if abc_candidate_rows else set()

    union_exec = c18_pairs | abc_pairs
    both_exec = c18_pairs & abc_pairs
    c18_only = c18_pairs - abc_pairs
    abc_only = abc_pairs - c18_pairs

    c18_by = _entries_by_date(c18_rows)
    abc_by = _entries_by_date(abc_rows)
    all_days = sorted(set(c18_by) | set(abc_by))
    day_jaccards: list[float] = []
    day_both = 0
    day_any = 0
    for d in all_days:
        a = c18_by.get(d, set())
        b = abc_by.get(d, set())
        if not a and not b:
            continue
        day_any += 1
        if a & b:
            day_both += 1
        j = _jaccard(a, b)
        if j is not None:
            day_jaccards.append(j)

    underfill_days = 0
    abc_on_underfill = 0
    abc_cand_on_underfill = 0
    cand_by = _entries_by_date(abc_candidate_rows or [])
    for d in all_days:
        n_c18 = len(c18_by.get(d, set()))
        if n_c18 >= C18ACC_SLOTS:
            continue
        underfill_days += 1
        if abc_by.get(d):
            abc_on_underfill += 1
        if cand_by.get(d):
            abc_cand_on_underfill += 1

    cand_union = c18_pairs | cand_pairs if cand_pairs else set()
    both_cand = c18_pairs & cand_pairs if cand_pairs else set()

    return {
        "c18_executed_pairs": len(c18_pairs),
        "abc_executed_pairs": len(abc_pairs),
        "union_executed_pairs": len(union_exec),
        "both_executed_pairs": len(both_exec),
        "c18_only_pairs": len(c18_only),
        "abc_only_pairs": len(abc_only),
        "entry_jaccard_executed": round(_jaccard(c18_pairs, abc_pairs) or 0.0, 4)
        if union_exec
        else None,
        "same_day_stock_rate_executed": round(len(both_exec) / len(union_exec), 4)
        if union_exec
        else None,
        "mean_daily_jaccard_executed": round(sum(day_jaccards) / len(day_jaccards), 4)
        if day_jaccards
        else None,
        "days_with_any_entry": day_any,
        "days_with_both_sleeves": day_both,
        "days_both_rate": round(day_both / day_any, 4) if day_any else None,
        "c18_underfill_days": underfill_days,
        "abc_executed_on_c18_underfill_days": abc_on_underfill,
        "abc_candidates_on_c18_underfill_days": abc_cand_on_underfill,
        "abc_candidate_pairs": len(cand_pairs) if cand_pairs else None,
        "entry_jaccard_c18_vs_abc_candidates": round(_jaccard(c18_pairs, cand_pairs) or 0.0, 4)
        if cand_union
        else None,
        "both_c18_abc_candidate_pairs": len(both_cand) if cand_pairs else None,
    }


def compute_daily_return_correlation(
    c18_daily_nav: list[dict[str, Any]],
    abc_daily_nav: list[dict[str, Any]],
) -> dict[str, Any]:
    """Align portfolio daily NAV · return Pearson on daily % changes."""
    c18_eq = {str(r["date"]): float(r["equity_ntd"]) for r in c18_daily_nav}
    abc_eq = {str(r["date"]): float(r["equity_ntd"]) for r in abc_daily_nav}
    dates = sorted(set(c18_eq) & set(abc_eq))
    c18_rets: list[float] = []
    abc_rets: list[float] = []
    for i in range(1, len(dates)):
        d0, d1 = dates[i - 1], dates[i]
        e0_c, e1_c = c18_eq.get(d0), c18_eq.get(d1)
        e0_a, e1_a = abc_eq.get(d0), abc_eq.get(d1)
        if not e0_c or not e1_c or not e0_a or not e1_a:
            continue
        if e0_c <= 0 or e0_a <= 0:
            continue
        c18_rets.append((e1_c / e0_c - 1.0) * 100.0)
        abc_rets.append((e1_a / e0_a - 1.0) * 100.0)
    corr = _pearson(c18_rets, abc_rets)
    return {
        "n_aligned_days": len(c18_rets),
        "daily_return_corr": round(corr, 4) if corr is not None else None,
    }


def compute_leg_return_correlation(
    c18_rows: list[dict[str, Any]],
    abc_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Pearson on overlapping (entry_date, stock_id) leg returns."""
    c18_map = {
        (str(r["entry_date"]), str(r["stock_id"])): float(r.get("return_pct") or r.get("net_pct") or 0)
        for r in c18_rows
        if r.get("entry_date") and r.get("stock_id")
    }
    abc_map = {
        (str(r["entry_date"]), str(r["stock_id"])): float(r.get("net_pct") or r.get("return_pct") or 0)
        for r in abc_rows
        if r.get("entry_date") and r.get("stock_id")
    }
    keys = sorted(set(c18_map) & set(abc_map))
    if len(keys) < 3:
        return {"n_overlap_legs": len(keys), "leg_return_corr": None}
    xs = [c18_map[k] for k in keys]
    ys = [abc_map[k] for k in keys]
    corr = _pearson(xs, ys)
    return {
        "n_overlap_legs": len(keys),
        "leg_return_corr": round(corr, 4) if corr is not None else None,
        "mean_c18_ret_overlap_pct": round(sum(xs) / len(xs), 3),
        "mean_abc_ret_overlap_pct": round(sum(ys) / len(ys), 3),
    }


def phase0_verdict(
    overlap: dict[str, Any],
    corr: dict[str, Any],
    *,
    max_entry_jaccard: float = PHASE0_MAX_ENTRY_JACCARD,
    max_same_day_stock_rate: float = PHASE0_MAX_SAME_DAY_STOCK_RATE,
    max_daily_return_corr: float = PHASE0_MAX_DAILY_RETURN_CORR,
) -> dict[str, Any]:
    jacc = overlap.get("entry_jaccard_executed")
    conflict = overlap.get("same_day_stock_rate_executed")
    dcorr = corr.get("daily_return_corr")

    checks = {
        "entry_jaccard_ok": jacc is not None and jacc <= max_entry_jaccard,
        "same_day_conflict_ok": conflict is not None and conflict <= max_same_day_stock_rate,
        "daily_return_corr_ok": dcorr is not None and abs(dcorr) <= max_daily_return_corr,
    }
    go = all(checks.values())
    fails = [k for k, v in checks.items() if not v]
    summary = (
        f"{'GO_PHASE1_DUAL_SLEEVE' if go else 'NO_GO_PHASE1'} · "
        f"Jaccard={jacc} · conflict={conflict} · daily_corr={dcorr} · "
        f"thresholds j≤{max_entry_jaccard} c≤{max_same_day_stock_rate} |r|≤{max_daily_return_corr}"
    )
    if not go:
        summary += f" · failed={','.join(fails)}"
    return {
        "go_phase1_dual_sleeve": go,
        "checks": checks,
        "thresholds": {
            "max_entry_jaccard": max_entry_jaccard,
            "max_same_day_stock_rate": max_same_day_stock_rate,
            "max_daily_return_corr": max_daily_return_corr,
        },
        "summary": summary,
    }


def build_phase0_payload(
    *,
    date_start: str,
    date_end: str,
    n_trade_dates: int,
    c18_rows: list[dict[str, Any]],
    abc_rows: list[dict[str, Any]],
    c18_daily_nav: list[dict[str, Any]],
    abc_daily_nav: list[dict[str, Any]],
    abc_candidate_rows: list[dict[str, Any]] | None = None,
    source: str = "live",
    runtime_s: float | None = None,
) -> dict[str, Any]:
    overlap = compute_entry_overlap(c18_rows, abc_rows, abc_candidate_rows=abc_candidate_rows)
    daily_corr = compute_daily_return_correlation(c18_daily_nav, abc_daily_nav)
    leg_corr = compute_leg_return_correlation(c18_rows, abc_rows)
    verdict = phase0_verdict(overlap, daily_corr)
    return {
        "schema": "c18acc_abc_dual_sleeve_phase0-v1",
        "topic": "c18acc-abc-dual-sleeve",
        "phase": "phase0_overlap",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "date_start": date_start,
        "date_end": date_end,
        "n_trade_dates": n_trade_dates,
        "contract": {
            "c18acc": "rrg-mono-swap-accel · executed legs · 3 slots",
            "abc": "abc-v3-f1-pullback · executed top-K · 5 slots",
            "note": "Phase0 only · no spec merge · no capital split yet",
        },
        "overlap": overlap,
        "correlation": {**daily_corr, **leg_corr},
        "verdict": verdict,
        "runtime_s": runtime_s,
    }


def build_phase0_from_comparison_json(path: str) -> dict[str, Any]:
    """Fast path · reuse comparison bundle ledgers + daily_nav."""
    payload = json.loads(open(path, encoding="utf-8").read())
    c18_rows = payload.get("c18acc_trade_ledger") or []
    abc_rows = payload.get("abc_f1_trade_ledger") or []
    c18_nav = (
        (payload.get("strategies") or {})
        .get("rrg-mono-swap-accel", {})
        .get("portfolio", {})
        .get("daily_nav")
        or []
    )
    abc_nav = (
        (payload.get("strategies") or {})
        .get("abc-v3-f1-pullback", {})
        .get("portfolio", {})
        .get("daily_nav")
        or []
    )
    return build_phase0_payload(
        date_start=str(payload.get("date_start") or ""),
        date_end=str(payload.get("date_end") or ""),
        n_trade_dates=int(payload.get("n_trade_dates") or 0),
        c18_rows=c18_rows,
        abc_rows=abc_rows,
        c18_daily_nav=c18_nav,
        abc_daily_nav=abc_nav,
        abc_candidate_rows=None,
        source=f"comparison_json:{path}",
    )


def run_c18acc_abc_dual_sleeve_phase0(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str | None = None,
    include_abc_candidates: bool = True,
) -> dict[str, Any]:
    """Collect executed legs + optional ABC candidates · Phase0 diagnostics."""
    import time

    t0 = time.perf_counter()
    std = load_backtest_standard_config()
    notional = comparison_notional_ntd()
    cost_model = dict(std.cost_model)

    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    if len(trade_dates) < 5:
        raise ValueError(f"insufficient trade dates: {len(trade_dates)}")

    c18_legs, _, _, _ = build_c18acc_s2_executed_legs_for_timeline(
        conn,
        trade_dates,
        n_slots=C18ACC_SLOTS,
        capital_ntd=notional / C18ACC_SLOTS,
        use_close_timing=True,
    )
    c18_periods = [
        {
            "entry_date": str(lg["entry_date"]),
            "exit_date": str(lg["exit_date"]),
            "stock_id": str(lg["stock_id"]),
        }
        for lg in c18_legs
    ]
    c18_pf = _portfolio_block(
        conn,
        close,
        trade_dates,
        c18_periods,
        total_capital=notional,
        n_slots=C18ACC_SLOTS,
        cost_model=cost_model,
    )

    abc_candidate_rows: list[dict[str, Any]] | None = None
    frames, trajectories, panel_meta, stock_maps, bench = _build_abc_v3_intraday_panel(
        conn, dates=trade_dates
    )
    f1_cands, _, _ = collect_abc_v3_skip0900_period_candidates(
        conn,
        dates=trade_dates,
        matcher=matches_w3_rv_hl_abc_v3_f1,
        frames=frames,
        trajectories=trajectories,
        meta=panel_meta,
        stock_maps=stock_maps,
        bench=bench,
    )
    if include_abc_candidates:
        abc_candidate_rows = f1_cands
    f1_scored = apply_score_spec(f1_cands, spec_id="v0")
    f1_ordered = order_periods_for_fill(f1_scored, fill_mode="topk_score")
    f1_executed = select_executed_slot_periods(
        conn,
        close,
        trade_dates,
        f1_ordered,
        total_capital=notional,
        n_slots=ABC_V3_SLOT_CHAMPION,
        cost_model=cost_model,
    )
    abc_periods = [
        {
            "entry_date": str(r["entry_date"]),
            "exit_date": str(r["exit_date"]),
            "stock_id": str(r["stock_id"]),
            "entry_px": r.get("entry_px"),
        }
        for r in f1_executed
    ]
    abc_pf = _portfolio_block(
        conn,
        close,
        trade_dates,
        abc_periods,
        total_capital=notional,
        n_slots=ABC_V3_SLOT_CHAMPION,
        cost_model=cost_model,
    )

    c18_ledger = [
        {
            "entry_date": lg.get("entry_date"),
            "stock_id": lg.get("stock_id"),
            "return_pct": lg.get("return_pct"),
        }
        for lg in c18_legs
    ]
    abc_ledger = [
        {
            "entry_date": r.get("entry_date"),
            "stock_id": r.get("stock_id"),
            "net_pct": r.get("net_pct"),
        }
        for r in f1_executed
    ]

    return build_phase0_payload(
        date_start=trade_dates[0],
        date_end=trade_dates[-1],
        n_trade_dates=len(trade_dates),
        c18_rows=c18_ledger,
        abc_rows=abc_ledger,
        c18_daily_nav=c18_pf.get("daily_nav") or [],
        abc_daily_nav=abc_pf.get("daily_nav") or [],
        abc_candidate_rows=abc_candidate_rows,
        source="live",
        runtime_s=round(time.perf_counter() - t0, 1),
    )


def render_c18acc_abc_dual_sleeve_phase0_md(payload: dict[str, Any]) -> str:
    ov = payload.get("overlap", {})
    corr = payload.get("correlation", {})
    v = payload.get("verdict", {})
    go = v.get("go_phase1_dual_sleeve")
    lines = [
        "# C18acc × ABC v3+f1 · dual-sleeve Phase0",
        "",
        "> 重疊 + 相關診斷 · **不修改** strategy 規格 · 僅判斷是否值得 Phase1 資金切袖。",
        "",
        f"- Span: **{payload.get('date_start')} → {payload.get('date_end')}** "
        f"({payload.get('n_trade_dates')} td)",
        f"- Source: `{payload.get('source')}`",
        "",
        "## Verdict",
        "",
        f"- {v.get('summary', '')}",
        "",
        "## Entry overlap（executed · stock-day）",
        "",
        "| metric | value |",
        "|--------|------:|",
        f"| C18 executed pairs | {ov.get('c18_executed_pairs', '—')} |",
        f"| ABC executed pairs | {ov.get('abc_executed_pairs', '—')} |",
        f"| Union | {ov.get('union_executed_pairs', '—')} |",
        f"| Both (conflict) | {ov.get('both_executed_pairs', '—')} |",
        f"| C18-only | {ov.get('c18_only_pairs', '—')} |",
        f"| ABC-only | {ov.get('abc_only_pairs', '—')} |",
        f"| **Jaccard (executed)** | **{ov.get('entry_jaccard_executed', '—')}** |",
        f"| Same-day stock rate | {ov.get('same_day_stock_rate_executed', '—')} |",
        f"| Mean daily Jaccard | {ov.get('mean_daily_jaccard_executed', '—')} |",
        f"| Days both sleeves enter | {ov.get('days_with_both_sleeves', '—')} / "
        f"{ov.get('days_with_any_entry', '—')} |",
        "",
        "## ABC on C18 underfill days",
        "",
        f"- C18 underfill days (<{C18ACC_SLOTS} entries): **{ov.get('c18_underfill_days', '—')}**",
        f"- ABC executed on those days: **{ov.get('abc_executed_on_c18_underfill_days', '—')}**",
        f"- ABC candidates on those days: **{ov.get('abc_candidates_on_c18_underfill_days', '—')}**",
    ]
    if ov.get("abc_candidate_pairs") is not None:
        lines.extend(
            [
                "",
                "## ABC candidates vs C18 executed",
                "",
                f"- ABC candidate pairs: **{ov.get('abc_candidate_pairs')}**",
                f"- Jaccard (C18 exec vs ABC cand): **{ov.get('entry_jaccard_c18_vs_abc_candidates', '—')}**",
                f"- Overlap pairs: **{ov.get('both_c18_abc_candidate_pairs', '—')}**",
            ]
        )
    lines.extend(
        [
            "",
            "## Correlation",
            "",
            f"- Daily portfolio return corr: **{corr.get('daily_return_corr', '—')}** "
            f"(n={corr.get('n_aligned_days', '—')})",
            f"- Overlap leg return corr: **{corr.get('leg_return_corr', '—')}** "
            f"(n={corr.get('n_overlap_legs', '—')})",
            "",
            "## Gates",
            "",
            f"- entry_jaccard ≤ {v.get('thresholds', {}).get('max_entry_jaccard', '—')}: "
            f"{'pass' if v.get('checks', {}).get('entry_jaccard_ok') else 'fail'}",
            f"- same_day_conflict ≤ {v.get('thresholds', {}).get('max_same_day_stock_rate', '—')}: "
            f"{'pass' if v.get('checks', {}).get('same_day_conflict_ok') else 'fail'}",
            f"- |daily_corr| ≤ {v.get('thresholds', {}).get('max_daily_return_corr', '—')}: "
            f"{'pass' if v.get('checks', {}).get('daily_return_corr_ok') else 'fail'}",
            "",
            "## Next",
            "",
            "- **GO** → Phase1 固定權重雙袖 NAV（例 70/30 · 不混篩選器）",
            "- **NO_GO** → 維持 C18acc 單袖 · ABC 僅 event observe",
            "",
        ]
    )
    return "\n".join(lines)
