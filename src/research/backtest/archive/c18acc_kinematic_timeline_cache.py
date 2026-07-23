"""C18acc · triple WMA kinematic timeline cache (relative MV/RV structure)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

import pandas as pd

from market_benchmark import load_benchmark_close
from research.backtest.archive.c18acc_hold3d_event_study import (
    _prepare_c18acc_context,
    c18acc_live_s2_config,
)
from research.backtest.archive.c18acc_s2_dual_wma_annot import (
    _collect_annot_dates,
    _exit_frame_idx_for_date,
)
from research.backtest.archive.c18acc_triple_wma_extreme_profile import (
    _frame_idx_for_minute,
)
from research.backtest.dual_wma_signal_backtest import _build_stock_frame_maps, _price_at
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_score_swap_c import simulate_score_swap_c
from research.backtest.triple_wma_pullback_sweep import _mv, _q, _rv
from rrg_universe_intraday_panel import (
    WMA_MICRO_LENGTH,
    build_dual_wma_intraday_trajectories,
    intraday_poll_minutes,
)


def _safe_gap(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return round(a - b, 4)


def _mv_order(mv: dict[str, float | None]) -> str:
    legs = [(k, v) for k, v in mv.items() if v is not None]
    if len(legs) < 2:
        return ""
    legs.sort(key=lambda x: x[1], reverse=True)
    return ">".join(k for k, _ in legs)


def _mv_ranks(mv: dict[str, float | None]) -> dict[str, int | None]:
    legs = [(k, v) for k, v in mv.items() if v is not None]
    out: dict[str, int | None] = {k: None for k in mv}
    if not legs:
        return out
    legs.sort(key=lambda x: x[1], reverse=True)
    for rank, (k, _) in enumerate(legs, start=1):
        out[k] = rank
    return out


@dataclass(frozen=True)
class KinematicSnap:
    frame_idx: int
    trade_date: str
    minute: str
    px: float
    ret_from_entry_pct: float
    w20_rv: float | None
    w20_mv: float | None
    w20_quad: str | None
    w5_rv: float | None
    w5_mv: float | None
    w5_quad: str | None
    w3_rv: float | None
    w3_mv: float | None
    w3_quad: str | None
    spread_class: str | None
    spread_dist: float | None
    gap_mv_w20_w5: float | None
    gap_mv_w5_w3: float | None
    gap_mv_w20_w3: float | None
    gap_rv_w20_w5: float | None
    gap_rv_w5_w3: float | None
    gap_rv_w20_w3: float | None
    gap_rv_w5_w20: float | None
    dmv_w20: float | None
    dmv_w5: float | None
    dmv_w3: float | None
    drv_w20: float | None
    drv_w5: float | None
    drv_w3: float | None
    mv_order: str
    mv_rank_w20: int | None
    mv_rank_w5: int | None
    mv_rank_w3: int | None
    w20_mv_decline_streak: int
    w5_mv_decline_streak: int
    w3_mv_decline_streak: int
    parallel_3leg_dmv_deepening: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_idx": self.frame_idx,
            "trade_date": self.trade_date,
            "minute": self.minute,
            "px": round(self.px, 4),
            "ret_from_entry_pct": round(self.ret_from_entry_pct, 3),
            "w20_rv": self.w20_rv,
            "w20_mv": self.w20_mv,
            "w20_quad": self.w20_quad,
            "w5_rv": self.w5_rv,
            "w5_mv": self.w5_mv,
            "w5_quad": self.w5_quad,
            "w3_rv": self.w3_rv,
            "w3_mv": self.w3_mv,
            "w3_quad": self.w3_quad,
            "spread_class": self.spread_class,
            "spread_dist": self.spread_dist,
            "gap_mv_w20_w5": self.gap_mv_w20_w5,
            "gap_mv_w5_w3": self.gap_mv_w5_w3,
            "gap_mv_w20_w3": self.gap_mv_w20_w3,
            "gap_rv_w20_w5": self.gap_rv_w20_w5,
            "gap_rv_w5_w3": self.gap_rv_w5_w3,
            "gap_rv_w20_w3": self.gap_rv_w20_w3,
            "gap_rv_w5_w20": self.gap_rv_w5_w20,
            "dmv_w20": self.dmv_w20,
            "dmv_w5": self.dmv_w5,
            "dmv_w3": self.dmv_w3,
            "drv_w20": self.drv_w20,
            "drv_w5": self.drv_w5,
            "drv_w3": self.drv_w3,
            "mv_order": self.mv_order,
            "mv_rank_w20": self.mv_rank_w20,
            "mv_rank_w5": self.mv_rank_w5,
            "mv_rank_w3": self.mv_rank_w3,
            "w20_mv_decline_streak": self.w20_mv_decline_streak,
            "w5_mv_decline_streak": self.w5_mv_decline_streak,
            "w3_mv_decline_streak": self.w3_mv_decline_streak,
            "parallel_3leg_dmv_deepening": self.parallel_3leg_dmv_deepening,
        }


def _build_kinematic_timeline(
    conn: sqlite3.Connection,
    bench: pd.Series,
    *,
    stock_id: str,
    entry_i: int,
    exit_i: int,
    frames: list[dict[str, str]],
    points_by_idx: list[dict[str, Any] | None],
    entry_px: float,
) -> list[KinematicSnap]:
    if entry_px <= 0:
        return []
    snaps: list[KinematicSnap] = []
    w20_peak = w5_peak = w3_peak = None
    rv20_peak = rv5_peak = rv3_peak = None
    prev_w20 = prev_w5 = prev_w3 = None
    w20_streak = w5_streak = w3_streak = 0
    prev_dmv: dict[str, float | None] = {"w20": None, "w5": None, "w3": None}

    for j in range(entry_i, min(exit_i + 1, len(frames))):
        px, _ = _price_at(conn, bench, stock_id, frames[j]["date"], frames[j]["minute"])
        if px is None or px <= 0:
            continue
        p = points_by_idx[j]
        if not p:
            continue

        w20mv = _mv(p, "w20")
        w5mv = _mv(p, "w5")
        w3mv = _mv(p, "w3")
        w20rv = _rv(p, "w20")
        w5rv = _rv(p, "w5")
        w3rv = _rv(p, "w3")

        if w20mv is not None:
            w20_peak = w20mv if w20_peak is None else max(w20_peak, w20mv)
            w20_streak = w20_streak + 1 if prev_w20 is not None and w20mv < prev_w20 - 1e-9 else 0
        if w5mv is not None:
            w5_peak = w5mv if w5_peak is None else max(w5_peak, w5mv)
            w5_streak = w5_streak + 1 if prev_w5 is not None and w5mv < prev_w5 - 1e-9 else 0
        if w3mv is not None:
            w3_peak = w3mv if w3_peak is None else max(w3_peak, w3mv)
            w3_streak = w3_streak + 1 if prev_w3 is not None and w3mv < prev_w3 - 1e-9 else 0

        if w20rv is not None:
            rv20_peak = w20rv if rv20_peak is None else max(rv20_peak, w20rv)
        if w5rv is not None:
            rv5_peak = w5rv if rv5_peak is None else max(rv5_peak, w5rv)
        if w3rv is not None:
            rv3_peak = w3rv if rv3_peak is None else max(rv3_peak, w3rv)

        dmv_w20 = round(w20mv - w20_peak, 4) if w20mv is not None and w20_peak is not None else None
        dmv_w5 = round(w5mv - w5_peak, 4) if w5mv is not None and w5_peak is not None else None
        dmv_w3 = round(w3mv - w3_peak, 4) if w3mv is not None and w3_peak is not None else None
        drv_w20 = round(w20rv - rv20_peak, 4) if w20rv is not None and rv20_peak is not None else None
        drv_w5 = round(w5rv - rv5_peak, 4) if w5rv is not None and rv5_peak is not None else None
        drv_w3 = round(w3rv - rv3_peak, 4) if w3rv is not None and rv3_peak is not None else None

        parallel = (
            dmv_w20 is not None
            and dmv_w5 is not None
            and dmv_w3 is not None
            and dmv_w20 < 0
            and dmv_w5 < 0
            and dmv_w3 < 0
            and (
                prev_dmv["w20"] is None
                or (
                    dmv_w20 <= (prev_dmv["w20"] or 0) - 1e-9
                    and dmv_w5 <= (prev_dmv["w5"] or 0) - 1e-9
                    and dmv_w3 <= (prev_dmv["w3"] or 0) - 1e-9
                )
            )
        )

        mv_map = {"w20": w20mv, "w5": w5mv, "w3": w3mv}
        ranks = _mv_ranks(mv_map)
        ret = (px / entry_px - 1.0) * 100.0
        f = frames[j]
        snaps.append(
            KinematicSnap(
                frame_idx=j,
                trade_date=f["date"],
                minute=f["minute"],
                px=float(px),
                ret_from_entry_pct=ret,
                w20_rv=w20rv,
                w20_mv=w20mv,
                w20_quad=_q(p, "w20"),
                w5_rv=w5rv,
                w5_mv=w5mv,
                w5_quad=_q(p, "w5"),
                w3_rv=w3rv,
                w3_mv=w3mv,
                w3_quad=_q(p, "w3"),
                spread_class=p.get("spread_class"),
                spread_dist=float(p["spread_dist"]) if p.get("spread_dist") is not None else None,
                gap_mv_w20_w5=_safe_gap(w20mv, w5mv),
                gap_mv_w5_w3=_safe_gap(w5mv, w3mv),
                gap_mv_w20_w3=_safe_gap(w20mv, w3mv),
                gap_rv_w20_w5=_safe_gap(w20rv, w5rv),
                gap_rv_w5_w3=_safe_gap(w5rv, w3rv),
                gap_rv_w20_w3=_safe_gap(w20rv, w3rv),
                gap_rv_w5_w20=_safe_gap(w5rv, w20rv),
                dmv_w20=dmv_w20,
                dmv_w5=dmv_w5,
                dmv_w3=dmv_w3,
                drv_w20=drv_w20,
                drv_w5=drv_w5,
                drv_w3=drv_w3,
                mv_order=_mv_order(mv_map),
                mv_rank_w20=ranks["w20"],
                mv_rank_w5=ranks["w5"],
                mv_rank_w3=ranks["w3"],
                w20_mv_decline_streak=w20_streak,
                w5_mv_decline_streak=w5_streak,
                w3_mv_decline_streak=w3_streak,
                parallel_3leg_dmv_deepening=parallel,
            )
        )
        prev_dmv = {"w20": dmv_w20, "w5": dmv_w5, "w3": dmv_w3}
        if w20mv is not None:
            prev_w20 = w20mv
        if w5mv is not None:
            prev_w5 = w5mv
        if w3mv is not None:
            prev_w3 = w3mv
    return snaps


def _leg_from_timeline(
    period: dict[str, Any],
    timeline: list[KinematicSnap],
) -> dict[str, Any] | None:
    if len(timeline) < 2:
        return None
    entry = timeline[0]
    peak_i = max(range(len(timeline)), key=lambda i: timeline[i].px)
    trough_i = min(range(len(timeline)), key=lambda i: timeline[i].px)
    peak = timeline[peak_i]
    actual_ret = float(period.get("return_pct") or 0)
    return {
        "leg_id": f"{period.get('entry_date')}|{period.get('stock_id')}|{period.get('slot', 0)}",
        "stock_id": period.get("stock_id"),
        "stock_name": period.get("stock_name") or "",
        "entry_date": period.get("entry_date"),
        "exit_date": period.get("exit_date"),
        "entry_minute": period.get("entry_minute"),
        "hold_days": period.get("hold_days"),
        "return_pct": actual_ret,
        "outcome": "win" if actual_ret > 0 else "loss",
        "peak_ret_pct": round(peak.ret_from_entry_pct, 3),
        "trough_ret_pct": round(timeline[trough_i].ret_from_entry_pct, 3),
        "peak_frame_idx": peak_i,
        "entry_t0": entry.to_dict(),
        "timeline": [s.to_dict() for s in timeline],
    }


def build_kinematic_legs_from_periods(
    conn: sqlite3.Connection,
    periods: list[dict[str, Any]],
    *,
    close: pd.DataFrame | None = None,
    full_dates: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Annotate executed sim periods with KinematicSnap 30m timelines."""
    if close is None:
        close, _, _ = load_price_panels(conn)
    dates = full_dates or close.index.astype(str).tolist()
    stock_ids = sorted({str(p["stock_id"]) for p in periods})
    annot_dates = _collect_annot_dates(periods, dates)
    print(
        f"kinematic-legs: {len(periods)} periods · {len(stock_ids)} stocks · "
        f"{len(annot_dates)} dates …",
        flush=True,
    )
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    frames, trajectories, traj_meta = build_dual_wma_intraday_trajectories(
        conn,
        dates=annot_dates,
        stock_ids=stock_ids,
        poll_minutes=intraday_poll_minutes(),
        micro_lengths=(WMA_MICRO_LENGTH,),
    )
    stock_maps = _build_stock_frame_maps(trajectories, frames)

    legs: list[dict[str, Any]] = []
    for p in periods:
        sid = str(p["stock_id"])
        entry = str(p["entry_date"])
        exit_d = str(p.get("exit_date") or "")
        entry_px = p.get("entry_px")
        if not exit_d or not entry_px or float(entry_px) <= 0:
            continue
        points = stock_maps.get(sid)
        if not points:
            continue
        entry_i = _frame_idx_for_minute(frames, entry, p.get("entry_minute"))
        exit_i = _exit_frame_idx_for_date(frames, exit_d)
        if exit_i < entry_i:
            continue
        timeline = _build_kinematic_timeline(
            conn,
            bench,
            stock_id=sid,
            entry_i=entry_i,
            exit_i=exit_i,
            frames=frames,
            points_by_idx=points,
            entry_px=float(entry_px),
        )
        leg = _leg_from_timeline(p, timeline)
        if leg:
            legs.append(leg)
    return legs, traj_meta


