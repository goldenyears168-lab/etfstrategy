"""Dual WMA 四情境訊號 · 事件回測與參數 sweep。"""

from __future__ import annotations

import itertools
import math
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

from flow_returns import return_pct, stock_close, trading_dates_after
from market_benchmark import load_benchmark_close
from research.backtest.finpilot_local_backtest import load_price_panels
from rrg_universe_intraday_panel import (
    DEEP_PULLBACK_MIN,
    SMALL_EXTENSION_MAX,
    SPREAD_ALIGNED_MAX,
    SPREAD_SIGNAL_MIN,
    build_dual_wma_intraday_trajectories,
    classify_dual_wma_trade_signal,
    classify_wma_spread,
    intraday_poll_minutes,
)
from rrg_universe_snapshot import kbar_close_at_minute

ROUND_TRIP_COST_PCT = 0.585  # 0.1425*2 + 0.3
DEFAULT_POLL_END = "13:30"


@dataclass(frozen=True)
class EntryParams:
    signal: str = "lead_pullback_buy"
    pullback_min: float = SPREAD_SIGNAL_MIN
    pullback_max: float | None = None
    require_w5_improving: bool = False
    small_extension_max: float | None = SMALL_EXTENSION_MAX
    aligned_max: float = SPREAD_ALIGNED_MAX
    require_both_vectors: bool = False
    w20_rs_min: float | None = None
    w20_rs_max: float | None = None
    first_per_day: bool = True


@dataclass(frozen=True)
class ExitRule:
    rule_id: str
    label: str


@dataclass(frozen=True)
class W20LeadingExitParams:
    """Parametric exit_w20_leading + W5-assisted variants."""

    min_hold_polls: int = 0
    w20_rs_min_at_exit: float | None = None
    w5_gate: str | None = None  # leading · extension · leading_or_ext · not_lagging
    confirm_polls: int = 1
    cap_days: int | None = 5
    trail_after_w20: str | None = None  # w5_extension · w5_weakening · w5_leading

    def label(self) -> str:
        parts = ["W20→Leading"]
        if self.w20_rs_min_at_exit is not None:
            parts.append(f"RS≥{self.w20_rs_min_at_exit}")
        if self.min_hold_polls:
            parts.append(f"min{self.min_hold_polls}p")
        if self.confirm_polls > 1:
            parts.append(f"cfm{self.confirm_polls}")
        if self.w5_gate:
            parts.append(f"W5={self.w5_gate}")
        if self.trail_after_w20:
            parts.append(f"trail→{self.trail_after_w20}")
        if self.cap_days:
            parts.append(f"cap{self.cap_days}d")
        return " · ".join(parts)


EXIT_RULES: tuple[ExitRule, ...] = (
    ExitRule("hold_1poll", "持有 1 幀（30m）"),
    ExitRule("hold_6poll", "持有 6 幀（~3h）"),
    ExitRule("hold_1d", "持有 1 交易日 → 13:30"),
    ExitRule("hold_3d", "持有 3 交易日 → 13:30"),
    ExitRule("hold_5d", "持有 5 交易日 → 13:30"),
    ExitRule("exit_w20_weakening", "W20 離開 Leading/Weakening"),
    ExitRule("exit_w5_extension", "W5 Extension（短線超前）"),
    ExitRule("exit_spread_aligned", "Spread 收斂至 Aligned"),
)

PHASE2_EXIT_RULES: tuple[ExitRule, ...] = EXIT_RULES + (
    ExitRule("exit_w5_leading", "W5 進 Leading"),
    ExitRule("exit_w20_leading", "W20 進 Leading"),
    ExitRule("hybrid_cap5d_w20_weak", "hold_5d 上限 · W20 轉弱提前"),
    ExitRule("hybrid_cap5d_spread_aligned", "hold_5d 上限 · Spread Aligned 提前"),
    ExitRule("hybrid_cap3d_w20_weak", "hold_3d 上限 · W20 轉弱提前"),
    ExitRule("hybrid_cap3d_spread_aligned", "hold_3d 上限 · Spread Aligned 提前"),
)


def imp_early_long_champion_entry() -> EntryParams:
    """Phase 1 champion · 20260703 sweep."""
    return EntryParams(
        signal="imp_early_long",
        small_extension_max=2.5,
        aligned_max=1.0,
        require_both_vectors=True,
        w20_rs_min=96.0,
        w20_rs_max=100.0,
        first_per_day=True,
    )


def default_w20_leading_exit() -> W20LeadingExitParams:
    """Phase 2 baseline · plain W20 quadrant leading."""
    return W20LeadingExitParams(
        min_hold_polls=0,
        w20_rs_min_at_exit=None,
        w5_gate=None,
        confirm_polls=1,
        cap_days=None,
        trail_after_w20=None,
    )


def _w20_leading_same_frame(
    p: dict[str, Any],
    *,
    w20_rs_min_at_exit: float | None,
    w5_gate: str | None,
) -> bool:
    w20 = p.get("w20") or {}
    if w20.get("quadrant") != "leading":
        return False
    if w20_rs_min_at_exit is not None:
        if float(w20.get("rs_ratio") or 0) < w20_rs_min_at_exit:
            return False
    if w5_gate is None:
        return True
    w5q = (p.get("w5") or {}).get("quadrant")
    sc = p.get("spread_class")
    if w5_gate == "leading":
        return w5q == "leading"
    if w5_gate == "extension":
        return sc == "extension"
    if w5_gate == "leading_or_ext":
        return w5q == "leading" or sc == "extension"
    if w5_gate == "not_lagging":
        return w5q != "lagging"
    return False


def _w5_trail_hit(p: dict[str, Any], trail: str) -> bool:
    w5q = (p.get("w5") or {}).get("quadrant")
    sc = p.get("spread_class")
    if trail == "w5_extension":
        return sc == "extension"
    if trail == "w5_weakening":
        return w5q in ("weakening", "lagging")
    if trail == "w5_leading":
        return w5q == "leading"
    return False


def _exit_frame_idx_w20_leading(
    entry_idx: int,
    frames: list[dict[str, str]],
    points_by_idx: list[dict[str, Any] | None],
    xp: W20LeadingExitParams,
    *,
    max_forward: int = 50,
) -> int:
    n = len(frames)
    last = min(n - 1, entry_idx + max_forward)
    if xp.cap_days is not None:
        last = min(last, _hold_nd_frame_idx(entry_idx, frames, xp.cap_days))
    start = entry_idx + max(1, xp.min_hold_polls)

    if xp.trail_after_w20:
        seen_w20 = False
        for j in range(start, last + 1):
            p = points_by_idx[j]
            if not p:
                continue
            if _w20_leading_same_frame(
                p, w20_rs_min_at_exit=xp.w20_rs_min_at_exit, w5_gate=None
            ):
                seen_w20 = True
            if seen_w20 and _w5_trail_hit(p, xp.trail_after_w20):
                return j
        return last

    streak = 0
    for j in range(start, last + 1):
        p = points_by_idx[j]
        if not p:
            streak = 0
            continue
        if _w20_leading_same_frame(
            p,
            w20_rs_min_at_exit=xp.w20_rs_min_at_exit,
            w5_gate=xp.w5_gate,
        ):
            streak += 1
            if streak >= xp.confirm_polls:
                return j
        else:
            streak = 0
    return last


def _matches_lead_pullback(
    p: dict[str, Any],
    *,
    pullback_min: float,
    pullback_max: float | None,
    require_w5_improving: bool,
) -> bool:
    if not p.get("fresh"):
        return False
    w20q = p["w20"]["quadrant"]
    w5q = p["w5"]["quadrant"]
    if w20q != "leading" or p["spread_class"] != "pullback":
        return False
    dist = float(p["spread_dist"])
    if dist < pullback_min:
        return False
    if pullback_max is not None and dist > pullback_max:
        return False
    if w5q == "lagging" and dist >= DEEP_PULLBACK_MIN:
        return False
    if require_w5_improving and w5q != "improving":
        return False
    return True


def _matches_imp_early_long(
    p: dict[str, Any],
    *,
    small_extension_max: float | None,
    aligned_max: float,
    require_both_vectors: bool,
    w20_rs_min: float | None,
    w20_rs_max: float | None,
) -> bool:
    if not p.get("fresh"):
        return False
    w20 = p.get("w20") or {}
    w5 = p.get("w5") or {}
    if w20.get("quadrant") != "improving" or w5.get("quadrant") != "improving":
        return False
    dist = float(p.get("spread_dist") or 0)
    srs = float(p.get("spread_rs") or 0)
    sms = float(p.get("spread_mom") or 0)
    is_aligned = dist <= aligned_max
    is_small_ext = False
    if small_extension_max is not None:
        is_extension = srs > 0 and sms > 0 and dist >= SPREAD_SIGNAL_MIN
        is_small_ext = is_extension and dist < small_extension_max
    if not (is_aligned or is_small_ext):
        return False
    if require_both_vectors:
        if not (srs >= 0 and sms >= 0):
            return False
    elif not (srs >= 0 or sms >= 0):
        return False
    w20_rs = float(w20.get("rs_ratio") or 0)
    if w20_rs_min is not None and w20_rs < w20_rs_min:
        return False
    if w20_rs_max is not None and w20_rs > w20_rs_max:
        return False
    return True


def _matches_entry(p: dict[str, Any], ep: EntryParams) -> bool:
    if ep.signal == "lead_pullback_buy":
        return _matches_lead_pullback(
            p,
            pullback_min=ep.pullback_min,
            pullback_max=ep.pullback_max,
            require_w5_improving=ep.require_w5_improving,
        )
    if ep.signal == "imp_early_long":
        return _matches_imp_early_long(
            p,
            small_extension_max=ep.small_extension_max,
            aligned_max=ep.aligned_max,
            require_both_vectors=ep.require_both_vectors,
            w20_rs_min=ep.w20_rs_min,
            w20_rs_max=ep.w20_rs_max,
        )
    sig = classify_dual_wma_trade_signal(p)
    return sig == ep.signal


