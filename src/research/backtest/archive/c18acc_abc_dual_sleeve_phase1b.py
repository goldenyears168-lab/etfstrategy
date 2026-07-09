"""C18acc × ABC v3+f1 · Phase1b event-observe sleeve NAV (research only).

ABC sleeve uses Champion-validated event model:
  all F1 candidates · FIFO observe order · auto slots = peak concurrent holds.
C18 sleeve unchanged · 3-slot portfolio NAV.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from typing import Any

from backtest_standard_config import comparison_notional_ntd, load_backtest_standard_config
from research.backtest.archive.abc_v3_slot_sweep import (
    ABC_V3_HOLD_DAYS,
    _build_abc_v3_intraday_panel,
    _unconstrained_leg_stats,
    apply_score_spec,
    collect_abc_v3_skip0900_period_candidates,
    order_periods_for_fill,
)
from research.backtest.archive.c18acc_abc_dual_sleeve_phase1 import (
    DEFAULT_ABC_WEIGHT,
    DEFAULT_C18_WEIGHT,
    IS_END_DEFAULT,
    OOS_H1_END,
    OOS_H1_START,
    PHASE1_WEIGHT_GRID,
    build_phase1_payload,
    phase1_verdict,
)
from research.backtest.archive.c18acc_abc_v3_f1_comparison import (
    ABC_F1_PIPELINE_CHAMPION,
    _portfolio_block,
    select_executed_slot_periods,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.triple_wma_pullback_sweep import matches_w3_rv_hl_abc_v3_f1

PIPELINE_TAIL_START = "2026-02-25"  # abc_v3_f1_pipeline 90d OOS tail


def strip_abc_candidate_periods(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Minimal period fields for sidecar / comparison export."""
    out: list[dict[str, Any]] = []
    for row in candidates:
        out.append(
            {
                "stock_id": str(row.get("stock_id") or ""),
                "entry_date": str(row.get("entry_date") or ""),
                "exit_date": str(row.get("exit_date") or ""),
                "entry_px": row.get("entry_px"),
                "entry_frame_idx": row.get("entry_frame_idx"),
                "entry_poll_idx": row.get("entry_poll_idx"),
                "net_pct": row.get("net_pct"),
                "hold_days": row.get("hold_days"),
            }
        )
    return out


def peak_concurrent_slots(
    periods: list[dict[str, Any]],
    trade_dates: list[str],
) -> int:
    """Peak open positions · auto slot count for event-observe sleeve."""
    if not periods or not trade_dates:
        return max(ABC_V3_HOLD_DAYS, 1)
    peak = 0
    for d in trade_dates:
        n_open = sum(
            1
            for p in periods
            if str(p.get("entry_date") or "") <= d <= str(p.get("exit_date") or "")
        )
        peak = max(peak, n_open)
    return max(peak, ABC_V3_HOLD_DAYS, 1)


def _event_stats_on_dates(
    periods: list[dict[str, Any]],
    *,
    date_start: str,
    date_end: str,
    hold_days: int = ABC_V3_HOLD_DAYS,
) -> dict[str, Any]:
    subset = [
        p
        for p in periods
        if date_start <= str(p.get("entry_date") or "") <= date_end
    ]
    stats = _unconstrained_leg_stats(subset)
    stats["hold_days"] = hold_days
    stats["date_start"] = date_start
    stats["date_end"] = date_end
    return stats


