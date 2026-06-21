"""Chunge funnel slot backtest — hold7 · entry_ready hold20 · entry_ready pivot/stop."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from flow_returns import return_pct, stock_close, stock_high, stock_low, stock_open, trading_dates_after
from .finpilot_local_backtest import load_price_panels, summarize_periods
from .rrg_mono_backtest import _close_trade
from rrg_mono_daily_brief import _backfill_exit_dates, _exit_date_from_entry, _expire_slots
from .slot_backtest_summary import SlotBacktestConfig
from market_breadth_ma import BREADTH_ZONE_DISPLAY, BREADTH_ZONE_ZH, BREADTH_ZONES_ORDER, build_breadth_panel
from stock_db import DEFAULT_DB_PATH, connect, load_vcp_screen_v2_for_date

from vcp_funnel_screen import FUNNEL_MODEL_IDS, MODEL_ID as VCP_FUNNEL_MODEL_ID

BreadthZoneFilter = Literal["oversold", "weak", "neutral", "strong", "overbought"] | None

MODEL_ID = VCP_FUNNEL_MODEL_ID
LEGACY_MODEL_IDS = tuple(m for m in FUNNEL_MODEL_IDS if m != MODEL_ID)
DEFAULT_EXECUTION_STATES = (
    "Pre-breakout",
    "Breakout",
    "Overextended",
    "Extended",
)
ENTRY_READY_EXECUTION_STATES = (
    "Pre-breakout",
    "Breakout",
)
# vcp-tm / Minervini Section A · tradermonty lineage
MINERVINI_SECTION_A_STATES = ("Pre-breakout", "Breakout")
MINERVINI_NEAR_PIVOT_STATES = ("Pre-breakout", "Breakout", "Early-post-breakout")
MINERVINI_MAX_ABOVE_PIVOT = 8.0
MINERVINI_MAX_BELOW_PIVOT = -8.0
ENTRY_READY_HOLD20_DEFAULTS = {
    "n_slots": 5,
    "hold_days": 20,
    "entry_ready_only": True,
    "execution_states": ENTRY_READY_EXECUTION_STATES,
    "variant": "entry_ready_hold20",
}
ENTRY_READY_PIVOT_STOP_DEFAULTS = {
    "n_slots": 5,
    "hold_days": 20,
    "entry_ready_only": True,
    "execution_states": ENTRY_READY_EXECUTION_STATES,
    "variant": "entry_ready_pivot_stop",
    "entry_price_mode": "pivot_stop",
    "max_entry_wait_days": 10,
    "stop_lookback_days": 20,
}
VCP_PIVOT_GATE_VARIANT = "vcp-pivot-gate-h20"
VCP_COIL_CLOSE_VARIANT = "vcp-coil-close-h20"
_LEGACY_VARIANT_ALIASES = {
    "chunge_minervini_calibrated": VCP_PIVOT_GATE_VARIANT,
    "chunge_coil_close": VCP_COIL_CLOSE_VARIANT,
}

VCP_PIVOT_GATE = {
    "n_slots": 5,
    "hold_days": 20,
    "min_composite": 45.0,
    "entry_ready_only": False,
    "execution_states": MINERVINI_NEAR_PIVOT_STATES,
    "require_pivot": True,
    "min_dist_pivot_pct": MINERVINI_MAX_BELOW_PIVOT,
    "max_dist_pivot_pct": 5.0,
    "entry_price_mode": "breakout_close",
    "max_entry_wait_days": 10,
    "stop_lookback_days": 20,
    "variant": VCP_PIVOT_GATE_VARIANT,
}
VCP_COIL_CLOSE = {
    "n_slots": 5,
    "hold_days": 20,
    "min_composite": 45.0,
    "entry_ready_only": False,
    "execution_states": MINERVINI_NEAR_PIVOT_STATES,
    "require_pivot": True,
    "min_dist_pivot_pct": MINERVINI_MAX_BELOW_PIVOT,
    "max_dist_pivot_pct": 5.0,
    "entry_price_mode": "close",
    "max_entry_wait_days": 0,
    "stop_lookback_days": 20,
    "variant": VCP_COIL_CLOSE_VARIANT,
}


def normalize_variant(variant: str) -> str:
    return _LEGACY_VARIANT_ALIASES.get(variant, variant)


def is_vcp_pivot_gate_variant(variant: str) -> bool:
    return normalize_variant(variant) == VCP_PIVOT_GATE_VARIANT


def is_vcp_coil_close_variant(variant: str, *, entry_mode: str = "", entry_ready: bool = False) -> bool:
    if normalize_variant(variant) == VCP_COIL_CLOSE_VARIANT:
        return True
    return (
        entry_mode == "close"
        and not entry_ready
        and variant not in ("hold7", "entry_ready_hold20", VCP_PIVOT_GATE_VARIANT)
        and normalize_variant(variant) not in (VCP_PIVOT_GATE_VARIANT,)
    )


@dataclass(frozen=True)
class ChungeCandidate:
    stock_id: str
    stock_name: str
    composite_score: float
    execution_state: str
    entry_ready: bool = False
    pivot_price: float | None = None
    stop_loss: float | None = None
    distance_from_pivot_pct: float | None = None


def build_chunge_candidates_calendar(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    model_id: str = MODEL_ID,
    min_composite: float = 45.0,
    execution_states: tuple[str, ...] = DEFAULT_EXECUTION_STATES,
    entry_ready_only: bool = False,
    require_pivot: bool = False,
    min_dist_pivot_pct: float | None = None,
    max_dist_pivot_pct: float | None = None,
) -> dict[str, list[ChungeCandidate]]:
    out: dict[str, list[ChungeCandidate]] = {d: [] for d in trade_dates}
    for as_of in trade_dates:
        rows = load_vcp_screen_v2_for_date(
            conn,
            as_of,
            model_id=model_id,
            min_score=min_composite,
            execution_states=execution_states,
        )
        if not rows and model_id == MODEL_ID:
            for legacy_id in LEGACY_MODEL_IDS:
                rows = load_vcp_screen_v2_for_date(
                    conn,
                    as_of,
                    model_id=legacy_id,
                    min_score=min_composite,
                    execution_states=execution_states,
                )
                if rows:
                    break
        candidates: list[ChungeCandidate] = []
        for r in rows:
            ready = bool(int(r["entry_ready"] or 0))
            if entry_ready_only and not ready:
                continue
            dist = float(r["distance_from_pivot_pct"]) if r["distance_from_pivot_pct"] is not None else None
            pivot = float(r["pivot_price"]) if r["pivot_price"] else None
            if require_pivot and (pivot is None or pivot <= 0):
                continue
            if min_dist_pivot_pct is not None and dist is not None and dist < min_dist_pivot_pct:
                continue
            if max_dist_pivot_pct is not None and dist is not None and dist > max_dist_pivot_pct:
                continue
            candidates.append(
                ChungeCandidate(
                    stock_id=str(r["stock_id"]),
                    stock_name=str(r["stock_name"] or ""),
                    composite_score=float(r["composite_score"] or 0.0),
                    execution_state=str(r["execution_state"] or ""),
                    entry_ready=ready,
                    pivot_price=pivot,
                    stop_loss=float(r["stop_loss"]) if r["stop_loss"] else None,
                    distance_from_pivot_pct=dist,
                )
            )
        candidates.sort(key=lambda c: (-c.composite_score, c.stock_id))
        out[as_of] = candidates
    return out


def _summarize(periods: list[dict]) -> dict:
    summary = summarize_periods(periods)
    if periods:
        summary["mean_excess_pct"] = round(
            sum(p["excess_pct"] for p in periods) / len(periods), 4
        )
        summary["total_return_pct"] = round(sum(p["return_pct"] for p in periods), 4)
        summary["total_excess_pct"] = round(sum(p["excess_pct"] for p in periods), 4)
    else:
        summary["mean_excess_pct"] = None
        summary["total_return_pct"] = None
        summary["total_excess_pct"] = None
    return summary


def _resolve_stop_loss(
    conn: sqlite3.Connection,
    stock_id: str,
    signal_date: str,
    pivot_price: float,
    *,
    db_stop: float | None = None,
    lookback_days: int = 20,
    full_dates: list[str] | None = None,
) -> float | None:
    if db_stop is not None and db_stop > 0:
        return round(db_stop, 2)
    if full_dates:
        eligible = [d for d in full_dates if d <= signal_date]
        prior = eligible[-lookback_days:]
    else:
        rows = conn.execute(
            """
            SELECT DISTINCT trade_date AS d
            FROM stock_daily_bars
            WHERE source = 'finmind' AND trade_date <= ?
            ORDER BY d DESC
            LIMIT ?
            """,
            (signal_date, lookback_days),
        ).fetchall()
        prior = [str(r["d"]) for r in reversed(rows)]
    lows = [stock_low(conn, stock_id, d) for d in prior]
    valid = [lv for lv in lows if lv is not None and lv > 0]
    if valid:
        return round(min(valid) * 0.99, 2)
    if pivot_price > 0:
        return round(pivot_price * 0.93, 2)
    return None


def _pivot_breakout_entry_px(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    pivot_price: float,
) -> float | None:
    high = stock_high(conn, stock_id, trade_date)
    if high is None or high < pivot_price:
        return None
    open_px = stock_open(conn, stock_id, trade_date)
    if open_px is not None and open_px >= pivot_price:
        return round(open_px, 2)
    return round(pivot_price, 2)


def _stop_hit_exit_px(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    stop_loss: float,
) -> float | None:
    low = stock_low(conn, stock_id, trade_date)
    if low is None or low > stop_loss:
        return None
    open_px = stock_open(conn, stock_id, trade_date)
    if open_px is not None and open_px <= stop_loss:
        return round(open_px, 2)
    return round(stop_loss, 2)


def _bench_return_close_to_close(
    conn: sqlite3.Connection,
    entry_date: str,
    exit_date: str,
) -> float | None:
    from .copytrade_backtest import _bench_close

    b0 = _bench_close(conn, entry_date)
    b1 = _bench_close(conn, exit_date)
    if b0 is None or b1 is None or b0 <= 0:
        return None
    return return_pct(b0, b1)


def _period_from_trade(
    conn: sqlite3.Connection,
    *,
    stock_id: str,
    stock_name: str,
    signal_date: str,
    entry_date: str,
    exit_date: str,
    entry_px: float,
    exit_px: float,
    composite_score: float | None,
    slot: int | None,
    exit_reason: str,
) -> dict | None:
    if entry_px <= 0 or exit_px <= 0:
        return None
    ret = return_pct(entry_px, exit_px)
    bench = _bench_return_close_to_close(conn, entry_date, exit_date)
    if bench is None:
        return None
    return {
        "stock_id": stock_id,
        "stock_name": stock_name,
        "signal_date": signal_date,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "entry_px": round(entry_px, 4),
        "exit_px": round(exit_px, 4),
        "return_pct": round(ret, 4),
        "bench_return_pct": round(bench, 4),
        "excess_pct": round(ret - bench, 4),
        "beat_bench": ret > bench,
        "gross_win": ret > 0,
        "composite_score": composite_score,
        "slot": slot,
        "exit_reason": exit_reason,
    }


def _breakout_close_entry_px(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    pivot_price: float,
) -> float | None:
    close_px = stock_close(conn, stock_id, trade_date)
    if close_px is None or close_px < pivot_price:
        return None
    return round(close_px, 2)


def simulate_chunge_pivot_stop(
    conn: sqlite3.Connection,
    *,
    trade_dates: list[str],
    full_dates: list[str],
    close: pd.DataFrame,
    candidates_by_date: dict[str, list[ChungeCandidate]],
    n_slots: int = 5,
    hold_days: int = 20,
    top_n: int = 15,
    max_entry_wait_days: int = 10,
    stop_lookback_days: int = 20,
    entry_mode: str = "pivot_stop",
    zone_by_date: dict[str, str] | None = None,
    zone_filter: BreadthZoneFilter = None,
) -> tuple[list[dict], dict]:
    """Pivot/breakout triggered entry · stop-loss or hold-N time exit."""
    zones = zone_by_date or {}
    if entry_mode == "breakout_close":
        fill_fn = _breakout_close_entry_px
        mode_label = "breakout_close"
    else:
        fill_fn = _pivot_breakout_entry_px
        mode_label = "pivot_stop"
    date_idx = {d: i for i, d in enumerate(full_dates)}
    pending: list[dict] = []
    open_positions: list[dict] = []
    periods: list[dict] = []
    signal_days_with_data = 0
    n_pending_expired = 0
    n_stopped = 0
    n_time_exit = 0

    def _occupied_stock_ids() -> set[str]:
        ids = {p["stock_id"] for p in open_positions}
        ids.update(p["stock_id"] for p in pending)
        return ids

    def _used_slots() -> set[int]:
        slots = {int(p["slot"]) for p in open_positions}
        slots.update(int(p["slot"]) for p in pending)
        return slots

    def _close_position(pos: dict, exit_date: str, exit_px: float, reason: str) -> None:
        nonlocal n_stopped, n_time_exit
        row = _period_from_trade(
            conn,
            stock_id=pos["stock_id"],
            stock_name=pos.get("stock_name", ""),
            signal_date=str(pos["signal_date"]),
            entry_date=str(pos["entry_date"]),
            exit_date=exit_date,
            entry_px=float(pos["entry_px"]),
            exit_px=exit_px,
            composite_score=pos.get("composite_score"),
            slot=pos.get("slot"),
            exit_reason=reason,
        )
        if row:
            row["breadth_zone_200"] = zones.get(str(row["entry_date"]), "unknown")
            periods.append(row)
        if reason == "stop":
            n_stopped += 1
        elif reason == "time":
            n_time_exit += 1
        open_positions.remove(pos)

    for as_of in trade_dates:
        # 1) Manage open positions: stop first, then time exit on hold_days
        for pos in list(open_positions):
            entry = str(pos["entry_date"])
            if as_of < entry:
                continue
            stop_px = _stop_hit_exit_px(conn, pos["stock_id"], as_of, float(pos["stop_loss"]))
            if stop_px is not None:
                _close_position(pos, as_of, stop_px, "stop")
                continue
            ei = date_idx.get(entry)
            ai = date_idx.get(as_of)
            if ei is not None and ai is not None and ai >= ei + hold_days:
                exit_px = stock_close(conn, pos["stock_id"], as_of)
                if exit_px is not None and exit_px > 0:
                    _close_position(pos, as_of, exit_px, "time")

        # 2) Try fill pending pivot orders
        for pend in list(pending):
            if as_of < pend["signal_date"]:
                continue
            if as_of > pend["expire_date"]:
                pending.remove(pend)
                n_pending_expired += 1
                continue
            entry_px = fill_fn(
                conn, pend["stock_id"], as_of, float(pend["pivot_price"])
            )
            if entry_px is None:
                continue
            pending.remove(pend)
            open_positions.append(
                {
                    "slot": pend["slot"],
                    "stock_id": pend["stock_id"],
                    "stock_name": pend["stock_name"],
                    "signal_date": pend["signal_date"],
                    "entry_date": as_of,
                    "entry_px": entry_px,
                    "stop_loss": pend["stop_loss"],
                    "composite_score": pend.get("composite_score"),
                }
            )

        # 3) New signals → pending (occupies slot until fill or expire)
        cands = candidates_by_date.get(as_of, [])
        if cands:
            signal_days_with_data += 1
        if zone_filter is not None and zones.get(as_of) != zone_filter:
            continue
        held_ids = _occupied_stock_ids()
        used = _used_slots()
        free = [i for i in range(n_slots) if i not in used]

        for cand in cands[:top_n]:
            if not free:
                break
            if cand.stock_id in held_ids:
                continue
            if cand.pivot_price is None or cand.pivot_price <= 0:
                continue
            stop = _resolve_stop_loss(
                conn,
                cand.stock_id,
                as_of,
                float(cand.pivot_price),
                db_stop=cand.stop_loss,
                lookback_days=stop_lookback_days,
                full_dates=full_dates,
            )
            if stop is None or stop <= 0:
                continue
            expire_dates = trading_dates_after(
                conn, as_of, count=max_entry_wait_days, inclusive_anchor=True
            )
            expire_date = expire_dates[-1] if expire_dates else as_of
            slot = free.pop(0)
            pending.append(
                {
                    "slot": slot,
                    "signal_date": as_of,
                    "expire_date": expire_date,
                    "stock_id": cand.stock_id,
                    "stock_name": cand.stock_name,
                    "pivot_price": float(cand.pivot_price),
                    "stop_loss": stop,
                    "composite_score": round(cand.composite_score, 2),
                }
            )
            held_ids.add(cand.stock_id)

    # End-of-window: close remaining positions at last close inside window
    last = trade_dates[-1] if trade_dates else ""
    for pos in list(open_positions):
        exit_px = stock_close(conn, pos["stock_id"], last)
        if exit_px is not None and exit_px > 0 and str(pos["entry_date"]) <= last:
            _close_position(pos, last, exit_px, "window_end")

    n_pending_expired += len(pending)
    summary = _summarize(periods)
    summary["n_slots"] = n_slots
    summary["hold_days"] = hold_days
    summary["max_entry_wait_days"] = max_entry_wait_days
    summary["signal_days_with_screen"] = signal_days_with_data
    summary["signal_days_with_candidates"] = signal_days_with_data
    summary["n_stopped"] = n_stopped
    summary["n_time_exit"] = n_time_exit
    summary["n_pending_expired"] = n_pending_expired
    summary["entry_price_mode"] = mode_label
    summary["zone_filter"] = zone_filter
    if trade_dates:
        summary["screen_coverage_pct"] = round(
            100.0 * signal_days_with_data / len(trade_dates), 2
        )
    return periods, summary


def simulate_chunge_slots(
    conn: sqlite3.Connection,
    *,
    trade_dates: list[str],
    full_dates: list[str],
    close: pd.DataFrame,
    candidates_by_date: dict[str, list[ChungeCandidate]],
    n_slots: int = 3,
    hold_days: int = 7,
    top_n: int = 15,
    zone_by_date: dict[str, str] | None = None,
    zone_filter: BreadthZoneFilter = None,
) -> tuple[list[dict], dict]:
    zones = zone_by_date or {}
    state: dict = {"slots": [], "history": []}
    periods: list[dict] = []
    signal_days_with_data = 0

    for as_of in trade_dates:
        expired = _expire_slots(state, as_of)
        _backfill_exit_dates(conn, state, full_dates)
        for pos in expired:
            row = _close_trade(conn, close, pos)
            if row is None:
                continue
            row["signal_date"] = row["entry_date"]
            row["composite_score"] = pos.get("composite_score")
            row["breadth_zone_200"] = zones.get(str(row["entry_date"]), "unknown")
            periods.append(row)

        cands = candidates_by_date.get(as_of, [])
        if cands:
            signal_days_with_data += 1

        if zone_filter is not None and zones.get(as_of) != zone_filter:
            continue

        held = {p["stock_id"] for p in state.get("slots", [])}
        used = {int(p["slot"]) for p in state.get("slots", [])}
        free = [i for i in range(n_slots) if i not in used]

        for cand in cands[:top_n]:
            if not free:
                break
            if cand.stock_id in held:
                continue
            exit_d = _exit_date_from_entry(conn, full_dates, as_of, hold_days) or ""
            slot = free.pop(0)
            pos = {
                "slot": slot,
                "stock_id": cand.stock_id,
                "stock_name": cand.stock_name,
                "entry_date": as_of,
                "exit_date": exit_d,
                "composite_score": round(cand.composite_score, 2),
            }
            if not exit_d:
                pos["exit_pending"] = True
            state.setdefault("slots", []).append(pos)
            held.add(cand.stock_id)

    for pos in list(state.get("slots", [])):
        exit_d = str(pos.get("exit_date") or "")
        if exit_d and exit_d <= trade_dates[-1]:
            row = _close_trade(conn, close, pos)
            if row:
                row["signal_date"] = row["entry_date"]
                row["composite_score"] = pos.get("composite_score")
                row["breadth_zone_200"] = zones.get(str(row["entry_date"]), "unknown")
                periods.append(row)

    summary = _summarize(periods)
    summary["n_slots"] = n_slots
    summary["hold_days"] = hold_days
    summary["signal_days_with_screen"] = signal_days_with_data
    summary["signal_days_with_candidates"] = signal_days_with_data
    summary["zone_filter"] = zone_filter
    if trade_dates:
        summary["screen_coverage_pct"] = round(
            100.0 * signal_days_with_data / len(trade_dates), 2
        )
    return periods, summary


# Backward-compatible alias
simulate_chunge_hold7 = simulate_chunge_slots


def _simulate_vcp_spec(
    conn: sqlite3.Connection,
    *,
    spec: dict,
    trade_dates: list[str],
    full_dates: list[str],
    close: pd.DataFrame,
    candidates_by_date: dict[str, list[ChungeCandidate]],
    zone_by_date: dict[str, str],
    zone_filter: BreadthZoneFilter = None,
) -> tuple[list[dict], dict]:
    mode = spec["entry_price_mode"]
    if mode in ("pivot_stop", "breakout_close"):
        return simulate_chunge_pivot_stop(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            candidates_by_date=candidates_by_date,
            n_slots=spec["n_slots"],
            hold_days=spec["hold_days"],
            max_entry_wait_days=spec.get("max_entry_wait_days", 10),
            stop_lookback_days=spec.get("stop_lookback_days", 20),
            entry_mode=mode,
            zone_by_date=zone_by_date,
            zone_filter=zone_filter,
        )
    return simulate_chunge_slots(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        candidates_by_date=candidates_by_date,
        n_slots=spec["n_slots"],
        hold_days=spec["hold_days"],
        zone_by_date=zone_by_date,
        zone_filter=zone_filter,
    )


def run_vcp_breadth_zone_comparison(
    conn: sqlite3.Connection | None = None,
    *,
    spec: dict,
    date_start: str,
    date_end: str,
) -> dict:
    own = conn is None
    if own:
        conn = connect(DEFAULT_DB_PATH)

    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]

    candidates = build_chunge_candidates_calendar(
        conn,
        trade_dates,
        min_composite=spec["min_composite"],
        execution_states=spec["execution_states"],
        entry_ready_only=spec["entry_ready_only"],
        require_pivot=spec.get("require_pivot", False),
        min_dist_pivot_pct=spec.get("min_dist_pivot_pct"),
        max_dist_pivot_pct=spec.get("max_dist_pivot_pct"),
    )

    zone_day_counts: dict[str, int] = {z: 0 for z in BREADTH_ZONES_ORDER}
    for d in trade_dates:
        z = zone_by_date.get(d)
        if z in zone_day_counts:
            zone_day_counts[z] += 1

    results: dict = {
        "date_start": date_start,
        "date_end": date_end,
        "variant": spec.get("variant"),
        "zone_day_counts": zone_day_counts,
        "pct_above_200_mean": round(float(panel["pct_above_200"].mean()), 2) if not panel.empty else None,
        "by_zone": {},
        "pooled_by_entry_zone": {},
    }

    for zone in BREADTH_ZONES_ORDER:
        periods, summary = _simulate_vcp_spec(
            conn,
            spec=spec,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            candidates_by_date=candidates,
            zone_by_date=zone_by_date,
            zone_filter=zone,
        )
        results["by_zone"][zone] = {
            "summary": summary,
            "periods": periods,
            "display": BREADTH_ZONE_DISPLAY[zone],
            "zh": BREADTH_ZONE_ZH[zone],
        }

    pooled_periods, pooled_summary = _simulate_vcp_spec(
        conn,
        spec=spec,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        candidates_by_date=candidates,
        zone_by_date=zone_by_date,
        zone_filter=None,
    )
    results["pooled_all"] = {"summary": pooled_summary, "periods": pooled_periods}

    buckets: dict[str, list[dict]] = {z: [] for z in BREADTH_ZONES_ORDER}
    for p in pooled_periods:
        z = p.get("breadth_zone_200")
        if z in buckets:
            buckets[z].append(p)
    for zone, sub in buckets.items():
        results["pooled_by_entry_zone"][zone] = _summarize(sub)

    if own:
        conn.close()
    return results


def _fmt_pct(val: object) -> str:
    if val is None:
        return "—"
    return f"{val}%"


def render_vcp_breadth_dual_markdown(
    *,
    pivot_results: dict,
    coil_results: dict,
    rrg_results: dict | None = None,
    year_label: str,
) -> str:
    """Side-by-side Pivot Gate vs Coil Close by 200MA breadth zone."""
    lines = [
        f"# VCP Pivot Gate vs Coil Close × 200MA Breadth · {year_label}",
        "",
        "**Breadth zone**（`market_breadth_ma.zone_200`）：universe 中股價 > MA200 的占比，",
        "依 TradingView / StockCharts 五區間分類：",
        "",
        "| 區間 | % Above 200MA |",
        "|------|---------------|",
        "| 超賣 Oversold | < 20% |",
        "| 偏弱 Weak | 20–40% |",
        "| 中性 Neutral | 40–60% |",
        "| 強勢 Strong | 60–80% |",
        "| 過熱 Overbought | > 80% |",
        "",
        f"### {year_label} 市場廣度分類（交易日分布）",
        "",
        "| 200MA 區間 | 交易日數 | Pivot Gate 進場日 | Coil Close 進場日 |",
        "|-----------|---------|------------------|------------------|",
    ]
    for zone in BREADTH_ZONES_ORDER:
        days = pivot_results["zone_day_counts"].get(zone, 0)
        pg_n = pivot_results["pooled_by_entry_zone"].get(zone, {}).get("n_periods") or 0
        cc_n = coil_results["pooled_by_entry_zone"].get(zone, {}).get("n_periods") or 0
        lines.append(
            f"| **{BREADTH_ZONE_ZH[zone]}** | {days} | {pg_n} | {cc_n} |"
        )
    pg_mean = pivot_results.get("pct_above_200_mean")
    dom_zone = max(pivot_results["zone_day_counts"], key=pivot_results["zone_day_counts"].get)
    lines.extend(
        [
            "",
            f"- 區間 `% above 200MA` 均值：**{pg_mean}%**",
            f"- 主導區間（交易日最多）：**{BREADTH_ZONE_ZH[dom_zone]}**（{pivot_results['zone_day_counts'][dom_zone]} 日）",
            "",
            "## 全樣本進場 · 依進場日 zone_200 分桶（並列對照）",
            "",
            "同一規格跑全程，再按**進場日**廣度分桶；指標可直接與 RRG mono hold7 對照。",
            "",
            "| 200MA 區間 | PG n | PG 均超額 | PG 勝率 | CC n | CC 均超額 | CC 勝率"
            + (" | RRG n | RRG 均超額 | RRG 勝率 |" if rrg_results else " |"),
            "|-----------|------|----------|--------|------|----------|--------"
            + ("|------|----------|--------|" if rrg_results else "|"),
        ]
    )
    for zone in BREADTH_ZONES_ORDER:
        pg = pivot_results["pooled_by_entry_zone"].get(zone) or {}
        cc = coil_results["pooled_by_entry_zone"].get(zone) or {}
        row = (
            f"| **{BREADTH_ZONE_ZH[zone]}** | "
            f"{pg.get('n_periods') or 0} | {_fmt_pct(pg.get('mean_excess_pct'))} | "
            f"{_fmt_pct(pg.get('win_rate_vs_bench_pct'))} | "
            f"{cc.get('n_periods') or 0} | {_fmt_pct(cc.get('mean_excess_pct'))} | "
            f"{_fmt_pct(cc.get('win_rate_vs_bench_pct'))}"
        )
        if rrg_results:
            rr = rrg_results["pooled_by_entry_zone"].get(zone) or {}
            row += (
                f" | {rr.get('n_periods') or 0} | {_fmt_pct(rr.get('mean_excess_pct'))} | "
                f"{_fmt_pct(rr.get('win_rate_vs_bench_pct'))} |"
            )
        lines.append(row)

    pg_all = pivot_results["pooled_all"]["summary"]
    cc_all = coil_results["pooled_all"]["summary"]
    lines.extend(
        [
            "",
            "## 全樣本合計",
            "",
            "| 策略 | 槽/持有 | n | 均超額 | 累計超額 | 勝率 vs IX0001 |",
            "|------|---------|---|--------|---------|----------------|",
            f"| **Pivot Gate** | 5槽 hold20 · breakout close | {pg_all.get('n_periods', 0)} | "
            f"{pg_all.get('mean_excess_pct', '—')}% | {pg_all.get('total_excess_pct', '—')}% | "
            f"{pg_all.get('win_rate_vs_bench_pct', '—')}% |",
            f"| **Coil Close** | 5槽 hold20 · 訊號日 close | {cc_all.get('n_periods', 0)} | "
            f"{cc_all.get('mean_excess_pct', '—')}% | {cc_all.get('total_excess_pct', '—')}% | "
            f"{cc_all.get('win_rate_vs_bench_pct', '—')}% |",
        ]
    )
    if rrg_results:
        rr_all = rrg_results["pooled_all"]["summary"]
        lines.append(
            f"| RRG mono hold7 | 3槽 hold7 · seg_last | {rr_all.get('n_periods', 0)} | "
            f"{rr_all.get('mean_excess_pct', '—')}% | {rr_all.get('total_excess_pct', '—')}% | "
            f"{rr_all.get('win_rate_vs_bench_pct', '—')}% |"
        )

    lines.extend(
        [
            "",
            "## 區間獨立回測（僅該 zone 日可開新倉）",
            "",
            "| 200MA 區間 | PG n | PG 均超額 | CC n | CC 均超額"
            + (" | RRG n | RRG 均超額 |" if rrg_results else " |"),
            "|-----------|------|----------|------|----------"
            + ("|------|----------|" if rrg_results else "|"),
        ]
    )
    for zone in BREADTH_ZONES_ORDER:
        pg = pivot_results["by_zone"][zone]["summary"]
        cc = coil_results["by_zone"][zone]["summary"]
        row = (
            f"| **{BREADTH_ZONE_ZH[zone]}** | {pg.get('n_periods') or 0} | "
            f"{_fmt_pct(pg.get('mean_excess_pct'))} | {cc.get('n_periods') or 0} | "
            f"{_fmt_pct(cc.get('mean_excess_pct'))}"
        )
        if rrg_results:
            rr = rrg_results["by_zone"][zone]["summary"]
            row += f" | {rr.get('n_periods') or 0} | {_fmt_pct(rr.get('mean_excess_pct'))} |"
        lines.append(row)

    lines.extend(
        [
            "",
            "---",
            "Pivot Gate：`breakout_close` · near pivot −8%～+5% · composite≥45 · wait≤10",
            "Coil Close：訊號日 `close` · 同漏斗 near pivot · hold20",
            "模組：`chunge_funnel_backtest.py` · 廣度：`market_breadth_ma.zone_200`",
        ]
    )
    return "\n".join(lines) + "\n"


def run_chunge_slot_backtest(
    conn: sqlite3.Connection | None = None,
    *,
    config: SlotBacktestConfig | None = None,
) -> dict:
    cfg = config or SlotBacktestConfig(
        date_start="2026-01-01",
        date_end="2026-12-31",
        model_id=MODEL_ID,
        min_composite=45.0,
        execution_states=DEFAULT_EXECUTION_STATES,
    )
    own = conn is None
    if own:
        conn = connect(DEFAULT_DB_PATH)

    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if cfg.date_start <= d <= cfg.date_end]
    states = cfg.execution_states or DEFAULT_EXECUTION_STATES
    model = cfg.model_id or MODEL_ID

    candidates = build_chunge_candidates_calendar(
        conn,
        trade_dates,
        model_id=model,
        min_composite=cfg.min_composite,
        execution_states=states,
        entry_ready_only=cfg.entry_ready_only,
        require_pivot=cfg.require_pivot,
        min_dist_pivot_pct=cfg.min_dist_pivot_pct,
        max_dist_pivot_pct=cfg.max_dist_pivot_pct,
    )
    mode = cfg.entry_price_mode
    if mode in ("pivot_stop", "breakout_close"):
        periods, summary = simulate_chunge_pivot_stop(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            candidates_by_date=candidates,
            n_slots=cfg.n_slots,
            hold_days=cfg.hold_days,
            top_n=cfg.top_n,
            max_entry_wait_days=cfg.max_entry_wait_days,
            stop_lookback_days=cfg.stop_lookback_days,
            entry_mode=mode,
        )
    else:
        periods, summary = simulate_chunge_slots(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            candidates_by_date=candidates,
            n_slots=cfg.n_slots,
            hold_days=cfg.hold_days,
            top_n=cfg.top_n,
        )
    summary["entry_ready_only"] = cfg.entry_ready_only
    summary["variant"] = cfg.variant

    result = {
        "date_start": cfg.date_start,
        "date_end": cfg.date_end,
        "model_id": model,
        "min_composite": cfg.min_composite,
        "execution_states": list(states),
        "entry_ready_only": cfg.entry_ready_only,
        "variant": cfg.variant,
        "periods": periods,
        "summary": summary,
    }
    if own:
        conn.close()
    return result


def render_chunge_backtest_markdown(result: dict) -> str:
    s = result["summary"]
    ds, de = result["date_start"], result["date_end"]
    variant = normalize_variant(str(result.get("variant") or s.get("variant") or "hold7"))
    entry_ready = bool(result.get("entry_ready_only") or s.get("entry_ready_only"))
    mode = str(s.get("entry_price_mode") or result.get("entry_price_mode") or "")
    is_pivot_gate = is_vcp_pivot_gate_variant(variant)
    is_coil_close = is_vcp_coil_close_variant(variant, entry_mode=mode, entry_ready=entry_ready)
    is_pivot_stop = variant == "entry_ready_pivot_stop" or mode == "pivot_stop"
    is_breakout_close = mode == "breakout_close" and not is_coil_close
    if is_coil_close:
        title = (
            f"VCP Coil Close · "
            f"{s.get('n_slots', 5)}-slot hold{s.get('hold_days', 20)}"
        )
        rule = "Near pivot −8%～+5% · 訊號日收盤進場（可低於 pivot）· hold 時間出"
    elif is_pivot_gate:
        title = (
            f"VCP Pivot Gate · "
            f"{s.get('n_slots', 5)}-slot hold{s.get('hold_days', 20)}"
        )
        rule = (
            "Near pivot −8%～+5% · Pre/Breakout/Early · "
            "close≥pivot 確認進場 · contraction low 停損 / hold 時間出"
        )
    elif is_pivot_stop:
        title = (
            f"Chunge funnel · entry_ready · pivot/stop · "
            f"{s.get('n_slots', 5)}-slot hold{s.get('hold_days', 20)}"
        )
        rule = (
            "vcp-tm Section A · entry_ready=1 · pivot 突破進場 · "
            f"停損 / hold{s.get('hold_days', 20)} 時間出場"
        )
    elif is_breakout_close:
        title = (
            f"Chunge funnel · breakout close · "
            f"{s.get('n_slots', 5)}-slot hold{s.get('hold_days', 20)}"
        )
        rule = "Near pivot · close≥pivot 確認 · 停損 / hold 時間出"
    elif entry_ready:
        title = f"Chunge funnel · entry_ready · {s.get('n_slots', 5)}-slot hold{s.get('hold_days', 20)}"
        rule = "vcp-tm Section A · entry_ready=1 · composite 排序 · 收盤進 / 收盤出"
    else:
        title = f"Chunge funnel × {s.get('n_slots', 3)}-slot hold{s.get('hold_days', 7)}"
        rule = "composite_score 排序 · 收盤進 / 收盤出（RRG hold7 模板）"
    lines = [
        f"# {title} 回測 · {ds}～{de}",
        "",
        f"策略：**{rule}**",
        "",
        f"- model_id：`{result.get('model_id')}` · min_composite ≥ {result.get('min_composite')}",
        f"- variant：`{variant}` · entry_ready_only={entry_ready}",
        f"- execution_states：{', '.join(result.get('execution_states') or [])}",
        f"- 訊號日覆蓋：{s.get('screen_coverage_pct', '—')}%"
        f"（{s.get('signal_days_with_candidates', 0)} 日有可進場候選）",
    ]
    if is_pivot_stop or is_breakout_close or is_calibrated:
        lines.extend(
            [
                f"- max_entry_wait：{s.get('max_entry_wait_days', '—')} 交易日",
                f"- 停損出場：{s.get('n_stopped', 0)} 筆 · "
                f"時間出場：{s.get('n_time_exit', 0)} 筆 · "
                f"pending 過期：{s.get('n_pending_expired', 0)}",
            ]
        )
    lines.extend(
        [
        "",
        "## 全樣本",
        "",
        "| 成交筆數 | 勝率 vs 基準 | 均報酬 | 均超額 | 累計超額 |",
        "|---------|-------------|--------|--------|---------|",
        f"| {s.get('n_periods', 0)} | {s.get('win_rate_vs_bench_pct', '—')}% | "
        f"{s.get('mean_return_pct', '—')}% | {s.get('mean_excess_pct', '—')}% | "
        f"{s.get('total_excess_pct', '—')}% |",
        "",
        "---",
        "模組：`chunge_funnel_backtest.py` · 訊號：`vcp_screen_scores_v2`（vcp-funnel）",
        ]
    )
    return "\n".join(lines) + "\n"


def build_executed_legs_for_timeline(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    n_slots: int = 3,
    hold_days: int = 7,
    capital_ntd: float = 10_000.0,
    min_composite: float = 45.0,
    execution_states: tuple[str, ...] = DEFAULT_EXECUTION_STATES,
    entry_ready_only: bool = False,
    top_n: int = 15,
) -> tuple[list[dict], list[dict], list[dict], dict]:
    """Build leg rows compatible with render_l1h9_slots_timeline_html."""
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    candidates = build_chunge_candidates_calendar(
        conn,
        dates,
        min_composite=min_composite,
        execution_states=execution_states,
        entry_ready_only=entry_ready_only,
    )

    state: dict = {"slots": [], "history": []}
    executed_signals: list[dict] = []
    skipped_signals: list[dict] = []
    legs_out: list[dict] = []
    peak = 0
    n_skip = 0

    for as_of in dates:
        _expire_slots(state, as_of)
        _backfill_exit_dates(conn, state, full_dates)

        held = {p["stock_id"] for p in state.get("slots", [])}
        used = {int(p["slot"]) for p in state.get("slots", [])}
        free = [i for i in range(n_slots) if i not in used]
        cands = candidates.get(as_of, [])

        for cand in cands[:top_n]:
            if cand.stock_id in held:
                continue
            if not free:
                exit_guess = _exit_date_from_entry(conn, full_dates, as_of, hold_days) or ""
                skipped_signals.append(
                    {
                        "signal_date": as_of,
                        "entry_date": as_of,
                        "exit_date": exit_guess,
                        "stock_id": cand.stock_id,
                        "stock_name": cand.stock_name,
                        "seg_last": cand.composite_score,
                        "n_legs": 1,
                        "return_pct": None,
                        "reason": "slots_full",
                    }
                )
                n_skip += 1
                continue

            exit_d = _exit_date_from_entry(conn, full_dates, as_of, hold_days) or ""
            slot = free.pop(0)
            pos = {
                "slot": slot,
                "stock_id": cand.stock_id,
                "stock_name": cand.stock_name,
                "entry_date": as_of,
                "exit_date": exit_d,
                "composite_score": round(cand.composite_score, 2),
            }
            if not exit_d:
                pos["exit_pending"] = True
            state.setdefault("slots", []).append(pos)
            held.add(cand.stock_id)

            entry_px = (
                float(close.at[as_of, cand.stock_id])
                if cand.stock_id in close.columns
                else None
            )
            trade = _close_trade(conn, close, pos) if exit_d and exit_d <= dates[-1] else None
            if trade:
                ret_pct = trade["return_pct"]
                bench_ret = trade["bench_return_pct"]
                excess = trade["excess_pct"]
            elif exit_d and entry_px:
                ret_pct = 0.0
                bench_ret = None
                excess = None
            else:
                ret_pct = 0.0
                bench_ret = None
                excess = None

            pnl = capital_ntd * ret_pct / 100.0
            executed_signals.append(
                {
                    "signal_date": as_of,
                    "entry_date": as_of,
                    "exit_date": exit_d,
                    "stock_id": cand.stock_id,
                    "stock_name": cand.stock_name,
                    "seg_last": cand.composite_score,
                    "n_legs": 1,
                    "return_pct": ret_pct,
                    "bench_return_pct": bench_ret,
                    "excess_pct": excess,
                    "pnl_ntd": pnl,
                    "slot_id": slot,
                }
            )
            legs_out.append(
                {
                    "stock_id": cand.stock_id,
                    "stock_name": cand.stock_name,
                    "action": "chunge",
                    "entry_date": as_of,
                    "exit_date": exit_d,
                    "entry_px": entry_px,
                    "exit_px": None,
                    "allocated_ntd": capital_ntd,
                    "return_pct": ret_pct,
                    "pnl_ntd": pnl,
                    "slot_id": slot,
                    "seg_last": cand.composite_score,
                }
            )
            peak = max(peak, len(state["slots"]))

    from .copytrade_backtest import _bench_close, bench_return_entry_to_exit

    for leg in legs_out:
        entry = str(leg["entry_date"])
        exit_d = str(leg["exit_date"])
        b0 = _bench_close(conn, entry)
        bench_ret = bench_return_entry_to_exit(conn, entry, exit_d, entry_price_mode="close")
        leg["bench_entry_px"] = round(b0, 4) if b0 is not None else None
        leg["bench_return_pct"] = round(bench_ret, 4) if bench_ret is not None else None

    n_executed = len(executed_signals)
    n_signals = n_executed + n_skip
    capture = round(100.0 * n_executed / n_signals, 2) if n_signals else None
    meta = {
        "n_slots": n_slots,
        "capital_ntd": capital_ntd,
        "total_capital_ntd": n_slots * capital_ntd,
        "cost_bps": 0.0,
        "hold_trading_days": hold_days,
        "n_signals": n_signals,
        "n_executed": n_executed,
        "n_skipped": n_skip,
        "signal_capture_pct": capture,
        "peak_concurrent_slots": peak,
        "strategy_id": "vcp-pivot-gate",
        "strategy_title": f"Chunge funnel · {n_slots}槽 hold{hold_days}",
        "strategy_rule": f"D 收盤進場 / D+{hold_days} 收盤出場（hold{hold_days}）",
        "strategy_filter": (
            "entry_ready=1 · chunge-funnel"
            if entry_ready_only
            else "composite_score 排序 · chunge-funnel 漏斗"
        ),
        "display_code": "Chunge",
        "table_mode": "mono",
        "entry_price_mode": "close",
    }
    return legs_out, executed_signals, skipped_signals, meta


def build_executed_legs_for_timeline_pivot(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    n_slots: int = 5,
    hold_days: int = 20,
    capital_ntd: float = 10_000.0,
    min_composite: float = 45.0,
    execution_states: tuple[str, ...] = MINERVINI_NEAR_PIVOT_STATES,
    entry_ready_only: bool = False,
    require_pivot: bool = True,
    min_dist_pivot_pct: float | None = MINERVINI_MAX_BELOW_PIVOT,
    max_dist_pivot_pct: float | None = 5.0,
    entry_mode: str = "breakout_close",
    max_entry_wait_days: int = 10,
    stop_lookback_days: int = 20,
    top_n: int = 15,
    variant: str = "chunge_near_pivot_breakout_close",
) -> tuple[list[dict], list[dict], list[dict], dict]:
    """Near-pivot pivot/breakout entry · legs for render_l1h9_slots_timeline_html."""
    if entry_mode == "breakout_close":
        fill_fn = _breakout_close_entry_px
        mode_label = "breakout_close"
        entry_rule = f"收盤≥pivot 進場（最多等 {max_entry_wait_days} 日）"
    else:
        fill_fn = _pivot_breakout_entry_px
        mode_label = "pivot_stop"
        entry_rule = f"pivot 突破進場（最多等 {max_entry_wait_days} 日）"

    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    date_idx = {d: i for i, d in enumerate(full_dates)}
    candidates = build_chunge_candidates_calendar(
        conn,
        dates,
        min_composite=min_composite,
        execution_states=execution_states,
        entry_ready_only=entry_ready_only,
        require_pivot=require_pivot,
        min_dist_pivot_pct=min_dist_pivot_pct,
        max_dist_pivot_pct=max_dist_pivot_pct,
    )

    pending: list[dict] = []
    open_positions: list[dict] = []
    executed_signals: list[dict] = []
    skipped_signals: list[dict] = []
    legs_out: list[dict] = []
    peak = 0
    n_skip = 0

    def _occupied_stock_ids() -> set[str]:
        ids = {p["stock_id"] for p in open_positions}
        ids.update(p["stock_id"] for p in pending)
        return ids

    def _used_slots() -> set[int]:
        slots = {int(p["slot"]) for p in open_positions}
        slots.update(int(p["slot"]) for p in pending)
        return slots

    def _record_closed(
        pos: dict,
        exit_date: str,
        exit_px: float,
        exit_reason: str,
    ) -> None:
        entry_date = str(pos["entry_date"])
        entry_px = float(pos["entry_px"])
        ret_pct = return_pct(entry_px, exit_px)
        bench = _bench_return_close_to_close(conn, entry_date, exit_date)
        excess = (ret_pct - bench) if bench is not None else None
        pnl = capital_ntd * ret_pct / 100.0
        alpha_ntd = capital_ntd * excess / 100.0 if excess is not None else None
        slot = int(pos["slot"])
        executed_signals.append(
            {
                "signal_date": str(pos["signal_date"]),
                "entry_date": entry_date,
                "exit_date": exit_date,
                "slot_id": slot,
                "n_legs": 1,
                "stock_id": pos["stock_id"],
                "stock_name": pos.get("stock_name", ""),
                "seg_last": pos.get("composite_score"),
                "deployed_ntd": capital_ntd,
                "pnl_ntd": pnl,
                "return_pct": round(ret_pct, 4),
                "bench_return_pct": round(bench, 4) if bench is not None else None,
                "alpha_ntd": round(alpha_ntd, 2) if alpha_ntd is not None else None,
                "exit_reason": exit_reason,
            }
        )
        legs_out.append(
            {
                "leg_id": f"{pos['signal_date']}|{pos['stock_id']}",
                "signal_date": str(pos["signal_date"]),
                "stock_id": pos["stock_id"],
                "stock_name": pos.get("stock_name", ""),
                "action": "chunge",
                "entry_date": entry_date,
                "exit_date": exit_date,
                "entry_px": round(entry_px, 4),
                "exit_px": round(exit_px, 4),
                "allocated_ntd": capital_ntd,
                "return_pct": round(ret_pct, 4),
                "pnl_ntd": pnl,
                "slot_id": slot,
                "seg_last": pos.get("composite_score"),
                "exit_reason": exit_reason,
            }
        )

    for as_of in dates:
        for pos in list(open_positions):
            entry = str(pos["entry_date"])
            if as_of < entry:
                continue
            stop_px = _stop_hit_exit_px(conn, pos["stock_id"], as_of, float(pos["stop_loss"]))
            if stop_px is not None:
                _record_closed(pos, as_of, stop_px, "stop")
                open_positions.remove(pos)
                continue
            ei = date_idx.get(entry)
            ai = date_idx.get(as_of)
            if ei is not None and ai is not None and ai >= ei + hold_days:
                exit_px = stock_close(conn, pos["stock_id"], as_of)
                if exit_px is not None and exit_px > 0:
                    _record_closed(pos, as_of, exit_px, "time")
                    open_positions.remove(pos)

        for pend in list(pending):
            if as_of < pend["signal_date"]:
                continue
            if as_of > pend["expire_date"]:
                pending.remove(pend)
                skipped_signals.append(
                    {
                        "signal_date": pend["signal_date"],
                        "entry_date": pend["signal_date"],
                        "exit_date": "",
                        "stock_id": pend["stock_id"],
                        "stock_name": pend["stock_name"],
                        "seg_last": pend.get("composite_score"),
                        "n_legs": 1,
                        "return_pct": None,
                        "reason": "pending_expired",
                    }
                )
                n_skip += 1
                continue
            entry_px = fill_fn(conn, pend["stock_id"], as_of, float(pend["pivot_price"]))
            if entry_px is None:
                continue
            pending.remove(pend)
            open_positions.append(
                {
                    "slot": pend["slot"],
                    "stock_id": pend["stock_id"],
                    "stock_name": pend["stock_name"],
                    "signal_date": pend["signal_date"],
                    "entry_date": as_of,
                    "entry_px": entry_px,
                    "stop_loss": pend["stop_loss"],
                    "composite_score": pend.get("composite_score"),
                }
            )
            peak = max(peak, len(open_positions) + len(pending))

        cands = candidates.get(as_of, [])
        held_ids = _occupied_stock_ids()
        used = _used_slots()
        free = [i for i in range(n_slots) if i not in used]

        for cand in cands[:top_n]:
            if cand.stock_id in held_ids:
                continue
            if not free:
                skipped_signals.append(
                    {
                        "signal_date": as_of,
                        "entry_date": as_of,
                        "exit_date": "",
                        "stock_id": cand.stock_id,
                        "stock_name": cand.stock_name,
                        "seg_last": cand.composite_score,
                        "n_legs": 1,
                        "return_pct": None,
                        "reason": "slots_full",
                    }
                )
                n_skip += 1
                break
            if cand.pivot_price is None or cand.pivot_price <= 0:
                continue
            stop = _resolve_stop_loss(
                conn,
                cand.stock_id,
                as_of,
                float(cand.pivot_price),
                db_stop=cand.stop_loss,
                lookback_days=stop_lookback_days,
                full_dates=full_dates,
            )
            if stop is None or stop <= 0:
                continue
            expire_dates = trading_dates_after(
                conn, as_of, count=max_entry_wait_days, inclusive_anchor=True
            )
            expire_date = expire_dates[-1] if expire_dates else as_of
            slot = free.pop(0)
            pending.append(
                {
                    "slot": slot,
                    "signal_date": as_of,
                    "expire_date": expire_date,
                    "stock_id": cand.stock_id,
                    "stock_name": cand.stock_name,
                    "pivot_price": float(cand.pivot_price),
                    "stop_loss": stop,
                    "composite_score": round(cand.composite_score, 2),
                }
            )
            held_ids.add(cand.stock_id)
            peak = max(peak, len(open_positions) + len(pending))

    last = dates[-1] if dates else ""
    for pos in list(open_positions):
        exit_px = stock_close(conn, pos["stock_id"], last)
        if exit_px is not None and exit_px > 0 and str(pos["entry_date"]) <= last:
            _record_closed(pos, last, exit_px, "window_end")
            open_positions.remove(pos)

    for pend in list(pending):
        pending.remove(pend)
        skipped_signals.append(
            {
                "signal_date": pend["signal_date"],
                "entry_date": pend["signal_date"],
                "exit_date": "",
                "stock_id": pend["stock_id"],
                "stock_name": pend["stock_name"],
                "seg_last": pend.get("composite_score"),
                "n_legs": 1,
                "return_pct": None,
                "reason": "pending_expired",
            }
        )
        n_skip += 1

    from .copytrade_backtest import _bench_close, bench_return_entry_to_exit

    for leg in legs_out:
        entry = str(leg["entry_date"])
        exit_d = str(leg["exit_date"])
        b0 = _bench_close(conn, entry)
        bench_ret = bench_return_entry_to_exit(conn, entry, exit_d, entry_price_mode="close")
        leg["bench_entry_px"] = round(b0, 4) if b0 is not None else None
        leg["bench_return_pct"] = round(bench_ret, 4) if bench_ret is not None else None

    n_executed = len(executed_signals)
    n_signals = n_executed + n_skip
    capture = round(100.0 * n_executed / n_signals, 2) if n_signals else None
    dist_lo = min_dist_pivot_pct if min_dist_pivot_pct is not None else MINERVINI_MAX_BELOW_PIVOT
    dist_hi = max_dist_pivot_pct if max_dist_pivot_pct is not None else 5.0
    meta = {
        "n_slots": n_slots,
        "capital_ntd": capital_ntd,
        "total_capital_ntd": n_slots * capital_ntd,
        "cost_bps": 0.0,
        "hold_trading_days": hold_days,
        "n_signals": n_signals,
        "n_executed": n_executed,
        "n_skipped": n_skip,
        "signal_capture_pct": capture,
        "peak_concurrent_slots": peak,
        "strategy_id": "vcp-pivot-gate",
        "strategy_title": f"Chunge · near pivot · {n_slots}槽 hold{hold_days}",
        "strategy_rule": f"{entry_rule} · hold{hold_days} 或停損出場",
        "strategy_filter": (
            f"near pivot {dist_lo:.0f}%～{dist_hi:.0f}% · "
            f"{', '.join(execution_states)} · composite≥{min_composite:.0f}"
        ),
        "display_code": "Chunge VCP",
        "table_mode": "mono",
        "entry_price_mode": mode_label,
        "variant": variant,
        "max_entry_wait_days": max_entry_wait_days,
    }
    return legs_out, executed_signals, skipped_signals, meta