def _price_at(
    conn: sqlite3.Connection,
    bench: pd.Series,
    stock_id: str,
    trade_date: str,
    minute: str,
) -> tuple[float | None, float | None]:
    px = kbar_close_at_minute(conn, stock_id, trade_date, minute)
    if px is None or px <= 0:
        px = stock_close(conn, stock_id, trade_date)
    bpx = kbar_close_at_minute(conn, "IX0001", trade_date, minute)
    if bpx is None or bpx <= 0:
        if trade_date in bench.index:
            bpx = float(bench.at[trade_date])
        else:
            bpx = None
    return px, bpx


def _hold_nd_frame_idx(
    entry_idx: int,
    frames: list[dict[str, str]],
    days: int,
) -> int:
    n = len(frames)
    entry_date = frames[entry_idx]["date"]
    dates = sorted({f["date"] for f in frames})
    if entry_date not in dates:
        return entry_idx
    di = dates.index(entry_date)
    target_date = dates[min(di + days, len(dates) - 1)]
    for j in range(entry_idx, n):
        if frames[j]["date"] == target_date and frames[j]["minute"] == DEFAULT_POLL_END:
            return j
    for j in range(entry_idx, n):
        if frames[j]["date"] == target_date:
            return j
    return min(entry_idx + 1, n - 1)


def _exit_frame_idx(
    rule_id: str,
    entry_idx: int,
    frames: list[dict[str, str]],
    points_by_idx: list[dict[str, Any] | None],
    *,
    max_forward: int = 50,
    aligned_max: float = SPREAD_ALIGNED_MAX,
) -> int:
    n = len(frames)
    last = min(n - 1, entry_idx + max_forward)

    if rule_id == "hold_1poll":
        return min(entry_idx + 1, n - 1)
    if rule_id == "hold_6poll":
        return min(entry_idx + 6, n - 1)
    if rule_id in ("hold_1d", "hold_3d", "hold_5d"):
        days = int(rule_id.replace("hold_", "").replace("d", ""))
        return _hold_nd_frame_idx(entry_idx, frames, days)

    if rule_id == "hybrid_cap5d_w20_weak":
        cap = _hold_nd_frame_idx(entry_idx, frames, 5)
        for j in range(entry_idx + 1, cap + 1):
            p = points_by_idx[j]
            if p and p["w20"]["quadrant"] in ("weakening", "lagging"):
                return j
        return cap
    if rule_id == "hybrid_cap5d_spread_aligned":
        cap = _hold_nd_frame_idx(entry_idx, frames, 5)
        for j in range(entry_idx + 1, cap + 1):
            p = points_by_idx[j]
            if p and float(p["spread_dist"]) <= aligned_max:
                return j
        return cap
    if rule_id == "hybrid_cap3d_w20_weak":
        cap = _hold_nd_frame_idx(entry_idx, frames, 3)
        for j in range(entry_idx + 1, cap + 1):
            p = points_by_idx[j]
            if p and p["w20"]["quadrant"] in ("weakening", "lagging"):
                return j
        return cap
    if rule_id == "hybrid_cap3d_spread_aligned":
        cap = _hold_nd_frame_idx(entry_idx, frames, 3)
        for j in range(entry_idx + 1, cap + 1):
            p = points_by_idx[j]
            if p and float(p["spread_dist"]) <= aligned_max:
                return j
        return cap

    for j in range(entry_idx + 1, last + 1):
        p = points_by_idx[j]
        if not p:
            continue
        w20q = p["w20"]["quadrant"]
        w5q = p["w5"]["quadrant"]
        if rule_id == "exit_w20_weakening" and w20q in ("weakening", "lagging"):
            return j
        if rule_id == "exit_w5_extension" and p["spread_class"] == "extension":
            return j
        if rule_id == "exit_spread_aligned" and float(p["spread_dist"]) <= aligned_max:
            return j
        if rule_id == "exit_w5_leading" and w5q == "leading":
            return j
        if rule_id == "exit_w20_leading" and w20q == "leading":
            return j
    return last


def _exit_frame_idx_w20_leading_or_legacy(
    rule_id: str,
    entry_idx: int,
    frames: list[dict[str, str]],
    points_by_idx: list[dict[str, Any] | None],
    *,
    w20_exit: W20LeadingExitParams | None = None,
    max_forward: int = 50,
    aligned_max: float = SPREAD_ALIGNED_MAX,
) -> int:
    if rule_id == "exit_w20_leading" and w20_exit is not None:
        return _exit_frame_idx_w20_leading(
            entry_idx, frames, points_by_idx, w20_exit, max_forward=max_forward
        )
    return _exit_frame_idx(
        rule_id,
        entry_idx,
        frames,
        points_by_idx,
        max_forward=max_forward,
        aligned_max=aligned_max,
    )


def _build_stock_frame_maps(
    trajectories: list[dict[str, Any]],
    frames: list[dict[str, str]],
) -> dict[str, list[dict[str, Any] | None]]:
    frame_ids = [f["id"] for f in frames]
    out: dict[str, list[dict[str, Any] | None]] = {}
    for t in trajectories:
        by_id = {p["frame_id"]: p for p in t["points"]}
        out[t["stock_id"]] = [by_id.get(fid) for fid in frame_ids]
    return out


def _collect_events(
    trajectories: list[dict],
    frames: list[dict[str, str]],
    ep: EntryParams,
) -> list[tuple[str, int]]:
    frame_ids = [f["id"] for f in frames]
    events: list[tuple[str, int]] = []
    seen_day: set[tuple[str, str]] = set()
    for t in trajectories:
        sid = t["stock_id"]
        by_id = {p["frame_id"]: p for p in t["points"]}
        for i, fid in enumerate(frame_ids):
            p = by_id.get(fid)
            if not p or not _matches_entry(p, ep):
                continue
            d = frames[i]["date"]
            if ep.first_per_day:
                key = (sid, d)
                if key in seen_day:
                    continue
                seen_day.add(key)
            events.append((sid, i))
    return events


def _trade_return(
    conn: sqlite3.Connection,
    bench: pd.Series,
    stock_maps: dict[str, list[dict[str, Any] | None]],
    sid: str,
    entry_idx: int,
    exit_idx: int,
    frames: list[dict[str, str]],
) -> dict[str, Any] | None:
    if exit_idx <= entry_idx:
        return None
    ef, xf = frames[entry_idx], frames[exit_idx]
    entry_px, entry_b = _price_at(conn, bench, sid, ef["date"], ef["minute"])
    exit_px, exit_b = _price_at(conn, bench, sid, xf["date"], xf["minute"])
    if not entry_px or not exit_px or not entry_b or not exit_b:
        return None
    gross = return_pct(entry_px, exit_px)
    bench_ret = return_pct(entry_b, exit_b)
    net = gross - ROUND_TRIP_COST_PCT
    excess = net - bench_ret
    hold_polls = exit_idx - entry_idx
    return {
        "stock_id": sid,
        "entry_date": ef["date"],
        "entry_minute": ef["minute"],
        "exit_date": xf["date"],
        "exit_minute": xf["minute"],
        "entry_px": round(entry_px, 2),
        "exit_px": round(exit_px, 2),
        "gross_pct": round(gross, 3),
        "net_pct": round(net, 3),
        "bench_return_pct": round(bench_ret, 3),
        "excess_pct": round(excess, 3),
        "hold_polls": hold_polls,
        "win": net > 0,
    }