def build_abc_event_observe_portfolio(
    conn: sqlite3.Connection,
    close,
    trade_dates: list[str],
    candidates: list[dict[str, Any]],
    *,
    total_capital: float,
    cost_model: dict | None = None,
    fill_mode: str = "fifo",
) -> dict[str, Any]:
    """Event-observe sleeve · all candidates · FIFO · auto peak slots."""
    ordered = order_periods_for_fill(candidates, fill_mode=fill_mode)  # type: ignore[arg-type]
    n_slots = peak_concurrent_slots(ordered, trade_dates)
    executed = select_executed_slot_periods(
        conn,
        close,
        trade_dates,
        ordered,
        total_capital=total_capital,
        n_slots=n_slots,
        cost_model=cost_model,
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
    pf = _portfolio_block(
        conn,
        close,
        trade_dates,
        periods,
        total_capital=total_capital,
        n_slots=n_slots,
        cost_model=cost_model,
    )
    event_all = _unconstrained_leg_stats(candidates)
    event_exec = _unconstrained_leg_stats(executed)
    capture = round(100.0 * len(executed) / len(candidates), 1) if candidates else None
    tail_stats = _event_stats_on_dates(
        candidates,
        date_start=PIPELINE_TAIL_START,
        date_end=trade_dates[-1],
    )
    return {
        **pf,
        "event_observe": {
            "mode": "event_observe_fifo",
            "fill_mode": fill_mode,
            "n_candidates": len(candidates),
            "n_slots_auto": n_slots,
            "n_executed": len(executed),
            "signal_capture_pct": capture,
            "event_all": event_all,
            "event_executed": event_exec,
            "pipeline_tail_event": tail_stats,
            "champion_reference": dict(ABC_F1_PIPELINE_CHAMPION),
        },
    }


def collect_abc_f1_candidates(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    kbar_bar_minutes: int | None = 5,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """ABC v3+f1 candidates · skip 09:00 · champion matcher."""
    t0 = time.perf_counter()
    frames, trajectories, panel_meta, stock_maps, bench = _build_abc_v3_intraday_panel(
        conn, dates=trade_dates, kbar_bar_minutes=kbar_bar_minutes
    )
    candidates, _, _ = collect_abc_v3_skip0900_period_candidates(
        conn,
        dates=trade_dates,
        matcher=matches_w3_rv_hl_abc_v3_f1,
        frames=frames,
        trajectories=trajectories,
        meta=panel_meta,
        stock_maps=stock_maps,
        bench=bench,
    )
    scored = apply_score_spec(candidates, spec_id="v0")
    meta = {
        "n_frames": len(frames),
        "n_trajectories": len(trajectories),
        "n_candidates": len(scored),
        "panel_runtime_s": round(time.perf_counter() - t0, 1),
        "kbar_bar_minutes": kbar_bar_minutes,
    }
    return scored, meta


def build_phase1b_payload(
    *,
    date_start: str,
    date_end: str,
    n_trade_dates: int,
    notional: float,
    c18_daily_nav: list[dict[str, Any]],
    abc_daily_nav: list[dict[str, Any]],
    c18_sleeve_metrics: dict[str, Any] | None,
    abc_sleeve_metrics: dict[str, Any] | None,
    event_level: dict[str, Any] | None,
    trade_dates: list[str],
    abc_event_observe: dict[str, Any] | None = None,
    candidate_meta: dict[str, Any] | None = None,
    weight_grid: tuple[tuple[float, float], ...] = PHASE1_WEIGHT_GRID,
    source: str = "live",
    runtime_s: float | None = None,
) -> dict[str, Any]:
    payload = build_phase1_payload(
        date_start=date_start,
        date_end=date_end,
        n_trade_dates=n_trade_dates,
        notional=notional,
        c18_daily_nav=c18_daily_nav,
        abc_daily_nav=abc_daily_nav,
        c18_sleeve_metrics=c18_sleeve_metrics,
        abc_sleeve_metrics=abc_sleeve_metrics,
        event_level=event_level,
        trade_dates=trade_dates,
        weight_grid=weight_grid,
        source=source,
        runtime_s=runtime_s,
    )
    payload["schema"] = "c18acc_abc_dual_sleeve_phase1b-v1"
    payload["phase"] = "phase1b_event_observe_nav"
    contract = dict(payload.get("contract") or {})
    contract.update(
        {
            "abc_sleeve_nav_layer": "event_observe_fifo",
            "abc_adopted_edge_layer": "event_level_3d",
            "champion_mean_ret_3d_net_pct": ABC_F1_PIPELINE_CHAMPION["mean_ret_3d_net_pct"],
            "kbar_bar_minutes": (candidate_meta or {}).get("kbar_bar_minutes", 5),
            "note": "ABC event-observe 子帳 · FIFO · auto slots · C18 3-slot portfolio",
        }
    )
    payload["contract"] = contract
    payload["abc_event_observe"] = abc_event_observe
    payload["candidate_meta"] = candidate_meta
    if candidate_meta and candidate_meta.get("periods"):
        payload["abc_f1_candidate_periods"] = candidate_meta["periods"]
    payload["phase1_reference"] = {
        "phase": "phase1_partitioned_nav",
        "abc_sleeve": "portfolio_5slot_topk",
        "verdict_note": "Phase1 used failed 5-slot portfolio · Phase1b uses event-observe",
    }
    return payload


def build_phase1b_from_comparison_json(
    path: str,
    conn: sqlite3.Connection,
    *,
    abc_periods_json: str | None = None,
    kbar_bar_minutes: int | None = 5,
    weight_grid: tuple[tuple[float, float], ...] = PHASE1_WEIGHT_GRID,
) -> dict[str, Any]:
    """Hybrid fast path · C18 NAV from comparison · ABC event sleeve live or sidecar."""
    t0 = time.perf_counter()
    payload = json.loads(open(path, encoding="utf-8").read())
    notional = float((payload.get("comparison_contract") or {}).get("notional_ntd") or 100_000)
    strategies = payload.get("strategies") or {}
    c18_nav = (strategies.get("rrg-mono-swap-accel") or {}).get("portfolio", {}).get("daily_nav") or []
    trade_dates = sorted({str(r["date"]) for r in c18_nav})
    date_start = str(payload.get("date_start") or trade_dates[0])
    date_end = str(payload.get("date_end") or trade_dates[-1])

    std = load_backtest_standard_config()
    cost_model = dict(std.cost_model)
    close, _, _ = load_price_panels(conn)
    panel_dates = [d for d in close.index.astype(str).tolist() if date_start <= d <= date_end]
    if not panel_dates:
        panel_dates = trade_dates

    candidates: list[dict[str, Any]] = []
    cand_meta: dict[str, Any] = {}
    if abc_periods_json:
        sidecar = json.loads(open(abc_periods_json, encoding="utf-8").read())
        candidates = sidecar.get("periods") or sidecar
        cand_meta = {"source": f"abc_periods_json:{abc_periods_json}", "n_candidates": len(candidates)}
    elif payload.get("abc_f1_candidate_periods"):
        candidates = payload["abc_f1_candidate_periods"]
        cand_meta = {"source": "comparison_json:abc_f1_candidate_periods", "n_candidates": len(candidates)}
    else:
        candidates, cand_meta = collect_abc_f1_candidates(
            conn, panel_dates, kbar_bar_minutes=kbar_bar_minutes
        )
    cand_meta = {**cand_meta, "periods": strip_abc_candidate_periods(candidates)}

    abc_pf = build_abc_event_observe_portfolio(
        conn,
        close,
        trade_dates,
        candidates,
        total_capital=notional,
        cost_model=cost_model,
    )
    abc_nav = abc_pf.get("daily_nav") or []

    return build_phase1b_payload(
        date_start=date_start,
        date_end=date_end,
        n_trade_dates=int(payload.get("n_trade_dates") or len(trade_dates)),
        notional=notional,
        c18_daily_nav=c18_nav,
        abc_daily_nav=abc_nav,
        c18_sleeve_metrics=strategies.get("rrg-mono-swap-accel", {}).get("portfolio"),
        abc_sleeve_metrics=abc_pf,
        event_level=payload.get("event_level"),
        trade_dates=trade_dates,
        abc_event_observe=abc_pf.get("event_observe"),
        weight_grid=weight_grid,
        source=f"hybrid:{path}",
        runtime_s=round(time.perf_counter() - t0, 1),
        candidate_meta=cand_meta,
    )


def run_c18acc_abc_dual_sleeve_phase1b(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str | None = None,
    kbar_bar_minutes: int | None = 5,
    weight_grid: tuple[tuple[float, float], ...] | None = None,
) -> dict[str, Any]:
    """Full live Phase1b · C18 3-slot + ABC event-observe sleeve."""
    t0 = time.perf_counter()
    std = load_backtest_standard_config()
    notional = comparison_notional_ntd()
    cost_model = dict(std.cost_model)

    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]

    from research.backtest.archive.c18acc_abc_v3_f1_comparison import (
        C18ACC_SLOTS,
        _periods_from_legs,
        enrich_event_level,
    )
    from research.backtest.rrg_mono_score_swap_c import build_c18acc_s2_executed_legs_for_timeline

    c18_legs, _, _, _ = build_c18acc_s2_executed_legs_for_timeline(
        conn,
        trade_dates,
        n_slots=C18ACC_SLOTS,
        capital_ntd=notional / C18ACC_SLOTS,
        use_close_timing=True,
    )
    c18_pf = _portfolio_block(
        conn,
        close,
        trade_dates,
        _periods_from_legs(c18_legs),
        total_capital=notional,
        n_slots=C18ACC_SLOTS,
        cost_model=cost_model,
    )
    c18_nav = c18_pf.get("daily_nav") or []
    c18_metrics = c18_pf

    candidates, cand_meta = collect_abc_f1_candidates(
        conn, trade_dates, kbar_bar_minutes=kbar_bar_minutes
    )
    cand_meta = {**cand_meta, "periods": strip_abc_candidate_periods(candidates)}
    abc_pf = build_abc_event_observe_portfolio(
        conn,
        close,
        trade_dates,
        candidates,
        total_capital=notional,
        cost_model=cost_model,
    )

    grid = weight_grid or PHASE1_WEIGHT_GRID
    event_level = enrich_event_level({"abc_f1_trade_ledger": [], "strategies": {}}).get(
        "event_level"
    )
    payload = build_phase1b_payload(
        date_start=trade_dates[0],
        date_end=trade_dates[-1],
        n_trade_dates=len(trade_dates),
        notional=notional,
        c18_daily_nav=c18_nav,
        abc_daily_nav=abc_pf.get("daily_nav") or [],
        c18_sleeve_metrics=c18_metrics,
        abc_sleeve_metrics=abc_pf,
        event_level=event_level,
        trade_dates=trade_dates,
        abc_event_observe=abc_pf.get("event_observe"),
        weight_grid=grid,
        source="live_event_observe",
        runtime_s=round(time.perf_counter() - t0, 1),
        candidate_meta=cand_meta,
    )
    return payload


def render_c18acc_abc_dual_sleeve_phase1b_md(payload: dict[str, Any]) -> str:
    from research.backtest.archive.c18acc_abc_dual_sleeve_phase1 import (
        render_c18acc_abc_dual_sleeve_phase1_md,
    )

    base = render_c18acc_abc_dual_sleeve_phase1_md(payload)
    base = base.replace(
        "# C18acc × ABC v3+f1 · Phase1 fixed-weight NAV",
        "# C18acc × ABC v3+f1 · Phase1b event-observe NAV",
    )
    base = base.replace(
        "### ABC v3+f1 only · 5-slot NAV",
        "### ABC v3+f1 only · event-observe FIFO",
    )
    base = base.replace(
        "## Verdict（組合 NAV 門檻 · vs C18 portfolio）",
        "## Verdict（event-observe ABC · vs C18 portfolio）",
    )
    ev_obs = payload.get("abc_event_observe") or {}
    champ = ev_obs.get("champion_reference") or ABC_F1_PIPELINE_CHAMPION
    event_all = ev_obs.get("event_all") or {}
    event_exec = ev_obs.get("event_executed") or {}
    tail = ev_obs.get("pipeline_tail_event") or {}
    phase1_ref = payload.get("phase1_reference") or {}

    extra = [
        "",
        "---",
        "",
        "## Phase1b · ABC event-observe sleeve",
        "",
        f"- Mode: **`{ev_obs.get('mode', 'event_observe_fifo')}`** · fill `{ev_obs.get('fill_mode', 'fifo')}`",
        f"- Auto slots: **{ev_obs.get('n_slots_auto', '—')}** · candidates **{ev_obs.get('n_candidates', '—')}** "
        f"→ executed **{ev_obs.get('n_executed', '—')}** · capture **{ev_obs.get('signal_capture_pct', '—')}%**",
        "",
        "### Champion anchor（採納 SSOT）",
        "",
        f"| 口徑 | n | 每筆 3d % | 勝率 % |",
        f"|------|---|----------|--------|",
        f"| **Pipeline OOS champion** | **{champ.get('n')}** | **{champ.get('mean_ret_3d_net_pct')}** | "
        f"**{champ.get('win_rate_pct')}** |",
        f"| Phase1b · event executed | {event_exec.get('n', '—')} | "
        f"**{event_exec.get('mean_ret_net_pct', '—')}** | {event_exec.get('win_rate_pct', '—')} |",
        f"| Phase1b · pipeline tail ({PIPELINE_TAIL_START}+) | {tail.get('n', '—')} | "
        f"**{tail.get('mean_ret_net_pct', '—')}** | {tail.get('win_rate_pct', '—')} |",
        f"| All candidates (unconstrained) | {event_all.get('n', '—')} | "
        f"{event_all.get('mean_ret_net_pct', '—')} | {event_all.get('win_rate_pct', '—')} |",
        "",
        f"Phase1 對照：{phase1_ref.get('abc_sleeve', 'portfolio_5slot')} · "
        "Phase1b 用 event-observe 公平測 dual-sleeve。",
        "",
    ]
    # Insert after title block · before event layer section in base md
    marker = "## ABC v3+f1 · 事件層"
    if marker in base:
        head, tail_md = base.split(marker, 1)
        return head + "\n".join(extra) + "\n" + marker + tail_md
    return base + "\n".join(extra)