def run_c18acc_kinematic_timeline_cache(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    n_slots: int = 99,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    if len(trade_dates) < 5:
        raise ValueError(f"insufficient trade dates: {len(trade_dates)}")

    ctx = _prepare_c18acc_context(conn, trade_dates)
    cfg = c18acc_live_s2_config(n_slots=n_slots)
    periods, sim_summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=ctx["full_dates"],
        close=ctx["close"],
        bench=ctx["bench"],
        fresh_by_date=ctx["fresh_by_date"],
        zone_by_date=ctx["zone_by_date"],
        config=cfg,
        mono_by_date=ctx["mono_by_date"],
        mono_up_by_date=ctx["mono_up_by_date"],
        mono_up_fresh_by_date=ctx["mono_up_fresh_by_date"],
        kbar_cache=ctx["kbar_cache"],
        rs_mom=ctx["rs_mom"],
        rs_ratio=ctx["rs_ratio"],
    )
    legs, traj_meta = build_kinematic_legs_from_periods(
        conn,
        periods,
        close=ctx["close"],
        full_dates=ctx["full_dates"],
    )

    return {
        "schema": "c18acc_kinematic_timeline-v1",
        "date_start": trade_dates[0],
        "date_end": trade_dates[-1],
        "n_slots": n_slots,
        "poll": "30m",
        "sim_summary": sim_summary,
        "trajectory_meta": traj_meta,
        "n_legs": len(legs),
        "legs": legs,
    }