def run_entry_exit_grid(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    entry_params: EntryParams,
    exit_rule: ExitRule,
    etf_codes: tuple[str, ...],
    _cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if _cache is not None:
        frames = _cache["frames"]
        trajectories = _cache["trajectories"]
        close = _cache["close"]
        bench = _cache["bench"]
        stock_maps = _cache["stock_maps"]
    else:
        frames, trajectories, _meta = build_dual_wma_intraday_trajectories(
            conn, dates=dates, etf_codes=etf_codes
        )
        close, _, _ = load_price_panels(conn)
        bench = load_benchmark_close(conn).reindex(close.index).astype(float)
        stock_maps = _build_stock_frame_maps(trajectories, frames)

    events = _collect_events(trajectories, frames, entry_params)

    trades: list[dict[str, Any]] = []
    for sid, ei in events:
        pts = stock_maps.get(sid)
        if not pts:
            continue
        xi = _exit_frame_idx(
            exit_rule.rule_id,
            ei,
            frames,
            pts,
            aligned_max=entry_params.aligned_max,
        )
        tr = _trade_return(conn, bench, stock_maps, sid, ei, xi, frames)
        if tr:
            trades.append(tr)

    return _summarize_trades(trades, entry_params, exit_rule)


def _summarize_w20_exit_trades(
    trades: list[dict[str, Any]],
    entry_params: EntryParams,
    exit_params: W20LeadingExitParams,
) -> dict[str, Any]:
    base = _summarize_trades(
        trades,
        entry_params,
        ExitRule("exit_w20_leading", exit_params.label()),
    )
    base.update(
        {
            "exit_min_hold_polls": exit_params.min_hold_polls,
            "exit_w20_rs_min": exit_params.w20_rs_min_at_exit,
            "exit_w5_gate": exit_params.w5_gate,
            "exit_confirm_polls": exit_params.confirm_polls,
            "exit_cap_days": exit_params.cap_days,
            "exit_trail_after_w20": exit_params.trail_after_w20,
        }
    )
    return base


def run_w20_leading_exit_grid(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    entry_params: EntryParams,
    exit_params: W20LeadingExitParams,
    etf_codes: tuple[str, ...],
    _cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if _cache is not None:
        frames = _cache["frames"]
        trajectories = _cache["trajectories"]
        bench = _cache["bench"]
        stock_maps = _cache["stock_maps"]
    else:
        frames, trajectories, _meta = build_dual_wma_intraday_trajectories(
            conn, dates=dates, etf_codes=etf_codes
        )
        close, _, _ = load_price_panels(conn)
        bench = load_benchmark_close(conn).reindex(close.index).astype(float)
        stock_maps = _build_stock_frame_maps(trajectories, frames)

    events = _collect_events(trajectories, frames, entry_params)
    trades: list[dict[str, Any]] = []
    for sid, ei in events:
        pts = stock_maps.get(sid)
        if not pts:
            continue
        xi = _exit_frame_idx_w20_leading(ei, frames, pts, exit_params)
        tr = _trade_return(conn, bench, stock_maps, sid, ei, xi, frames)
        if tr:
            trades.append(tr)
    return _summarize_w20_exit_trades(trades, entry_params, exit_params)


def w20_leading_exit_grid() -> list[W20LeadingExitParams]:
    """Phase 2b · W20 leading exit + W5 gate / trail sweep."""
    seen: set[tuple] = set()
    grid: list[W20LeadingExitParams] = []

    def add(xp: W20LeadingExitParams) -> None:
        key = (
            xp.min_hold_polls,
            xp.w20_rs_min_at_exit,
            xp.w5_gate,
            xp.confirm_polls,
            xp.cap_days,
            xp.trail_after_w20,
        )
        if key in seen:
            return
        seen.add(key)
        grid.append(xp)

    add(default_w20_leading_exit())

    for min_hold, rs_min, w5_gate, cap in itertools.product(
        (0, 3, 6),
        (None, 100.0, 100.5, 101.0),
        (None, "leading", "extension", "leading_or_ext", "not_lagging"),
        (5,),
    ):
        add(
            W20LeadingExitParams(
                min_hold_polls=min_hold,
                w20_rs_min_at_exit=rs_min,
                w5_gate=w5_gate,
                confirm_polls=1,
                cap_days=cap,
            )
        )

    for min_hold, rs_min, cfm in itertools.product((0, 3), (None, 100.5), (1, 2, 3)):
        add(
            W20LeadingExitParams(
                min_hold_polls=min_hold,
                w20_rs_min_at_exit=rs_min,
                w5_gate=None,
                confirm_polls=cfm,
                cap_days=5,
            )
        )

    for trail, min_hold, cap, rs_min in itertools.product(
        ("w5_extension", "w5_weakening", "w5_leading"),
        (0, 3),
        (5, 3),
        (None, 100.5),
    ):
        add(
            W20LeadingExitParams(
                min_hold_polls=min_hold,
                w20_rs_min_at_exit=rs_min,
                trail_after_w20=trail,
                cap_days=cap,
            )
        )

    for cap in (5, 3, None):
        add(
            W20LeadingExitParams(
                min_hold_polls=0,
                cap_days=cap,
            )
        )

    return grid


def _summarize_trades(
    trades: list[dict[str, Any]],
    entry_params: EntryParams,
    exit_rule: ExitRule,
) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {
            "entry": entry_params,
            "exit": exit_rule.rule_id,
            "n": 0,
            "mean_net_pct": None,
            "mean_excess_pct": None,
            "win_rate": None,
            "median_net_pct": None,
        }
    nets = [t["net_pct"] for t in trades]
    excess = [t["excess_pct"] for t in trades]
    wins = sum(1 for t in trades if t["win"])
    excess_wins = sum(1 for e in excess if e > 0)
    nets_sorted = sorted(nets)
    med = nets_sorted[n // 2] if n % 2 else (nets_sorted[n // 2 - 1] + nets_sorted[n // 2]) / 2
    excess_sorted = sorted(excess)
    med_excess = (
        excess_sorted[n // 2]
        if n % 2
        else (excess_sorted[n // 2 - 1] + excess_sorted[n // 2]) / 2
    )
    return {
        "entry_signal": entry_params.signal,
        "pullback_min": entry_params.pullback_min,
        "pullback_max": entry_params.pullback_max,
        "require_w5_improving": entry_params.require_w5_improving,
        "small_extension_max": entry_params.small_extension_max,
        "aligned_max": entry_params.aligned_max,
        "require_both_vectors": entry_params.require_both_vectors,
        "w20_rs_min": entry_params.w20_rs_min,
        "w20_rs_max": entry_params.w20_rs_max,
        "first_per_day": entry_params.first_per_day,
        "exit_rule": exit_rule.rule_id,
        "exit_label": exit_rule.label,
        "n": n,
        "mean_net_pct": round(sum(nets) / n, 3),
        "mean_excess_pct": round(sum(excess) / n, 3),
        "median_net_pct": round(med, 3),
        "median_excess_pct": round(med_excess, 3),
        "win_rate": round(100 * wins / n, 1),
        "excess_win_rate": round(100 * excess_wins / n, 1),
        "mean_hold_polls": round(sum(t["hold_polls"] for t in trades) / n, 1),
        "p10_net_pct": round(nets_sorted[max(0, n // 10)], 3),
    }


def lead_pullback_entry_grid() -> list[EntryParams]:
    grid: list[EntryParams] = []
    for pb_min, pb_max, w5imp in itertools.product(
        (1.5, 2.0, 2.5, 3.0, 4.0),
        (None, 5.0, 7.0),
        (False, True),
    ):
        if pb_max is not None and pb_max <= pb_min:
            continue
        grid.append(
            EntryParams(
                signal="lead_pullback_buy",
                pullback_min=pb_min,
                pullback_max=pb_max,
                require_w5_improving=w5imp,
                first_per_day=True,
            )
        )
    return grid


def imp_early_long_entry_grid() -> list[EntryParams]:
    grid: list[EntryParams] = []
    w20_bands: list[tuple[float | None, float | None]] = [
        (None, None),
        (98.0, 100.0),
        (96.0, 100.0),
    ]
    for small_ext, aligned, both_vec, (rs_min, rs_max) in itertools.product(
        (2.0, 2.5, 3.0, 3.5, None),
        (1.0, 1.5, 2.0),
        (False, True),
        w20_bands,
    ):
        if small_ext is not None and small_ext <= SPREAD_SIGNAL_MIN:
            continue
        grid.append(
            EntryParams(
                signal="imp_early_long",
                small_extension_max=small_ext,
                aligned_max=aligned,
                require_both_vectors=both_vec,
                w20_rs_min=rs_min,
                w20_rs_max=rs_max,
                first_per_day=True,
            )
        )
    return grid


def _entry_dedup_key(ep: EntryParams) -> tuple:
    return (
        ep.signal,
        ep.pullback_min,
        ep.pullback_max,
        ep.require_w5_improving,
        ep.small_extension_max,
        ep.aligned_max,
        ep.require_both_vectors,
        ep.w20_rs_min,
        ep.w20_rs_max,
        ep.first_per_day,
    )


def _is_default_lead_pullback_row(r: dict[str, Any]) -> bool:
    return r.get("entry_signal") != "lead_pullback_buy" or (
        r.get("pullback_min") == SPREAD_SIGNAL_MIN
        and r.get("pullback_max") is None
        and not r.get("require_w5_improving")
    )


def _is_default_imp_early_long_row(r: dict[str, Any]) -> bool:
    return r.get("entry_signal") != "imp_early_long" or (
        r.get("small_extension_max") == SMALL_EXTENSION_MAX
        and r.get("aligned_max") == SPREAD_ALIGNED_MAX
        and not r.get("require_both_vectors")
        and r.get("w20_rs_min") is None
        and r.get("w20_rs_max") is None
    )


def all_signal_entry_grid() -> list[EntryParams]:
    return [
        EntryParams(signal=s, first_per_day=True)
        for s in (
            "lead_pullback_buy",
            "imp_early_long",
            "imp_extension",
            "lead_deep_pullback_wait",
        )
    ]


def run_full_sweep(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...],
    focus_signal: str = "lead_pullback_buy",
    focus_pullback: bool | None = None,
) -> dict[str, Any]:
    if focus_pullback is not None:
        focus_signal = "lead_pullback_buy" if focus_pullback else "all"

    print("  building dual WMA trajectories (once) ...", flush=True)
    frames, trajectories, _meta = build_dual_wma_intraday_trajectories(
        conn, dates=dates, etf_codes=etf_codes
    )
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    cache = {
        "frames": frames,
        "trajectories": trajectories,
        "close": close,
        "bench": bench,
        "stock_maps": _build_stock_frame_maps(trajectories, frames),
    }

    if focus_signal == "imp_early_long":
        entry_grid = imp_early_long_entry_grid()
    elif focus_signal == "lead_pullback_buy":
        entry_grid = lead_pullback_entry_grid()
    else:
        entry_grid = []

    rows: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    combined: list[EntryParams] = []
    for ep in entry_grid + all_signal_entry_grid():
        key = _entry_dedup_key(ep)
        if key in seen:
            continue
        seen.add(key)
        combined.append(ep)
    total = len(combined) * len(EXIT_RULES)
    done = 0

    for ep in combined:
        for ex in EXIT_RULES:
            done += 1
            if done % 20 == 0:
                print(f"  sweep {done}/{total} ...", flush=True)
            row = run_entry_exit_grid(
                conn,
                dates=dates,
                entry_params=ep,
                exit_rule=ex,
                etf_codes=etf_codes,
                _cache=cache,
            )
            rows.append(row)

    valid = [r for r in rows if r.get("n", 0) >= 5]
    by_excess = sorted(
        valid,
        key=lambda r: (r.get("mean_excess_pct") or -999, r.get("n", 0)),
        reverse=True,
    )
    if focus_signal == "imp_early_long":
        baseline_filter = _is_default_imp_early_long_row
    else:
        baseline_filter = _is_default_lead_pullback_row
    by_signal = sorted(
        [r for r in rows if baseline_filter(r)],
        key=lambda r: (r.get("mean_excess_pct") or -999, r.get("n", 0)),
        reverse=True,
    )
    return {
        "dates": dates,
        "focus_signal": focus_signal,
        "rows": rows,
        "top_by_excess": by_excess[:15],
        "baseline_compare": by_signal,
    }


def render_sweep_md(payload: dict[str, Any]) -> str:
    dates = payload["dates"]
    focus = payload.get("focus_signal", "lead_pullback_buy")
    if focus == "imp_early_long":
        return _render_imp_early_long_sweep_md(payload)
    lines = [
        "# Dual WMA 強勢回踩 · 參數 Sweep 回測",
        "",
        f"期間：**{dates[0]} → {dates[-1]}** · 成本 round-trip **{ROUND_TRIP_COST_PCT}%** · "
        f"進場：**當幀 fresh K 收盤** · 每檔每日至多一筆",
        "",
        "## Top 15（依平均超額報酬）",
        "",
        "| 進場 | pb_min | pb_max | W5↑ | 出場 | n | 淨報酬% | 超額% | 勝率 | 持有幀 |",
        "|------|--------|--------|-----|------|---|---------|-------|------|--------|",
    ]
    for r in payload["top_by_excess"]:
        lines.append(
            f"| {r.get('entry_signal','')} | {r.get('pullback_min','—')} | "
            f"{r.get('pullback_max') or '—'} | "
            f"{'Y' if r.get('require_w5_improving') else 'N'} | "
            f"{r.get('exit_label','')} | {r['n']} | "
            f"{r.get('mean_net_pct')} | {r.get('mean_excess_pct')} | "
            f"{r.get('win_rate')}% | {r.get('mean_hold_polls')} |"
        )

    lines.extend(
        [
            "",
            "## 預設參數 · 四情境 vs 出場規則",
            "",
            "| 訊號 | 出場 | n | 淨報酬% | 超額% | 勝率 |",
            "|------|------|---|---------|-------|------|",
        ]
    )
    for r in payload["baseline_compare"][:20]:
        lines.append(
            f"| {r.get('entry_signal')} | {r.get('exit_label')} | {r['n']} | "
            f"{r.get('mean_net_pct')} | {r.get('mean_excess_pct')} | {r.get('win_rate')}% |"
        )

    lines.extend(
        [
            "",
            "## 解讀備註",
            "",
            "- **超額%** = 淨報酬 − IX0001 同期報酬。",
            "- `exit_spread_aligned` = W5 追上 W20（spread≤1.5）即出場，符合回踩均值回歸假設。",
            "- `exit_w20_weakening` = 結構轉弱止損。",
            "- 樣本期間僅 ~64 交易日，n 偏小時僅供方向、不作顯著性結論。",
        ]
    )
    return "\n".join(lines) + "\n"


def _fmt_w20_band(r: dict[str, Any]) -> str:
    lo, hi = r.get("w20_rs_min"), r.get("w20_rs_max")
    if lo is None and hi is None:
        return "—"
    return f"{lo}–{hi}"


def _fmt_small_ext(v: float | None) -> str:
    return "aligned-only" if v is None else str(v)


def _render_imp_early_long_sweep_md(payload: dict[str, Any]) -> str:
    dates = payload["dates"]
    lines = [
        "# Dual WMA 輪動早期 (imp_early_long) · 參數 Sweep 回測",
        "",
        f"期間：**{dates[0]} → {dates[-1]}** · 成本 round-trip **{ROUND_TRIP_COST_PCT}%** · "
        f"進場：**當幀 fresh K 收盤** · 每檔每日至多一筆",
        "",
        "## Top 15（依平均超額報酬）",
        "",
        "| small_ext | aligned | both_vec | W20 RS | 出場 | n | 淨報酬% | 超額% | 勝率 | 持有幀 |",
        "|-----------|---------|----------|--------|------|---|---------|-------|------|--------|",
    ]
    for r in payload["top_by_excess"]:
        lines.append(
            f"| {_fmt_small_ext(r.get('small_extension_max'))} | "
            f"{r.get('aligned_max')} | "
            f"{'Y' if r.get('require_both_vectors') else 'N'} | "
            f"{_fmt_w20_band(r)} | "
            f"{r.get('exit_label','')} | {r['n']} | "
            f"{r.get('mean_net_pct')} | {r.get('mean_excess_pct')} | "
            f"{r.get('win_rate')}% | {r.get('mean_hold_polls')} |"
        )

    lines.extend(
        [
            "",
            "## 預設參數 · 四情境 vs 出場規則",
            "",
            "| 訊號 | 出場 | n | 淨報酬% | 超額% | 勝率 |",
            "|------|------|---|---------|-------|------|",
        ]
    )
    for r in payload["baseline_compare"][:20]:
        lines.append(
            f"| {r.get('entry_signal')} | {r.get('exit_label')} | {r['n']} | "
            f"{r.get('mean_net_pct')} | {r.get('mean_excess_pct')} | {r.get('win_rate')}% |"
        )

    lines.extend(
        [
            "",
            "## 解讀備註",
            "",
            "- **超額%** = 淨報酬 − IX0001 同期報酬。",
            "- `small_ext=None` = 僅 aligned 路徑（spread_dist ≤ aligned_max）。",
            "- `both_vec=Y` = spread_rs≥0 **且** spread_mom≥0；否則 OR 條件。",
            "- W20 RS band 篩選 w20.rs_ratio 區間（Regime layer 輪動早期強度）。",
            "- 樣本期間僅 ~64 交易日，n 偏小時僅供方向、不作顯著性結論。",
        ]
    )
    return "\n".join(lines) + "\n"


def run_phase2_imp_early_long_exit_sweep(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...],
    entry_params: EntryParams | None = None,
) -> dict[str, Any]:
    """Phase 2 · 固定 champion 進場 × 全出場規則（含 hybrid）。"""
    ep = entry_params or imp_early_long_champion_entry()
    print("  building dual WMA trajectories (once) ...", flush=True)
    frames, trajectories, _meta = build_dual_wma_intraday_trajectories(
        conn, dates=dates, etf_codes=etf_codes
    )
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    cache = {
        "frames": frames,
        "trajectories": trajectories,
        "close": close,
        "bench": bench,
        "stock_maps": _build_stock_frame_maps(trajectories, frames),
    }

    rows: list[dict[str, Any]] = []
    for i, ex in enumerate(PHASE2_EXIT_RULES, 1):
        print(f"  phase2 exit {i}/{len(PHASE2_EXIT_RULES)} {ex.rule_id} ...", flush=True)
        rows.append(
            run_entry_exit_grid(
                conn,
                dates=dates,
                entry_params=ep,
                exit_rule=ex,
                etf_codes=etf_codes,
                _cache=cache,
            )
        )

    by_excess = sorted(
        rows,
        key=lambda r: (r.get("mean_excess_pct") or -999, r.get("median_excess_pct") or -999),
        reverse=True,
    )
    hold5 = next((r for r in rows if r.get("exit_rule") == "hold_5d"), None)
    return {
        "dates": dates,
        "focus_signal": "imp_early_long_phase2",
        "champion_entry": ep,
        "rows": rows,
        "top_by_excess": by_excess,
        "hold5_baseline": hold5,
    }


def render_phase2_imp_early_long_md(payload: dict[str, Any]) -> str:
    dates = payload["dates"]
    ep = payload["champion_entry"]
    hold5 = payload.get("hold5_baseline") or {}
    lines = [
        "# Dual WMA imp_early_long · Phase 2 出場交叉",
        "",
        f"期間：**{dates[0]} → {dates[-1]}** · 成本 round-trip **{ROUND_TRIP_COST_PCT}%**",
        "",
        "## Champion 進場（Phase 1 凍結）",
        "",
        f"- `small_extension_max` = **{ep.small_extension_max}**",
        f"- `aligned_max` = **{ep.aligned_max}**",
        f"- `require_both_vectors` = **{ep.require_both_vectors}**",
        f"- W20 RS band = **{ep.w20_rs_min}–{ep.w20_rs_max}**",
        f"- hold_5d 基線：n={hold5.get('n', '—')} · 超額 **{hold5.get('mean_excess_pct')}%** · "
        f"勝率 {hold5.get('win_rate')}% · 持有 {hold5.get('mean_hold_polls')} 幀",
        "",
        "## 出場規則排行（依平均超額報酬）",
        "",
        "| 出場 | n | 淨報酬% | 超額% | 中位超額% | 勝率 | 超額勝率 | 持有幀 | P10淨% |",
        "|------|---|---------|-------|-----------|------|----------|--------|--------|",
    ]
    for r in payload["top_by_excess"]:
        delta = ""
        if hold5.get("mean_excess_pct") is not None and r.get("mean_excess_pct") is not None:
            d = round(r["mean_excess_pct"] - hold5["mean_excess_pct"], 3)
            delta = f" ({'+' if d >= 0 else ''}{d})"
        lines.append(
            f"| {r.get('exit_label')} | {r['n']} | "
            f"{r.get('mean_net_pct')} | {r.get('mean_excess_pct')}{delta} | "
            f"{r.get('median_excess_pct')} | {r.get('win_rate')}% | "
            f"{r.get('excess_win_rate')}% | {r.get('mean_hold_polls')} | "
            f"{r.get('p10_net_pct')} |"
        )

    best = payload["top_by_excess"][0] if payload["top_by_excess"] else {}
    lines.extend(
        [
            "",
            "## Phase 2 假說",
            "",
            "| ID | 假說 | 結果 |",
            "|----|------|------|",
        ]
    )
    h5_excess = hold5.get("mean_excess_pct")
    best_excess = best.get("mean_excess_pct")
    h2a = (
        best.get("exit_rule", "").startswith("hybrid")
        and best_excess is not None
        and h5_excess is not None
        and best_excess >= h5_excess
    )
    spread_row = next((r for r in payload["rows"] if r.get("exit_rule") == "exit_spread_aligned"), {})
    h2b = (
        spread_row.get("mean_excess_pct") is not None
        and h5_excess is not None
        and spread_row["mean_excess_pct"] < h5_excess
    )
    w20_lead = next((r for r in payload["rows"] if r.get("exit_rule") == "exit_w20_leading"), {})
    h2c = (
        w20_lead.get("mean_excess_pct") is not None
        and h5_excess is not None
        and w20_lead["mean_excess_pct"] >= h5_excess * 0.85
    )
    lines.append(
        f"| H2a | hybrid 出場（cap + 提前）優於純 hold_5d | "
        f"{'supported' if h2a else 'rejected'} |"
    )
    lines.append(
        f"| H2b | 純 spread_aligned 犧牲超額換取較短持有 | "
        f"{'supported' if h2b else 'partial'} |"
    )
    lines.append(
        f"| H2c | W20 進 Leading 獲利了結保留大部分超額 | "
        f"{'supported' if h2c else 'rejected'} |"
    )

    lines.extend(
        [
            "",
            "## 解讀備註",
            "",
            "- 括號內數字 = 相對 hold_5d 基線之超額差。",
            "- **超額勝率** = P(淨報酬 − IX0001 > 0)。",
            "- **P10淨%** = 第 10 百分位淨報酬（尾部風險 proxy）。",
            "- hybrid 規則使用進場 `aligned_max` 作 spread 收斂門檻。",
            "- 樣本 n≈31，僅供方向；Phase 3 再加 Regime layer 濾網。",
        ]
    )
    return "\n".join(lines) + "\n"


def run_phase2b_w20_leading_tune(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...],
    entry_params: EntryParams | None = None,
) -> dict[str, Any]:
    """Phase 2b · champion 進場 × exit_w20_leading 參數 + W5 輔助 sweep。"""
    ep = entry_params or imp_early_long_champion_entry()
    exit_grid = w20_leading_exit_grid()
    print("  building dual WMA trajectories (once) ...", flush=True)
    frames, trajectories, _meta = build_dual_wma_intraday_trajectories(
        conn, dates=dates, etf_codes=etf_codes
    )
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    cache = {
        "frames": frames,
        "trajectories": trajectories,
        "close": close,
        "bench": bench,
        "stock_maps": _build_stock_frame_maps(trajectories, frames),
    }

    rows: list[dict[str, Any]] = []
    total = len(exit_grid)
    for i, xp in enumerate(exit_grid, 1):
        if i % 15 == 0 or i == total:
            print(f"  phase2b w20 exit {i}/{total} ...", flush=True)
        rows.append(
            run_w20_leading_exit_grid(
                conn,
                dates=dates,
                entry_params=ep,
                exit_params=xp,
                etf_codes=etf_codes,
                _cache=cache,
            )
        )

    by_excess = sorted(
        rows,
        key=lambda r: (
            r.get("mean_excess_pct") or -999,
            r.get("median_excess_pct") or -999,
        ),
        reverse=True,
    )
    baseline = next(
        (
            r
            for r in rows
            if r.get("exit_min_hold_polls") == 0
            and r.get("exit_w20_rs_min") is None
            and r.get("exit_w5_gate") is None
            and r.get("exit_confirm_polls") == 1
            and r.get("exit_cap_days") is None
            and r.get("exit_trail_after_w20") is None
        ),
        None,
    )
    hold5_row = run_entry_exit_grid(
        conn,
        dates=dates,
        entry_params=ep,
        exit_rule=ExitRule("hold_5d", "持有 5 交易日 → 13:30"),
        etf_codes=etf_codes,
        _cache=cache,
    )
    return {
        "dates": dates,
        "focus_signal": "imp_early_long_phase2b",
        "champion_entry": ep,
        "rows": rows,
        "top_by_excess": by_excess[:20],
        "w20_baseline": baseline,
        "hold5_baseline": hold5_row,
    }


def render_phase2b_w20_leading_md(payload: dict[str, Any]) -> str:
    dates = payload["dates"]
    w20b = payload.get("w20_baseline") or {}
    hold5 = payload.get("hold5_baseline") or {}
    lines = [
        "# Dual WMA imp_early_long · Phase 2b exit_w20_leading 調參 + W5 輔助",
        "",
        f"期間：**{dates[0]} → {dates[-1]}** · 成本 round-trip **{ROUND_TRIP_COST_PCT}%**",
        "",
        "## 基線",
        "",
        f"- 純 W20→Leading（無 cap）：超額 **{w20b.get('mean_excess_pct')}%** · "
        f"n={w20b.get('n')} · 持有 {w20b.get('mean_hold_polls')} 幀",
        f"- hold_5d：超額 **{hold5.get('mean_excess_pct')}%** · "
        f"持有 {hold5.get('mean_hold_polls')} 幀",
        "",
        "## Top 20（依平均超額報酬）",
        "",
        "| 出場規格 | n | 超額% | 中位超額% | 勝率 | 超額勝率 | 持有幀 | P10淨% |",
        "|----------|---|-------|-----------|------|----------|--------|--------|",
    ]
    ref = w20b.get("mean_excess_pct")
    for r in payload["top_by_excess"]:
        delta = ""
        if ref is not None and r.get("mean_excess_pct") is not None:
            d = round(r["mean_excess_pct"] - ref, 3)
            delta = f" ({'+' if d >= 0 else ''}{d})"
        lines.append(
            f"| {r.get('exit_label')} | {r['n']} | "
            f"{r.get('mean_excess_pct')}{delta} | {r.get('median_excess_pct')} | "
            f"{r.get('win_rate')}% | {r.get('excess_win_rate')}% | "
            f"{r.get('mean_hold_polls')} | {r.get('p10_net_pct')} |"
        )

    best = payload["top_by_excess"][0] if payload["top_by_excess"] else {}
    lines.extend(
        [
            "",
            "## Phase 2b 假說",
            "",
            "| ID | 假說 | 結果 |",
            "|----|------|------|",
        ]
    )
    w5_gate_rows = [r for r in payload["rows"] if r.get("exit_w5_gate")]
    best_gate = max(
        w5_gate_rows,
        key=lambda r: r.get("mean_excess_pct") or -999,
        default={},
    )
    h3a = (
        best_gate.get("mean_excess_pct") is not None
        and ref is not None
        and best_gate["mean_excess_pct"] > ref
    )
    trail_rows = [r for r in payload["rows"] if r.get("exit_trail_after_w20")]
    best_trail = max(
        trail_rows,
        key=lambda r: r.get("mean_excess_pct") or -999,
        default={},
    )
    h3b = (
        best_trail.get("mean_excess_pct") is not None
        and ref is not None
        and best_trail["mean_excess_pct"] > ref
    )
    rs_rows = [
        r
        for r in payload["rows"]
        if r.get("exit_w20_rs_min") is not None and not r.get("exit_trail_after_w20")
    ]
    best_rs = max(rs_rows, key=lambda r: r.get("mean_excess_pct") or -999, default={})
    h3c = (
        best_rs.get("mean_excess_pct") is not None
        and ref is not None
        and best_rs["mean_excess_pct"] > ref
    )
    lines.append(
        f"| H3a | W5 同幀 gate（延後至 W5 條件滿足）提升超額 | "
        f"{'supported' if h3a else 'rejected'} |"
    )
    lines.append(
        f"| H3b | W20 Leading 後 W5 trail 優於立即出場 | "
        f"{'supported' if h3b else 'rejected'} |"
    )
    lines.append(
        f"| H3c | W20 RS≥門檻確認減少假突破 | "
        f"{'supported' if h3c else 'rejected'} |"
    )

    if best:
        lines.extend(
            [
                "",
                "## 建議 champion 出場",
                "",
                f"- `{best.get('exit_label')}`",
                f"- 超額 **{best.get('mean_excess_pct')}%** · 中位超額 {best.get('median_excess_pct')}% · "
                f"持有 {best.get('mean_hold_polls')} 幀",
            ]
        )

    lines.extend(
        [
            "",
            "## 參數說明",
            "",
            "- **W5 gate**：W20 已 Leading 的**同一幀**須同時滿足 W5 條件才出場（延後出場）。",
            "- **trail**：W20 首次 Leading 後，等 W5 extension/weakening/leading 再出場。",
            "- **cfmN**：連續 N 幀滿足 W20 Leading 才出場。",
            "- **capNd**：最長持有 N 交易日（13:30）上限。",
            "- 括號 = 相對純 W20→Leading 基線超額差。",
        ]
    )
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class RegimeFilterParams:
    """Phase 3 · Regime layer 進場濾網（PIT：廣度 / flow tape 用信號日前一交易日）。"""

    breadth_zones: tuple[str, ...] | None = None
    flow_tape_regimes: tuple[str, ...] | None = None
    max_entry_chase_pct: float | None = None

    def label(self) -> str:
        parts: list[str] = []
        if self.breadth_zones:
            parts.append("zone=" + "+".join(self.breadth_zones))
        if self.flow_tape_regimes:
            parts.append("tape=" + "+".join(self.flow_tape_regimes))
        if self.max_entry_chase_pct is not None:
            parts.append(f"chase≤{self.max_entry_chase_pct}%")
        return " · ".join(parts) if parts else "無濾網（baseline）"


def build_regime_filter_context(
    conn: sqlite3.Connection,
    trade_dates: list[str],
) -> dict[str, Any]:
    from flow_returns import flow_tape_regime
    from market_breadth_ma import build_breadth_panel

    if not trade_dates:
        return {"zone_by_date": {}, "prior_by_date": {}, "flow_tape_by_date": {}}
    panel = build_breadth_panel(
        conn, date_start=trade_dates[0], date_end=trade_dates[-1]
    )
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    prior_by_date: dict[str, str | None] = {}
    for i, d in enumerate(trade_dates):
        prior_by_date[d] = trade_dates[i - 1] if i > 0 else None
    flow_tape_by_date: dict[str, str | None] = {}
    for d in trade_dates:
        ref = prior_by_date[d] or d
        flow_tape_by_date[d] = flow_tape_regime(conn, ref)
    return {
        "zone_by_date": zone_by_date,
        "prior_by_date": prior_by_date,
        "flow_tape_by_date": flow_tape_by_date,
    }


def _entry_chase_pct(
    conn: sqlite3.Connection,
    bench: pd.Series,
    sid: str,
    entry_date: str,
    entry_minute: str,
    prior_date: str | None,
) -> float | None:
    if not prior_date:
        return None
    prev_close = stock_close(conn, sid, prior_date)
    entry_px, _ = _price_at(conn, bench, sid, entry_date, entry_minute)
    if not prev_close or not entry_px or prev_close <= 0:
        return None
    return return_pct(prev_close, entry_px)


def _passes_regime_filter(
    entry_date: str,
    regime: RegimeFilterParams,
    ctx: dict[str, Any],
    *,
    chase_pct: float | None,
) -> bool:
    prior = ctx["prior_by_date"].get(entry_date) or entry_date
    if regime.breadth_zones:
        zone = ctx["zone_by_date"].get(prior, "unknown")
        if zone not in regime.breadth_zones:
            return False
    if regime.flow_tape_regimes:
        tape = ctx["flow_tape_by_date"].get(entry_date)
        if tape not in regime.flow_tape_regimes:
            return False
    if regime.max_entry_chase_pct is not None:
        if chase_pct is None or chase_pct > regime.max_entry_chase_pct:
            return False
    return True


def _regime_filter_key(rp: RegimeFilterParams) -> tuple:
    return (rp.breadth_zones, rp.flow_tape_regimes, rp.max_entry_chase_pct)


def regime_filter_grid() -> list[RegimeFilterParams]:
    seen: set[tuple] = set()
    grid: list[RegimeFilterParams] = []

    def add(rp: RegimeFilterParams) -> None:
        key = _regime_filter_key(rp)
        if key in seen:
            return
        seen.add(key)
        grid.append(rp)

    add(RegimeFilterParams())

    for zones in (
        ("neutral", "strong"),
        ("strong",),
        ("oversold", "weak", "neutral", "strong"),
        ("neutral",),
        ("weak", "neutral", "strong", "overbought"),
    ):
        add(RegimeFilterParams(breadth_zones=zones))

    for tape in (("多頭",), ("多頭", "震盪")):
        add(RegimeFilterParams(flow_tape_regimes=tape))

    for chase in (1.0, 2.0):
        add(RegimeFilterParams(max_entry_chase_pct=chase))

    add(
        RegimeFilterParams(
            breadth_zones=("neutral", "strong"),
            flow_tape_regimes=("多頭", "震盪"),
        )
    )
    add(
        RegimeFilterParams(
            breadth_zones=("neutral", "strong"),
            max_entry_chase_pct=1.0,
        )
    )
    add(
        RegimeFilterParams(
            flow_tape_regimes=("多頭", "震盪"),
            max_entry_chase_pct=1.0,
        )
    )
    add(
        RegimeFilterParams(
            breadth_zones=("neutral", "strong"),
            flow_tape_regimes=("多頭", "震盪"),
            max_entry_chase_pct=1.0,
        )
    )
    add(
        RegimeFilterParams(
            breadth_zones=("oversold", "weak", "neutral", "strong"),
            flow_tape_regimes=("多頭", "震盪"),
            max_entry_chase_pct=1.0,
        )
    )
    return grid


def collect_regime_filtered_trades(
    conn: sqlite3.Connection,
    *,
    entry_params: EntryParams,
    exit_params: W20LeadingExitParams,
    regime: RegimeFilterParams,
    trajectories: list[dict],
    frames: list[dict[str, str]],
    stock_maps: dict[str, list[dict[str, Any] | None]],
    bench: pd.Series,
    regime_ctx: dict[str, Any],
) -> list[dict[str, Any]]:
    events = _collect_events(trajectories, frames, entry_params)
    trades: list[dict[str, Any]] = []
    for sid, ei in events:
        ef = frames[ei]
        prior = regime_ctx["prior_by_date"].get(ef["date"])
        chase = _entry_chase_pct(
            conn, bench, sid, ef["date"], ef["minute"], prior
        )
        if not _passes_regime_filter(ef["date"], regime, regime_ctx, chase_pct=chase):
            continue
        pts = stock_maps.get(sid)
        if not pts:
            continue
        xi = _exit_frame_idx_w20_leading(ei, frames, pts, exit_params)
        tr = _trade_return(conn, bench, stock_maps, sid, ei, xi, frames)
        if not tr:
            continue
        tr["breadth_zone_200"] = regime_ctx["zone_by_date"].get(
            prior or ef["date"], "unknown"
        )
        tr["flow_tape_regime"] = regime_ctx["flow_tape_by_date"].get(ef["date"])
        tr["entry_chase_pct"] = round(chase, 3) if chase is not None else None
        trades.append(tr)
    return trades


def run_champion_with_regime_filter(
    conn: sqlite3.Connection,
    *,
    entry_params: EntryParams,
    exit_params: W20LeadingExitParams,
    regime: RegimeFilterParams,
    trajectories: list[dict],
    frames: list[dict[str, str]],
    stock_maps: dict[str, list[dict[str, Any] | None]],
    bench: pd.Series,
    regime_ctx: dict[str, Any],
) -> dict[str, Any]:
    trades = collect_regime_filtered_trades(
        conn,
        entry_params=entry_params,
        exit_params=exit_params,
        regime=regime,
        trajectories=trajectories,
        frames=frames,
        stock_maps=stock_maps,
        bench=bench,
        regime_ctx=regime_ctx,
    )
    base = _summarize_w20_exit_trades(trades, entry_params, exit_params)
    base["regime_filter"] = regime.label()
    base["regime_breadth_zones"] = regime.breadth_zones
    base["regime_flow_tape"] = regime.flow_tape_regimes
    base["regime_max_chase_pct"] = regime.max_entry_chase_pct
    return base


def _stratify_trades_by_zone(trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    from market_breadth_ma import BREADTH_ZONES_ORDER

    buckets: dict[str, list[dict[str, Any]]] = {z: [] for z in BREADTH_ZONES_ORDER}
    buckets["unknown"] = []
    for t in trades:
        z = str(t.get("breadth_zone_200") or "unknown")
        buckets.setdefault(z, []).append(t)

    out: dict[str, dict[str, Any]] = {}
    for zone, legs in buckets.items():
        if not legs:
            continue
        n = len(legs)
        excess = [t["excess_pct"] for t in legs]
        out[zone] = {
            "n": n,
            "mean_excess_pct": round(sum(excess) / n, 3),
            "win_rate": round(100 * sum(1 for t in legs if t["win"]) / n, 1),
        }
    return out


def run_phase3_imp_early_long_regime_sweep(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...],
    entry_params: EntryParams | None = None,
    exit_params: W20LeadingExitParams | None = None,
) -> dict[str, Any]:
    """Phase 3 · 凍結 champion 進出場 × Regime layer 濾網 sweep。"""
    ep = entry_params or imp_early_long_champion_entry()
    xp = exit_params or default_w20_leading_exit()
    regime_grid = regime_filter_grid()

    print("  building dual WMA trajectories (once) ...", flush=True)
    frames, trajectories, _meta = build_dual_wma_intraday_trajectories(
        conn, dates=dates, etf_codes=etf_codes
    )
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    stock_maps = _build_stock_frame_maps(trajectories, frames)
    regime_ctx = build_regime_filter_context(conn, dates)

    rows: list[dict[str, Any]] = []
    total = len(regime_grid)
    for i, rp in enumerate(regime_grid, 1):
        if i % 5 == 0 or i == total:
            print(f"  phase3 regime {i}/{total} {rp.label()} ...", flush=True)
        rows.append(
            run_champion_with_regime_filter(
                conn,
                entry_params=ep,
                exit_params=xp,
                regime=rp,
                trajectories=trajectories,
                frames=frames,
                stock_maps=stock_maps,
                bench=bench,
                regime_ctx=regime_ctx,
            )
        )

    baseline_trades: list[dict[str, Any]] = []
    events = _collect_events(trajectories, frames, ep)
    for sid, ei in events:
        ef = frames[ei]
        prior = regime_ctx["prior_by_date"].get(ef["date"])
        pts = stock_maps.get(sid)
        if not pts:
            continue
        xi = _exit_frame_idx_w20_leading(ei, frames, pts, xp)
        tr = _trade_return(conn, bench, stock_maps, sid, ei, xi, frames)
        if tr:
            tr["breadth_zone_200"] = regime_ctx["zone_by_date"].get(
                prior or ef["date"], "unknown"
            )
            tr["flow_tape_regime"] = regime_ctx["flow_tape_by_date"].get(ef["date"])
            baseline_trades.append(tr)

    by_excess = sorted(
        [r for r in rows if r.get("n", 0) >= 3],
        key=lambda r: (r.get("mean_excess_pct") or -999, r.get("n", 0)),
        reverse=True,
    )
    baseline_row = next(
        (
            r
            for r in rows
            if not r.get("regime_breadth_zones")
            and not r.get("regime_flow_tape")
            and r.get("regime_max_chase_pct") is None
        ),
        None,
    )
    return {
        "dates": dates,
        "focus_signal": "imp_early_long_phase3",
        "champion_entry": ep,
        "champion_exit": xp,
        "rows": rows,
        "top_by_excess": by_excess[:15],
        "baseline": baseline_row,
        "stratify_by_zone": _stratify_trades_by_zone(baseline_trades),
    }


def render_phase3_regime_md(payload: dict[str, Any]) -> str:
    dates = payload["dates"]
    ep = payload["champion_entry"]
    xp = payload["champion_exit"]
    base = payload.get("baseline") or {}
    lines = [
        "# Dual WMA imp_early_long · Phase 3 Regime layer 濾網",
        "",
        f"期間：**{dates[0]} → {dates[-1]}** · 成本 round-trip **{ROUND_TRIP_COST_PCT}%**",
        "",
        "## 凍結規格",
        "",
        f"- 進場：small_ext={ep.small_extension_max} · aligned={ep.aligned_max} · "
        f"both_vec={ep.require_both_vectors} · W20 RS {ep.w20_rs_min}–{ep.w20_rs_max}",
        f"- 出場：{xp.label()}",
        f"- 基線（無濾網）：n={base.get('n')} · 超額 **{base.get('mean_excess_pct')}%** · "
        f"勝率 {base.get('win_rate')}%",
        "",
        "## 濾網排行（n≥3 · 依平均超額報酬）",
        "",
        "| 濾網 | n | 超額% | 中位超額% | 勝率 | 超額勝率 | P10淨% |",
        "|------|---|-------|-----------|------|----------|--------|",
    ]
    ref = base.get("mean_excess_pct")
    for r in payload["top_by_excess"]:
        delta = ""
        if ref is not None and r.get("mean_excess_pct") is not None:
            d = round(r["mean_excess_pct"] - ref, 3)
            delta = f" ({'+' if d >= 0 else ''}{d})"
        lines.append(
            f"| {r.get('regime_filter')} | {r['n']} | "
            f"{r.get('mean_excess_pct')}{delta} | {r.get('median_excess_pct')} | "
            f"{r.get('win_rate')}% | {r.get('excess_win_rate')}% | {r.get('p10_net_pct')} |"
        )

    strat = payload.get("stratify_by_zone") or {}
    if strat:
        lines.extend(
            [
                "",
                "## 基線分層 · Market breadth zone（信號日前一日）",
                "",
                "| zone | n | 超額% | 勝率 |",
                "|------|---|-------|------|",
            ]
        )
        for zone, s in sorted(strat.items(), key=lambda x: -(x[1].get("n") or 0)):
            lines.append(
                f"| {zone} | {s['n']} | {s.get('mean_excess_pct')} | {s.get('win_rate')}% |"
            )

    best = payload["top_by_excess"][0] if payload["top_by_excess"] else {}
    lines.extend(
        [
            "",
            "## Phase 3 假說",
            "",
            "| ID | 假說 | 結果 |",
            "|----|------|------|",
        ]
    )
    h4a = (
        best.get("regime_filter") != "無濾網（baseline）"
        and best.get("mean_excess_pct") is not None
        and ref is not None
        and best["mean_excess_pct"] > ref
        and best.get("n", 0) >= 5
    )
    chase_1 = next(
        (r for r in payload["rows"] if r.get("regime_max_chase_pct") == 1.0 and not r.get("regime_breadth_zones") and not r.get("regime_flow_tape")),
        {},
    )
    h4b = (
        chase_1.get("mean_excess_pct") is not None
        and ref is not None
        and chase_1["mean_excess_pct"] >= ref * 0.95
    )
    tape_rows = [r for r in payload["rows"] if r.get("regime_flow_tape")]
    best_tape = max(tape_rows, key=lambda r: r.get("mean_excess_pct") or -999, default={})
    h4c = (
        best_tape.get("mean_excess_pct") is not None
        and ref is not None
        and best_tape["mean_excess_pct"] > ref
    )
    lines.append(
        f"| H4a | Regime 濾網提升 mean 超額且 n≥5 | "
        f"{'supported' if h4a else 'rejected'} |"
    )
    lines.append(
        f"| H4b | 排除 chase（≤1%）不犧牲超額 | "
        f"{'supported' if h4b else 'partial'} |"
    )
    lines.append(
        f"| H4c | flow tape 多頭/震盪濾網優於無濾網 | "
        f"{'supported' if h4c else 'rejected'} |"
    )

    if best:
        lines.extend(
            [
                "",
                "## 建議",
                "",
                f"- 最佳濾網：`{best.get('regime_filter')}` · 超額 **{best.get('mean_excess_pct')}%** · n={best.get('n')}",
            ]
        )

    lines.extend(
        [
            "",
            "## 解讀備註",
            "",
            "- **Market breadth zone** · **flow tape regime** 皆取信號日 **前一交易日** PIT 快照。",
            "- **chase** = 進場價相對前日收盤漲幅；對齊 lifecycle S2 排除已 chase 標的。",
            "- n 偏小時僅供方向；若濾網有效可寫入 research 採納候選。",
        ]
    )
    return "\n".join(lines) + "\n"


IS_START_DEFAULT = "2026-04-01"
IS_END_DEFAULT = "2026-07-03"

OOS_HOLDOUT_WINDOWS: tuple[tuple[str, str, str], ...] = (
    ("IS", IS_START_DEFAULT, IS_END_DEFAULT),
    ("OOS_2026Q1", "2026-01-01", "2026-03-31"),
    ("OOS_2025H2", "2025-07-01", "2025-12-31"),
)

MIN_OOS_EVENTS = 5


def imp_early_long_frozen_regime() -> RegimeFilterParams:
    return RegimeFilterParams(flow_tape_regimes=("多頭",))


def _load_dates_in_range(
    conn: sqlite3.Connection,
    *,
    date_from: str,
    date_to: str,
) -> list[str]:
    bench = load_benchmark_close(conn)
    return [d for d in bench.index.astype(str).tolist() if date_from <= d <= date_to]


def _run_frozen_window(
    conn: sqlite3.Connection,
    *,
    label: str,
    date_from: str,
    date_to: str,
    etf_codes: tuple[str, ...],
    entry_params: EntryParams,
    exit_params: W20LeadingExitParams,
    regime: RegimeFilterParams | None,
) -> dict[str, Any]:
    dates = _load_dates_in_range(conn, date_from=date_from, date_to=date_to)
    if len(dates) < 3:
        return {
            "window": label,
            "date_from": date_from,
            "date_to": date_to,
            "error": "insufficient_trade_dates",
            "n_trade_dates": len(dates),
        }
    print(f"  OOS window {label} {date_from} → {date_to} ({len(dates)}d) ...", flush=True)
    frames, trajectories, _meta = build_dual_wma_intraday_trajectories(
        conn, dates=dates, etf_codes=etf_codes
    )
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    stock_maps = _build_stock_frame_maps(trajectories, frames)
    regime_ctx = build_regime_filter_context(conn, dates)
    rp = regime or RegimeFilterParams()
    row = run_champion_with_regime_filter(
        conn,
        entry_params=entry_params,
        exit_params=exit_params,
        regime=rp,
        trajectories=trajectories,
        frames=frames,
        stock_maps=stock_maps,
        bench=bench,
        regime_ctx=regime_ctx,
    )
    row["window"] = label
    row["date_from"] = date_from
    row["date_to"] = date_to
    row["n_trade_dates"] = len(dates)
    return row


def run_phase4_oos_holdout(
    conn: sqlite3.Connection,
    *,
    etf_codes: tuple[str, ...],
    windows: tuple[tuple[str, str, str], ...] | None = None,
) -> dict[str, Any]:
    """Phase 4 · 凍結 champion + tape=多頭 · IS vs OOS holdout。"""
    ep = imp_early_long_champion_entry()
    xp = default_w20_leading_exit()
    frozen_regime = imp_early_long_frozen_regime()
    wins = windows or OOS_HOLDOUT_WINDOWS

    rows: list[dict[str, Any]] = []
    for label, d0, d1 in wins:
        rows.append(
            _run_frozen_window(
                conn,
                label=label,
                date_from=d0,
                date_to=d1,
                etf_codes=etf_codes,
                entry_params=ep,
                exit_params=xp,
                regime=frozen_regime,
            )
        )
        rows.append(
            _run_frozen_window(
                conn,
                label=f"{label}_nofilter",
                date_from=d0,
                date_to=d1,
                etf_codes=etf_codes,
                entry_params=ep,
                exit_params=xp,
                regime=RegimeFilterParams(),
            )
        )

    is_row = next((r for r in rows if r.get("window") == "IS"), {})
    oos_rows = [
        r
        for r in rows
        if r.get("window", "").startswith("OOS_") and not r.get("window", "").endswith("_nofilter")
    ]
    g2_pass = True
    g2_notes: list[str] = []
    for oos in oos_rows:
        n = oos.get("n", 0)
        excess = oos.get("mean_excess_pct")
        if n < MIN_OOS_EVENTS:
            g2_pass = False
            g2_notes.append(f"{oos.get('window')}: n={n} < {MIN_OOS_EVENTS}")
        elif excess is None or excess <= 0:
            g2_pass = False
            g2_notes.append(f"{oos.get('window')}: excess={excess}% ≤ 0")
    is_excess = is_row.get("mean_excess_pct")
    for oos in oos_rows:
        if (
            is_excess is not None
            and oos.get("mean_excess_pct") is not None
            and is_excess > 0
            and oos["mean_excess_pct"] < is_excess * 0.3
            and oos.get("n", 0) >= MIN_OOS_EVENTS
        ):
            g2_notes.append(
                f"{oos.get('window')}: OOS excess {oos['mean_excess_pct']}% "
                f"<< IS {is_excess}%"
            )

    return {
        "focus_signal": "imp_early_long_phase4_oos",
        "frozen_entry": ep,
        "frozen_exit": xp,
        "frozen_regime": frozen_regime,
        "windows": wins,
        "rows": rows,
        "is": is_row,
        "oos": oos_rows,
        "top_by_excess": sorted(
            [r for r in rows if not r.get("error")],
            key=lambda r: (r.get("mean_excess_pct") is None, -(r.get("mean_excess_pct") or -999)),
        ),
        "g2_oos_holdout": {
            "passed": g2_pass and len(g2_notes) == 0,
            "min_events": MIN_OOS_EVENTS,
            "notes": g2_notes,
        },
    }


def render_phase4_oos_md(payload: dict[str, Any]) -> str:
    ep = payload["frozen_entry"]
    xp = payload["frozen_exit"]
    fr = payload["frozen_regime"]
    g2 = payload.get("g2_oos_holdout") or {}
    lines = [
        "# Dual WMA imp_early_long · Phase 4 OOS Holdout",
        "",
        "## 凍結規格（Phase 1–3 · 不再調參）",
        "",
        f"- 進場：small_ext={ep.small_extension_max} · aligned={ep.aligned_max} · "
        f"both_vec={ep.require_both_vectors} · W20 RS {ep.w20_rs_min}–{ep.w20_rs_max}",
        f"- 出場：{xp.label()}",
        f"- Regime：`{fr.label()}`",
        "",
        "## IS / OOS 視窗",
        "",
        "| 視窗 | 期間 | 濾網 | n | 超額% | 中位超額% | 勝率 | 超額勝率 |",
        "|------|------|------|---|-------|-----------|------|----------|",
    ]
    for r in payload["rows"]:
        if r.get("error"):
            lines.append(
                f"| {r.get('window')} | {r.get('date_from')}→{r.get('date_to')} | "
                f"— | — | _{r.get('error')}_ | — | — | — |"
            )
            continue
        filt = "tape=多頭" if not str(r.get("window", "")).endswith("_nofilter") else "無"
        lines.append(
            f"| {r.get('window')} | {r.get('date_from')}→{r.get('date_to')} | "
            f"{filt} | {r.get('n')} | {r.get('mean_excess_pct')} | "
            f"{r.get('median_excess_pct')} | {r.get('win_rate')}% | "
            f"{r.get('excess_win_rate')}% |"
        )

    lines.extend(
        [
            "",
            "## G2 採納門檻（OOS holdout）",
            "",
            f"- **結果**：{'**PASS**' if g2.get('passed') else '**FAIL**'}",
            f"- 條件：各 OOS 視窗 n≥{g2.get('min_events')} 且 mean 超額 > 0",
        ]
    )
    for note in g2.get("notes") or []:
        lines.append(f"- ⚠ {note}")

    is_f = payload.get("is") or {}
    lines.extend(
        [
            "",
            "## 解讀",
            "",
            f"- IS 基準（tape=多頭）：n={is_f.get('n')} · 超額 **{is_f.get('mean_excess_pct')}%**",
            "- 比較同視窗 `_nofilter` 列可見 Regime 濾網在 OOS 是否仍有效。",
            "- 1m kbar 覆蓋：2025H2 與 2026Q1 皆有盤中資料；IS 為調參期。",
        ]
    )
    return "\n".join(lines) + "\n"


def default_lead_pullback_entry() -> EntryParams:
    """Frozen spec · config/research.yaml `lead_pullback_buy_default`."""
    return EntryParams()


def default_lead_pullback_exit() -> ExitRule:
    """Research exit · config/research.yaml `exit_research: hold_5d`."""
    return ExitRule("hold_5d", "持有 5 交易日 → 13:30")


def _collect_lead_pullback_events(
    trajectories: list[dict[str, Any]],
    frames: list[dict[str, str]],
    ep: EntryParams,
) -> list[dict[str, Any]]:
    frame_ids = [f["id"] for f in frames]
    events: list[dict[str, Any]] = []
    seen_day: set[tuple[str, str]] = set()
    for t in trajectories:
        sid = str(t["stock_id"])
        name = str(t.get("stock_name") or "")
        by_id = {p["frame_id"]: p for p in t.get("points") or []}
        for i, fid in enumerate(frame_ids):
            p = by_id.get(fid)
            if not p or not _matches_entry(p, ep):
                continue
            d = frames[i]["date"]
            if ep.first_per_day:
                key = (sid, d)
                if key in seen_day:
                    continue
                seen_day.add(key)
            events.append(
                {
                    "stock_id": sid,
                    "stock_name": name,
                    "frame_idx": i,
                    "signal_date": d,
                    "spread_dist": round(float(p.get("spread_dist") or 0), 2),
                }
            )
    events.sort(key=lambda e: (e["frame_idx"], e["stock_id"]))
    return events


def build_lead_pullback_executed_legs_for_timeline(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    n_slots: int = 3,
    capital_ntd: float = 10_000.0,
    entry_params: EntryParams | None = None,
    exit_rule: ExitRule | None = None,
    etf_codes: tuple[str, ...] | None = None,
    _cache: dict[str, Any] | None = None,
) -> tuple[list[dict], list[dict], list[dict], dict]:
    """3 槽 lead_pullback_buy · hold_5d → timeline legs（render_l1h9_slots_timeline_html）。"""
    from project_config import DEFAULT_ETF_CODES

    ep = entry_params or default_lead_pullback_entry()
    ex = exit_rule or default_lead_pullback_exit()
    codes = etf_codes or DEFAULT_ETF_CODES

    if _cache is not None:
        frames = _cache["frames"]
        trajectories = _cache["trajectories"]
        bench = _cache["bench"]
        stock_maps = _cache["stock_maps"]
    else:
        frames, trajectories, _meta = build_dual_wma_intraday_trajectories(
            conn, dates=dates, etf_codes=codes
        )
        bench = load_benchmark_close(conn).reindex(
            load_price_panels(conn)[0].index
        ).astype(float)
        stock_maps = _build_stock_frame_maps(trajectories, frames)

    events = _collect_lead_pullback_events(trajectories, frames, ep)
    open_pos: list[dict[str, Any]] = []
    legs_out: list[dict] = []
    executed_signals: list[dict] = []
    skipped_signals: list[dict] = []
    peak = 0

    def _release_through(frame_idx: int) -> None:
        nonlocal open_pos
        open_pos = [p for p in open_pos if p["exit_idx"] > frame_idx]

    for ev in events:
        ei = int(ev["frame_idx"])
        sid = ev["stock_id"]
        _release_through(ei)
        used = {p["slot_id"] for p in open_pos}
        held = {p["stock_id"] for p in open_pos}
        free = [s for s in range(n_slots) if s not in used]
        pts = stock_maps.get(sid)
        exit_idx = (
            _exit_frame_idx(ex.rule_id, ei, frames, pts, aligned_max=ep.aligned_max)
            if pts
            else ei
        )
        exit_date = frames[exit_idx]["date"]

        if sid in held:
            skipped_signals.append(
                {
                    "signal_date": ev["signal_date"],
                    "entry_date": frames[ei]["date"],
                    "exit_date": exit_date,
                    "stock_id": sid,
                    "stock_name": ev["stock_name"],
                    "seg_last": ev["spread_dist"],
                    "n_legs": 1,
                    "return_pct": None,
                    "reason": "duplicate_stock",
                }
            )
            continue
        if not free:
            skipped_signals.append(
                {
                    "signal_date": ev["signal_date"],
                    "entry_date": frames[ei]["date"],
                    "exit_date": exit_date,
                    "stock_id": sid,
                    "stock_name": ev["stock_name"],
                    "seg_last": ev["spread_dist"],
                    "n_legs": 1,
                    "return_pct": None,
                    "reason": "slots_full",
                }
            )
            continue

        tr = _trade_return(conn, bench, stock_maps, sid, ei, exit_idx, frames)
        if not tr:
            skipped_signals.append(
                {
                    "signal_date": ev["signal_date"],
                    "entry_date": frames[ei]["date"],
                    "exit_date": exit_date,
                    "stock_id": sid,
                    "stock_name": ev["stock_name"],
                    "seg_last": ev["spread_dist"],
                    "n_legs": 1,
                    "return_pct": None,
                    "reason": "no_price",
                }
            )
            continue

        slot_id = free[0]
        ret = float(tr["net_pct"])
        excess = float(tr["excess_pct"])
        pnl = capital_ntd * ret / 100.0
        alpha_ntd = capital_ntd * excess / 100.0
        entry_date = tr["entry_date"]
        open_pos.append({"slot_id": slot_id, "stock_id": sid, "exit_idx": exit_idx})
        peak = max(peak, len(open_pos))

        leg = {
            "leg_id": f"{entry_date}|{sid}|{slot_id}|{tr['entry_minute']}",
            "signal_date": ev["signal_date"],
            "stock_id": sid,
            "stock_name": ev["stock_name"],
            "action": "lead_pullback_buy",
            "entry_date": entry_date,
            "exit_date": tr["exit_date"],
            "entry_minute": tr["entry_minute"],
            "exit_minute": tr["exit_minute"],
            "entry_px": tr["entry_px"],
            "exit_px": tr["exit_px"],
            "allocated_ntd": capital_ntd,
            "return_pct": ret,
            "pnl_ntd": pnl,
            "slot_id": slot_id,
            "seg_last": ev["spread_dist"],
            "exit_reason": ex.rule_id,
        }
        executed = {
            "signal_date": ev["signal_date"],
            "entry_date": entry_date,
            "exit_date": tr["exit_date"],
            "slot_id": slot_id,
            "n_legs": 1,
            "stock_id": sid,
            "stock_name": ev["stock_name"],
            "seg_last": ev["spread_dist"],
            "deployed_ntd": capital_ntd,
            "pnl_ntd": pnl,
            "return_pct": ret,
            "entry_px": tr["entry_px"],
            "exit_px": tr["exit_px"],
            "bench_return_pct": tr.get("bench_return_pct"),
            "alpha_ntd": alpha_ntd,
            "exit_reason": ex.rule_id,
        }
        legs_out.append(leg)
        executed_signals.append(executed)

    from research.backtest.copytrade_backtest import _bench_close, bench_return_entry_to_exit

    for leg in legs_out:
        entry = str(leg["entry_date"])
        exit_d = str(leg["exit_date"])
        b0 = _bench_close(conn, entry)
        bench_ret = bench_return_entry_to_exit(
            conn, entry, exit_d, entry_price_mode="close"
        )
        leg["bench_entry_px"] = round(b0, 4) if b0 is not None else None
        leg["bench_return_pct"] = round(bench_ret, 4) if bench_ret is not None else None

    n_executed = len(executed_signals)
    n_signals = n_executed + len(skipped_signals)
    total_capital = n_slots * capital_ntd
    meta = {
        "n_slots": n_slots,
        "capital_ntd": capital_ntd,
        "total_capital_ntd": total_capital,
        "cost_bps": ROUND_TRIP_COST_PCT * 100.0,
        "hold_trading_days": 5,
        "min_hold_days": 0,
        "n_signals": n_signals,
        "n_executed": n_executed,
        "n_skipped": len(skipped_signals),
        "signal_capture_pct": round(100.0 * n_executed / n_signals, 2) if n_signals else None,
        "peak_concurrent_slots": peak,
        "table_mode": "lead_pullback",
        "entry_price_mode": "intraday",
        "strategy_title": "強勢回踩 · 預設規格 · 3槽",
        "strategy_rule": (
            "W20 Leading + pullback spread≥2 · fresh K · "
            f"{ex.label} · 盤中 poll 進出"
        ),
        "strategy_filter": "lead_pullback_buy · 預設 · hold_5d · advisory 規格",
        "display_code": "LPB-DEFAULT",
        "exit_rule": ex.rule_id,
    }
    return legs_out, executed_signals, skipped_signals, meta
