"""ABC v3 / v3+f1 × RH50 regime entry gate · slot backtest overlay.

R0 = ABC v3 (or v3+f1) entry · 5-slot top-K.
+RH50 = same, but only enter on sessions where T-1 universe rotation_health ≥ 50%.
Leg-level excess is computed on slot-executed legs (faithful to portfolio fill).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Literal

from research.backtest.archive.abc_v3_slot_sweep import (
    ABC_V3_SLOT_CHAMPION,
    CAPITAL_PER_SLOT_NTD,
    _build_abc_v3_intraday_panel,
    apply_score_spec,
    collect_abc_v3_skip0900_period_candidates,
    matches_w3_rv_hl_abc_v3,
    order_periods_for_fill,
)
from research.backtest.triple_wma_pullback_sweep import matches_w3_rv_hl_abc_v3_f1
from research.backtest.archive.c18acc_abc_v3_f1_comparison import select_executed_slot_periods
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.archive.rrg_regime_four_axis_annot import IS_END, IS_START, OOS_START
from research.backtest.archive.rrg_regime_four_axis_phase3 import (
    RH50_THRESHOLD_PCT,
    build_rh50_session_flags,
    build_rotation_health_by_date,
)
from research.backtest.slot_portfolio_metrics import simulate_slot_portfolio

OOS_END_DEFAULT = "2026-06-30"
OOS_MIN_LEGS = 20
OOS_MAX_DELTA_EXCESS_PP = -0.2
MIN_CAPTURE_RATE = 0.25
MatcherMode = Literal["v3", "f1"]

_MATCHER_CONFIG: dict[MatcherMode, dict[str, Any]] = {
    "v3": {
        "matcher_id": "w3_rv_hl_abc_v3",
        "matcher_note": "ABC v3 base · no H-ABC-FILTER-1 (f1)",
        "matcher_fn": matches_w3_rv_hl_abc_v3,
        "gated_variant_id": "ABC-V3+RH50",
        "hypothesis_id": "P3-ABC-RH50-OOS",
        "hypothesis_statement": (
            "ABC v3 base (no f1) · RH50 entry gate · OOS Δ mean_excess vs R0 ≥ −0.2pp"
        ),
    },
    "f1": {
        "matcher_id": "w3_rv_hl_abc_v3_f1",
        "matcher_note": "ABC v3+f1 · H-ABC-FILTER-1 embedded",
        "matcher_fn": matches_w3_rv_hl_abc_v3_f1,
        "gated_variant_id": "ABC-V3-F1+RH50",
        "hypothesis_id": "P3-ABC-F1-RH50-OOS",
        "hypothesis_statement": (
            "ABC v3+f1 · RH50 entry gate · OOS Δ mean_excess vs R0 ≥ −0.2pp"
        ),
    },
}


def _matcher_config(mode: MatcherMode) -> dict[str, Any]:
    return _MATCHER_CONFIG[mode]


def _finite(values: list[Any]) -> list[float]:
    out: list[float] = []
    for v in values:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == f:
            out.append(f)
    return out


def _delta_pp(a: Any, b: Any) -> float | None:
    if a is None or b is None:
        return None
    return round(float(a) - float(b), 4)


def _leg_stats(executed: list[dict[str, Any]]) -> dict[str, Any]:
    nets = _finite([r.get("net_pct") for r in executed])
    exs = _finite([r.get("excess_pct") for r in executed])
    return {
        "n_legs": len(executed),
        "mean_net_pct": round(sum(nets) / len(nets), 4) if nets else None,
        "mean_excess_pct": round(sum(exs) / len(exs), 4) if exs else None,
        "win_rate_pct": round(100.0 * sum(1 for x in nets if x > 0) / len(nets), 2) if nets else None,
    }


def _run_window(
    conn: sqlite3.Connection,
    *,
    candidates: list[dict[str, Any]],
    close: Any,
    full_dates: list[str],
    date_start: str,
    date_end: str,
    n_slots: int,
) -> dict[str, Any]:
    win_cands = [c for c in candidates if date_start <= str(c["entry_date"]) <= date_end]
    ordered = order_periods_for_fill(win_cands, fill_mode="topk_score")
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    if not trade_dates:
        return {"error": "no_trade_dates", "n_signals": len(win_cands)}
    total_capital = CAPITAL_PER_SLOT_NTD * n_slots
    executed = select_executed_slot_periods(
        conn, close, trade_dates, ordered, total_capital=total_capital, n_slots=n_slots
    )
    periods = [
        {
            "entry_date": str(r["entry_date"]),
            "exit_date": str(r["exit_date"]),
            "stock_id": str(r["stock_id"]),
            "entry_px": r.get("entry_px"),
        }
        for r in executed
    ]
    pf = simulate_slot_portfolio(
        conn, close, trade_dates, periods, total_capital=total_capital, n_slots=n_slots
    )
    return {
        "date_start": date_start,
        "date_end": date_end,
        "n_signals": len(win_cands),
        "leg_stats": _leg_stats(executed),
        "portfolio": {
            "n_trades": pf.get("n_trades"),
            "total_return_pct": pf.get("total_return_pct"),
            "cagr_pct": pf.get("cagr_pct"),
            "mean_daily_return_pct": pf.get("mean_daily_return_pct"),
            "ann_mean_return_pct": pf.get("ann_mean_return_pct"),
            "sharpe_ratio": pf.get("sharpe_ratio"),
            "max_drawdown_pct": pf.get("max_drawdown_pct"),
            "avg_utilization_pct": pf.get("avg_utilization_pct"),
        },
    }


def _evaluate(base: dict[str, Any], gated: dict[str, Any]) -> dict[str, Any]:
    base_full_n = int((base.get("FULL", {}).get("leg_stats") or {}).get("n_legs") or 0)
    gated_full_n = int((gated.get("FULL", {}).get("leg_stats") or {}).get("n_legs") or 0)
    capture = round(gated_full_n / base_full_n, 4) if base_full_n > 0 else None

    base_oos = (base.get("OOS", {}).get("leg_stats") or {})
    gated_oos = (gated.get("OOS", {}).get("leg_stats") or {})
    oos_n = int(gated_oos.get("n_legs") or 0)
    oos_delta_ex = _delta_pp(gated_oos.get("mean_excess_pct"), base_oos.get("mean_excess_pct"))

    base_full_ls = base.get("FULL", {}).get("leg_stats") or {}
    gated_full_ls = gated.get("FULL", {}).get("leg_stats") or {}
    full_delta_ex = _delta_pp(gated_full_ls.get("mean_excess_pct"), base_full_ls.get("mean_excess_pct"))

    base_oos_pf = base.get("OOS", {}).get("portfolio") or {}
    gated_oos_pf = gated.get("OOS", {}).get("portfolio") or {}
    oos_delta_daily = _delta_pp(
        gated_oos_pf.get("mean_daily_return_pct"), base_oos_pf.get("mean_daily_return_pct")
    )

    passed = (
        capture is not None
        and capture >= MIN_CAPTURE_RATE
        and oos_n >= OOS_MIN_LEGS
        and oos_delta_ex is not None
        and float(oos_delta_ex) >= OOS_MAX_DELTA_EXCESS_PP
    )
    return {
        "capture_rate_full": capture,
        "capture_rate_full_pct": round(capture * 100, 2) if capture is not None else None,
        "FULL_delta_excess_pp": full_delta_ex,
        "OOS_delta_excess_pp": oos_delta_ex,
        "OOS_delta_daily_pp": oos_delta_daily,
        "OOS_n_legs": oos_n,
        "pass": passed,
    }


def run_abc_v3_rh50_gate(
    conn: sqlite3.Connection,
    *,
    date_start: str = IS_START,
    date_end: str | None = None,
    is_end: str = IS_END,
    oos_start: str = OOS_START,
    oos_end: str = OOS_END_DEFAULT,
    n_slots: int = ABC_V3_SLOT_CHAMPION,
    score_spec: str = "v0",
    rh50_threshold_pct: float = RH50_THRESHOLD_PCT,
    matcher_mode: MatcherMode = "v3",
    rh_wma_length: int = 20,
    candidates: list[dict[str, Any]] | None = None,
    close: Any | None = None,
    full_dates: list[str] | None = None,
    dates: list[str] | None = None,
) -> dict[str, Any]:
    """ABC v3 or v3+f1 · R0 vs +RH50 entry gate · IS/OOS/FULL.

    ``rh_wma_length`` controls the RRG WMA window for rotation_health.
    Official gate uses 20; shorter lengths (e.g. 5) are research-only.
    Pass prebuilt ``candidates``/``close``/``dates`` to reuse an intraday panel.
    """
    cfg = _matcher_config(matcher_mode)
    match_fn: Callable[[dict[str, Any]], bool] = cfg["matcher_fn"]
    gated_variant_id: str = cfg["gated_variant_id"]
    if rh_wma_length != 20:
        gated_variant_id = f"{gated_variant_id}-W{rh_wma_length}"

    if close is None or full_dates is None:
        close, _, _ = load_price_panels(conn)
        full_dates = close.index.astype(str).tolist()
    assert close is not None and full_dates is not None
    if date_end is None or date_end > full_dates[-1]:
        date_end = full_dates[-1]
    if dates is None:
        dates = [d for d in full_dates if date_start <= d <= date_end]
    assert dates is not None

    if candidates is None:
        frames, trajectories, panel_meta, stock_maps, bench = _build_abc_v3_intraday_panel(
            conn, dates=dates
        )
        candidates, _, _ = collect_abc_v3_skip0900_period_candidates(
            conn,
            dates=dates,
            matcher=match_fn,
            frames=frames,
            trajectories=trajectories,
            meta=panel_meta,
            stock_maps=stock_maps,
            bench=bench,
        )
        candidates = apply_score_spec(candidates, spec_id=score_spec)

    rh_by_date = build_rotation_health_by_date(conn, length=rh_wma_length)
    flags = build_rh50_session_flags(dates, full_dates, rh_by_date, threshold_pct=rh50_threshold_pct)
    rh50_candidates = [
        c for c in candidates if (flags.get(str(c["entry_date"])) or {}).get("rh50_pass")
    ]
    n_flagged_days = sum(1 for f in flags.values() if f.get("rh50_pass"))

    windows: list[tuple[str, str, str]] = [
        ("IS", date_start, min(is_end, date_end)),
        ("OOS", oos_start, min(oos_end, date_end)),
        ("FULL", date_start, date_end),
    ]

    base: dict[str, Any] = {}
    gated: dict[str, Any] = {}
    for win_label, w_start, w_end in windows:
        if w_start > w_end:
            continue
        base[win_label] = _run_window(
            conn,
            candidates=candidates,
            close=close,
            full_dates=full_dates,
            date_start=w_start,
            date_end=w_end,
            n_slots=n_slots,
        )
        gated[win_label] = _run_window(
            conn,
            candidates=rh50_candidates,
            close=close,
            full_dates=full_dates,
            date_start=w_start,
            date_end=w_end,
            n_slots=n_slots,
        )

    eval_row = _evaluate(base, gated)
    winners = (
        [{"variant_id": gated_variant_id, "track": "abc_v3_entry_gate", **eval_row}]
        if eval_row.get("pass")
        else []
    )

    return {
        "schema": "abc_v3_rh50_gate-v1",
        "topic": "rrg-regime-four-axis-annot",
        "phase": "phase3_abc_v3_entry_gate",
        "matcher_mode": matcher_mode,
        "rh_wma_length": rh_wma_length,
        "date_start": date_start,
        "date_end": date_end,
        "is_span": f"{date_start}..{min(is_end, date_end)}",
        "oos_span": f"{oos_start}..{min(oos_end, date_end)}",
        "matcher": cfg["matcher_id"],
        "matcher_note": cfg["matcher_note"],
        "score_spec": score_spec,
        "n_slots": n_slots,
        "hold_days": 3,
        "n_candidates_base": len(candidates),
        "n_candidates_rh50": len(rh50_candidates),
        "gate": {
            "rule": (
                f"T-1 universe rotation_health_pct(WMA{rh_wma_length}) >= {rh50_threshold_pct}"
            ),
            "rh_wma_length": rh_wma_length,
            "pit": "regime_t1_as_of = prior TW session",
            "entry_only": True,
            "flagged_session_days": n_flagged_days,
            "total_session_days": len(dates),
            "allowed_share_pct": round(100.0 * n_flagged_days / len(dates), 2) if dates else None,
        },
        "variants": {"R0": base, gated_variant_id: gated},
        "evaluation": {
            gated_variant_id: eval_row,
            "pass_rules": {
                "min_capture_rate": MIN_CAPTURE_RATE,
                "oos_min_legs": OOS_MIN_LEGS,
                "oos_max_delta_excess_pp": OOS_MAX_DELTA_EXCESS_PP,
            },
        },
        "hypotheses": {
            cfg["hypothesis_id"]: {
                "statement": (
                    f"{cfg['hypothesis_statement']} · RH WMA length={rh_wma_length}"
                ),
                "rh_wma_length": rh_wma_length,
                "OOS_delta_excess_pp": eval_row.get("OOS_delta_excess_pp"),
                "pass": eval_row.get("pass"),
            },
        },
        "n_passed": 1 if eval_row.get("pass") else 0,
        "winners": winners,
    }


def run_abc_v3_f1_rh50_wma_length_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str | None = None,
    is_end: str = IS_END,
    oos_start: str = OOS_START,
    oos_end: str = OOS_END_DEFAULT,
    n_slots: int = ABC_V3_SLOT_CHAMPION,
    score_spec: str = "v0",
    rh50_threshold_pct: float = RH50_THRESHOLD_PCT,
    lengths: tuple[int, ...] = (20, 5),
    candidates: list[dict[str, Any]] | None = None,
    rebuild_panel: bool = False,
) -> dict[str, Any]:
    """ABC v3+f1 × RH50 across RRG WMA lengths (research).

    Default: pass prebuilt candidates (e.g. from comparison ledger) · **no 1m panel**.
    ``rebuild_panel=True`` rebuilds dual-WMA intraday (slow · uses existing 1m kbar polls).
    """
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    if date_end is None or date_end > full_dates[-1]:
        date_end = full_dates[-1]
    dates = [d for d in full_dates if date_start <= d <= date_end]

    panel_meta: dict[str, Any] = {"source": "prebuilt_candidates"}
    if candidates is None:
        if not rebuild_panel:
            raise ValueError("candidates required unless rebuild_panel=True")
        frames, trajectories, panel_meta, stock_maps, bench = _build_abc_v3_intraday_panel(
            conn, dates=dates
        )
        candidates, _, _ = collect_abc_v3_skip0900_period_candidates(
            conn,
            dates=dates,
            matcher=matches_w3_rv_hl_abc_v3_f1,
            frames=frames,
            trajectories=trajectories,
            meta=panel_meta,
            stock_maps=stock_maps,
            bench=bench,
        )
        candidates = apply_score_spec(candidates, spec_id=score_spec)

    by_length: dict[str, Any] = {}
    for length in lengths:
        by_length[f"W{length}"] = run_abc_v3_rh50_gate(
            conn,
            date_start=date_start,
            date_end=date_end,
            is_end=is_end,
            oos_start=oos_start,
            oos_end=oos_end,
            n_slots=n_slots,
            score_spec=score_spec,
            rh50_threshold_pct=rh50_threshold_pct,
            matcher_mode="f1",
            rh_wma_length=length,
            candidates=candidates,
            close=close,
            full_dates=full_dates,
            dates=dates,
        )

    w20 = by_length.get("W20") or {}
    w5 = by_length.get("W5") or {}
    ev20 = (w20.get("evaluation") or {}).get("ABC-V3-F1+RH50") or {}
    ev5 = (w5.get("evaluation") or {}).get("ABC-V3-F1+RH50-W5") or {}

    return {
        "schema": "abc_v3_f1_rh50_wma_length_study-v1",
        "topic": "rrg-regime-four-axis-annot",
        "phase": "research_rh_wma5_vs_wma20",
        "matcher": "w3_rv_hl_abc_v3_f1",
        "candidate_source": panel_meta.get("source") or "rebuild_panel",
        "date_start": date_start,
        "date_end": date_end,
        "oos_span": f"{oos_start}..{min(oos_end, date_end)}",
        "n_candidates_base": len(candidates),
        "rh50_threshold_pct": rh50_threshold_pct,
        "lengths": list(lengths),
        "by_length": by_length,
        "compare": {
            "W20_pass": ev20.get("pass"),
            "W5_pass": ev5.get("pass"),
            "W20_OOS_delta_excess_pp": ev20.get("OOS_delta_excess_pp"),
            "W5_OOS_delta_excess_pp": ev5.get("OOS_delta_excess_pp"),
            "W20_capture_pct": ev20.get("capture_rate_full_pct"),
            "W5_capture_pct": ev5.get("capture_rate_full_pct"),
            "W20_allowed_share_pct": (w20.get("gate") or {}).get("allowed_share_pct"),
            "W5_allowed_share_pct": (w5.get("gate") or {}).get("allowed_share_pct"),
            "delta_oos_excess_w5_minus_w20_pp": _delta_pp(
                ev5.get("OOS_delta_excess_pp"), ev20.get("OOS_delta_excess_pp")
            ),
        },
        "panel_meta": panel_meta,
        "note": (
            "RH rotation_health is daily-close RRG (WMA length varies). "
            "No 1m rearward panel rebuild when candidates are supplied."
        ),
    }


def render_abc_v3_f1_rh50_wma_length_study_md(payload: dict[str, Any]) -> str:
    cmp_ = payload.get("compare") or {}
    lines = [
        "# ABC v3+f1 × RH50 · WMA length study (20 vs 5)",
        "",
        "> Research-only · official RH50 gate remains **WMA(20)**.",
        "> Same f1 candidates · T-1 rotation_health ≥ 50% · differ only RH RRG length.",
        f"> Candidate source: `{payload.get('candidate_source')}` · "
        f"{payload.get('note') or ''}",
        "",
        f"- Span: **{payload.get('date_start')} → {payload.get('date_end')}**",
        f"- OOS: {payload.get('oos_span')}",
        f"- Candidates (base R0): **{payload.get('n_candidates_base')}**",
        f"- Threshold: RH ≥ {payload.get('rh50_threshold_pct')}%",
        "",
        "## Verdict summary",
        "",
        "| RH WMA | pass | capture% | allowed sessions% | OOS Δexcess vs R0 |",
        "|--------|------|----------|-------------------|-------------------|",
    ]
    for key in ("W20", "W5"):
        block = (payload.get("by_length") or {}).get(key) or {}
        gate = block.get("gate") or {}
        vid = "ABC-V3-F1+RH50" if key == "W20" else "ABC-V3-F1+RH50-W5"
        ev = (block.get("evaluation") or {}).get(vid) or {}
        lines.append(
            f"| {key} | {ev.get('pass')} | {ev.get('capture_rate_full_pct')} | "
            f"{gate.get('allowed_share_pct')} | {ev.get('OOS_delta_excess_pp')} |"
        )
    lines += [
        "",
        f"- Δ OOS excess (W5 − W20 gate-deltas)：**{cmp_.get('delta_oos_excess_w5_minus_w20_pp')}** pp",
        "",
        "## Leg-level (slot-executed)",
        "",
        "| RH WMA | variant | window | n | mean net% | mean excess% | win% |",
        "|--------|---------|--------|---|-----------|--------------|------|",
    ]
    for key in ("W20", "W5"):
        block = (payload.get("by_length") or {}).get(key) or {}
        vid = "ABC-V3-F1+RH50" if key == "W20" else "ABC-V3-F1+RH50-W5"
        for vid_row in ("R0", vid):
            variant = (block.get("variants") or {}).get(vid_row) or {}
            for win in ("IS", "OOS", "FULL"):
                r = variant.get(win)
                if not r or r.get("error"):
                    continue
                ls = r.get("leg_stats") or {}
                lines.append(
                    f"| {key} | {vid_row} | {win} | {ls.get('n_legs')} | "
                    f"{ls.get('mean_net_pct')} | {ls.get('mean_excess_pct')} | "
                    f"{ls.get('win_rate_pct')} |"
                )
    lines += [
        "",
        "## Pass rules (same as official RH50)",
        "",
        f"- capture ≥ {MIN_CAPTURE_RATE * 100:.0f}% · OOS n ≥ {OOS_MIN_LEGS} · "
        f"OOS Δexcess ≥ {OOS_MAX_DELTA_EXCESS_PP}pp vs R0",
        "",
        "## Note",
        "",
        "- W5 is **exploratory** · do not replace W20 in strategy.yaml without OOS confirmation.",
        "",
    ]
    return "\n".join(lines)


def render_abc_v3_rh50_gate_md(payload: dict[str, Any]) -> str:
    gate = payload.get("gate", {})
    matcher_mode = str(payload.get("matcher_mode") or "v3")
    cfg = _matcher_config("f1" if matcher_mode == "f1" else "v3")
    rh_len = int(payload.get("rh_wma_length") or gate.get("rh_wma_length") or 20)
    gated_variant_id = cfg["gated_variant_id"]
    if rh_len != 20:
        gated_variant_id = f"{gated_variant_id}-W{rh_len}"
    # Fallback for older payloads keyed without length suffix
    ev = (payload.get("evaluation") or {}).get(gated_variant_id)
    if ev is None:
        ev = (payload.get("evaluation") or {}).get(cfg["gated_variant_id"]) or {}
    len_note = f" · RH WMA={rh_len}" if rh_len != 20 else ""
    title = (
        f"# ABC v3+f1 × RH50 regime entry gate{len_note}"
        if matcher_mode == "f1"
        else f"# ABC v3 base (no f1) × RH50 regime entry gate{len_note}"
    )
    r0_note = (
        "> R0 = ABC v3+f1 entry（matcher `w3_rv_hl_abc_v3_f1`）· 5 槽 top-K。"
        if matcher_mode == "f1"
        else "> R0 = ABC v3 base entry（matcher `w3_rv_hl_abc_v3`, 不含 f1 過濾）· 5 槽 top-K。"
    )
    lines = [
        title,
        "",
        r0_note,
        "> +RH50 = 僅在 T-1 universe rotation_health ≥ 50% 的交易日進場。",
        "",
        f"- Span: **{payload.get('date_start')} → {payload.get('date_end')}**",
        f"- IS: {payload.get('is_span')} · OOS: {payload.get('oos_span')}",
        f"- Matcher: **{payload.get('matcher')}** · hold {payload.get('hold_days')}d · "
        f"{payload.get('n_slots')} slots top-K ({payload.get('score_spec')})",
        f"- Gate: {gate.get('rule')} · allowed {gate.get('allowed_share_pct')}% sessions",
        f"- Candidates: base {payload.get('n_candidates_base')} → RH50 {payload.get('n_candidates_rh50')}",
        "",
        "## Verdict",
        "",
        f"- {gated_variant_id} pass=**{ev.get('pass')}** · capture={ev.get('capture_rate_full_pct')}% "
        f"· OOS Δexcess={ev.get('OOS_delta_excess_pp')}pp · OOS Δdaily={ev.get('OOS_delta_daily_pp')}pp "
        f"· OOS n={ev.get('OOS_n_legs')}",
        "",
        "## Leg-level (slot-executed)",
        "",
        "| variant | window | n legs | mean net% | mean excess% | win% |",
        "|---------|--------|--------|-----------|--------------|------|",
    ]
    for vid in ("R0", gated_variant_id):
        block = (payload.get("variants") or {}).get(vid, {})
        for win in ("IS", "OOS", "FULL"):
            r = block.get(win)
            if not r or r.get("error"):
                continue
            ls = r.get("leg_stats") or {}
            lines.append(
                f"| {vid} | {win} | {ls.get('n_legs')} | {ls.get('mean_net_pct')} | "
                f"{ls.get('mean_excess_pct')} | {ls.get('win_rate_pct')} |"
            )
    lines += [
        "",
        "## Portfolio (5-slot top-K)",
        "",
        "| variant | window | trades | daily% | ann% | CAGR% | Sharpe | maxDD% |",
        "|---------|--------|--------|--------|------|-------|--------|--------|",
    ]
    for vid in ("R0", gated_variant_id):
        block = (payload.get("variants") or {}).get(vid, {})
        for win in ("IS", "OOS", "FULL"):
            r = block.get(win)
            if not r or r.get("error"):
                continue
            pf = r.get("portfolio") or {}
            lines.append(
                f"| {vid} | {win} | {pf.get('n_trades')} | {pf.get('mean_daily_return_pct')} | "
                f"{pf.get('ann_mean_return_pct')} | {pf.get('cagr_pct')} | "
                f"{pf.get('sharpe_ratio')} | {pf.get('max_drawdown_pct')} |"
            )
    lines += [
        "",
        "## Pass rules",
        "",
        f"- capture ≥ {MIN_CAPTURE_RATE * 100:.0f}% · OOS n ≥ {OOS_MIN_LEGS} · "
        f"OOS Δexcess ≥ {OOS_MAX_DELTA_EXCESS_PP}pp vs R0",
    ]
    return "\n".join(lines) + "\n"
