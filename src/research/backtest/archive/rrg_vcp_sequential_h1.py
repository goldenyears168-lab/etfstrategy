"""H1 · RRG 觸發 → N 日內首個 VCP pivot 進場（序列 · hold7）。

對照：
  A  · mono fresh · 觸發日收盤進場（RRG mono hold7 基線）
  D0 · 僅 VCP pivot · 當日收盤進場（無 RRG 觸發）
  H1 · RRG 觸發 → 0..max_lag 日內首個 VCP pivot 日收盤進場
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd

from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
from research.backtest.chunge_funnel_backtest import VCP_PIVOT_GATE
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import (
    _settle_trade,
    _summarize,
    build_fresh_mono_calendar,
    simulate_mono_hold7,
)
from research.backtest.rrg_mono_intraday_ab import build_vcp_pivot_calendar, vcp_close_shortlist
from rrg_mono_daily_brief import (
    HOLD_DAYS,
    MAX_SLOTS,
    TOP_N,
    ScanRow,
    _backfill_exit_dates,
    _exit_date_from_entry,
    _expire_slots,
    _feat,
    _fresh_mono,
    _mono_tier2,
)
from project_config import DEFAULT_ETF_CODES
from rrg_rotation import compute_rrg_panel
from stock_db import load_etf_constituent_watchlist, load_vcp_screen_v2_for_date
from vcp_funnel_screen import MODEL_ID as VCP_FUNNEL_MODEL_ID

RrgTriggerGate = Literal["mono_fresh", "mono_tier2_new", "mono_tier2"]


@dataclass(frozen=True)
class _PendingWatch:
    stock_id: str
    stock_name: str
    trigger_date: str
    seg_last: float
    disp: float
    deadline: str


def _vcp_pass_ids(conn: sqlite3.Connection, as_of: str) -> dict[str, float]:
    """當日 VCP pivot gate 通過標的 → composite。"""
    min_composite = float(VCP_PIVOT_GATE["min_composite"])
    states = tuple(VCP_PIVOT_GATE["execution_states"])
    min_dist = float(VCP_PIVOT_GATE["min_dist_pivot_pct"])
    max_dist = float(VCP_PIVOT_GATE["max_dist_pivot_pct"])
    require_pivot = bool(VCP_PIVOT_GATE.get("require_pivot"))

    rows = load_vcp_screen_v2_for_date(
        conn,
        as_of,
        model_id=VCP_FUNNEL_MODEL_ID,
        min_score=min_composite,
        execution_states=states,
    )
    out: dict[str, float] = {}
    for r in rows:
        sid = str(r["stock_id"])
        dist = (
            float(r["distance_from_pivot_pct"])
            if r["distance_from_pivot_pct"] is not None
            else None
        )
        pivot = float(r["pivot_price"]) if r["pivot_price"] else None
        if require_pivot and (pivot is None or pivot <= 0):
            continue
        if dist is not None and dist < min_dist:
            continue
        if dist is not None and dist > max_dist:
            continue
        out[sid] = float(r["composite_score"] or 0.0)
    return out


def build_rrg_trigger_calendar(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    gate: RrgTriggerGate = "mono_tier2_new",
) -> dict[str, list[ScanRow]]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=20)
    daily_pct = close.pct_change(fill_method=None) * 100.0
    full_dates = close.index.astype(str).tolist()
    date_set = set(trade_dates)
    watch = load_etf_constituent_watchlist(conn, DEFAULT_ETF_CODES)
    name_map = {w["stock_id"]: w.get("stock_name", "") for w in watch}

    out: dict[str, list[ScanRow]] = {d: [] for d in trade_dates}
    prev_tier2: dict[str, bool] = {}

    for si, as_of in enumerate(full_dates):
        if as_of not in date_set or si < 4:
            continue
        triggered: list[ScanRow] = []
        for sid in close.columns:
            f = _feat(rs_ratio, rs_mom, full_dates, si, str(sid))
            if f is None:
                continue
            is_tier2 = _mono_tier2(f)
            is_fresh = _fresh_mono(rs_ratio, rs_mom, full_dates, si, str(sid))
            fire = False
            if gate == "mono_fresh":
                fire = is_fresh
            elif gate == "mono_tier2":
                fire = is_tier2
            elif gate == "mono_tier2_new":
                fire = is_tier2 and not prev_tier2.get(str(sid), False)
            if not fire:
                prev_tier2[str(sid)] = is_tier2
                continue
            prev_tier2[str(sid)] = is_tier2
            pct = float(daily_pct.at[as_of, sid]) if sid in daily_pct.columns else None
            if pct != pct:
                pct = None
            triggered.append(
                ScanRow(
                    stock_id=str(sid),
                    stock_name=str(name_map.get(str(sid), sid)),
                    fresh=is_fresh,
                    mono=True,
                    seg_last=float(f["seg_last"]),
                    disp=float(f["disp"]),
                    segs=[float(x) for x in f["segs"]],
                    quadrants=[q or "?" for q in f["quadrants"]],
                    rs_ratio=float(f["rs_ratio"]),
                    rs_momentum=float(f["rs_momentum"]),
                    daily_pct=pct,
                )
            )
        triggered.sort(key=lambda r: (-r.seg_last, r.stock_id))
        out[as_of] = triggered
    return out


def _trading_deadline(full_dates: list[str], trigger_date: str, max_lag: int) -> str | None:
    if trigger_date not in full_dates:
        return None
    idx = full_dates.index(trigger_date) + max_lag
    if idx >= len(full_dates):
        return full_dates[-1]
    return full_dates[idx]


def simulate_rrg_then_vcp(
    conn: sqlite3.Connection,
    *,
    trade_dates: list[str],
    full_dates: list[str],
    close: pd.DataFrame,
    zone_by_date: dict[str, str],
    trigger_by_date: dict[str, list[ScanRow]],
    max_lag: int = 10,
    leg_id: str = "H1",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    state: dict[str, Any] = {"slots": [], "history": []}
    periods: list[dict[str, Any]] = []
    pending: list[_PendingWatch] = []

    def _held_ids() -> set[str]:
        return {p["stock_id"] for p in state.get("slots", [])}

    def _try_enter(w: _PendingWatch, entry_date: str, vcp_comp: float) -> bool:
        held = _held_ids()
        used_slots = {int(p["slot"]) for p in state.get("slots", [])}
        free_slots = [i for i in range(MAX_SLOTS) if i not in used_slots]
        if not free_slots or w.stock_id in held:
            return False
        if entry_date not in full_dates or w.trigger_date not in full_dates:
            return False
        exit_d = _exit_date_from_entry(conn, full_dates, entry_date, HOLD_DAYS)
        slot = free_slots[0]
        pos = {
            "slot": slot,
            "stock_id": w.stock_id,
            "stock_name": w.stock_name,
            "signal_date": w.trigger_date,
            "entry_date": entry_date,
            "exit_date": exit_d or "",
            "seg_last": round(w.seg_last, 4),
            "disp": round(w.disp, 4),
            "lag_days": full_dates.index(entry_date) - full_dates.index(w.trigger_date),
            "vcp_composite": round(vcp_comp, 2),
        }
        if exit_d is None:
            pos["exit_pending"] = True
        state.setdefault("slots", []).append(pos)
        return True

    for as_of in trade_dates:
        expired = _expire_slots(state, as_of)
        _backfill_exit_dates(conn, state, full_dates)
        for pos in expired:
            row = _settle_trade(conn, close, pos)
            if row is None:
                continue
            row["entry_leg"] = leg_id
            row["breadth_zone_200"] = zone_by_date.get(str(row["signal_date"]), "unknown")
            row["lag_days"] = pos.get("lag_days")
            row["vcp_composite"] = pos.get("vcp_composite")
            periods.append(row)

        pending = [w for w in pending if w.deadline >= as_of and w.stock_id not in _held_ids()]
        vcp_today = _vcp_pass_ids(conn, as_of)

        if vcp_today and pending and len(state.get("slots", [])) < MAX_SLOTS:
            ready = [w for w in pending if w.stock_id in vcp_today]
            ready.sort(
                key=lambda w: (
                    -vcp_today.get(w.stock_id, 0.0),
                    -w.seg_last,
                    w.stock_id,
                )
            )
            entered: set[str] = set()
            for w in ready:
                if len(state.get("slots", [])) >= MAX_SLOTS:
                    break
                if _try_enter(w, as_of, vcp_today[w.stock_id]):
                    entered.add(w.stock_id)
            pending = [w for w in pending if w.stock_id not in entered]

        pending_ids = {w.stock_id for w in pending}
        for row in trigger_by_date.get(as_of, [])[:TOP_N]:
            if row.stock_id in _held_ids() or row.stock_id in pending_ids:
                continue
            deadline = _trading_deadline(full_dates, as_of, max_lag)
            if deadline is None:
                continue
            watch = _PendingWatch(
                stock_id=row.stock_id,
                stock_name=row.stock_name,
                trigger_date=as_of,
                seg_last=row.seg_last,
                disp=row.disp,
                deadline=deadline,
            )
            if row.stock_id in vcp_today and len(state.get("slots", [])) < MAX_SLOTS:
                if _try_enter(watch, as_of, vcp_today[row.stock_id]):
                    continue
            pending.append(watch)
            pending_ids.add(row.stock_id)

    for pos in list(state.get("slots", [])):
        exit_d = str(pos.get("exit_date") or "")
        if exit_d and exit_d <= trade_dates[-1]:
            row = _settle_trade(conn, close, pos)
            if row:
                row["entry_leg"] = leg_id
                row["breadth_zone_200"] = zone_by_date.get(str(row["signal_date"]), "unknown")
                row["lag_days"] = pos.get("lag_days")
                row["vcp_composite"] = pos.get("vcp_composite")
                periods.append(row)

    summary = _summarize(periods)
    summary["entry_leg"] = leg_id
    summary["max_lag"] = max_lag
    lags = [p.get("lag_days") for p in periods if p.get("lag_days") is not None]
    summary["mean_lag_days"] = round(sum(lags) / len(lags), 2) if lags else None
    n_triggers = sum(len(v) for v in trigger_by_date.values())
    summary["fill_rate_pct"] = round(len(periods) / max(1, n_triggers) * 100, 2)
    return periods, summary


def simulate_vcp_only_d0(
    conn: sqlite3.Connection,
    *,
    trade_dates: list[str],
    full_dates: list[str],
    close: pd.DataFrame,
    zone_by_date: dict[str, str],
    pool_by_date: dict[str, list[ScanRow]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    state: dict[str, Any] = {"slots": [], "history": []}
    periods: list[dict[str, Any]] = []

    for as_of in trade_dates:
        expired = _expire_slots(state, as_of)
        _backfill_exit_dates(conn, state, full_dates)
        for pos in expired:
            row = _settle_trade(conn, close, pos)
            if row is None:
                continue
            row["entry_leg"] = "D0"
            row["breadth_zone_200"] = zone_by_date.get(str(row["signal_date"]), "unknown")
            periods.append(row)

        held = {p["stock_id"] for p in state.get("slots", [])}
        used_slots = {int(p["slot"]) for p in state.get("slots", [])}
        free_slots = [i for i in range(MAX_SLOTS) if i not in used_slots]
        for row in vcp_close_shortlist(pool_by_date.get(as_of, [])):
            if not free_slots:
                break
            if row.stock_id in held:
                continue
            if as_of not in close.index or row.stock_id not in close.columns:
                continue
            exit_d = _exit_date_from_entry(conn, full_dates, as_of, HOLD_DAYS)
            slot = free_slots.pop(0)
            pos = {
                "slot": slot,
                "stock_id": row.stock_id,
                "stock_name": row.stock_name,
                "signal_date": as_of,
                "entry_date": as_of,
                "exit_date": exit_d or "",
                "seg_last": round(row.seg_last, 4),
                "disp": round(row.disp, 4),
            }
            if exit_d is None:
                pos["exit_pending"] = True
            state.setdefault("slots", []).append(pos)
            held.add(row.stock_id)

    for pos in list(state.get("slots", [])):
        exit_d = str(pos.get("exit_date") or "")
        if exit_d and exit_d <= trade_dates[-1]:
            row = _settle_trade(conn, close, pos)
            if row:
                row["entry_leg"] = "D0"
                row["breadth_zone_200"] = zone_by_date.get(str(row["signal_date"]), "unknown")
                periods.append(row)

    summary = _summarize(periods)
    summary["entry_leg"] = "D0"
    return periods, summary


def run_h1_comparison(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    max_lag: int = 10,
    trigger_gate: RrgTriggerGate = "mono_tier2_new",
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]

    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    trigger_by_date = build_rrg_trigger_calendar(conn, trade_dates, gate=trigger_gate)
    pool_by_date = build_vcp_pivot_calendar(conn, trade_dates)

    legs: dict[str, Any] = {}

    periods_a, summary_a = simulate_mono_hold7(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        zone_by_date=zone_by_date,
        fresh_by_date=fresh_by_date,
        zone_filter=None,
        entry_price_mode="close",
    )
    for p in periods_a:
        p["entry_leg"] = "A"
    legs["A"] = {
        "label": "RRG mono fresh · 觸發日收盤（hold7 基線）",
        "summary": summary_a,
        "n_periods": summary_a.get("n_periods") or len(periods_a),
    }

    periods_d0, summary_d0 = simulate_vcp_only_d0(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        zone_by_date=zone_by_date,
        pool_by_date=pool_by_date,
    )
    legs["D0"] = {
        "label": "僅 VCP pivot · 當日收盤（無 RRG 觸發）",
        "summary": summary_d0,
        "n_periods": summary_d0.get("n_periods") or len(periods_d0),
    }

    leg_h1 = f"H1_lag{max_lag}"
    periods_h1, summary_h1 = simulate_rrg_then_vcp(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        zone_by_date=zone_by_date,
        trigger_by_date=trigger_by_date,
        max_lag=max_lag,
        leg_id=leg_h1,
    )
    legs[leg_h1] = {
        "label": f"RRG {trigger_gate} → {max_lag} 日內首個 VCP pivot 收盤",
        "summary": summary_h1,
        "n_periods": summary_h1.get("n_periods") or len(periods_h1),
    }

    base = legs["A"]["summary"].get("mean_excess_pct")
    for lid, payload in legs.items():
        excess = payload["summary"].get("mean_excess_pct")
        payload["delta_vs_a_pp"] = (
            round(float(excess) - float(base), 4)
            if excess is not None and base is not None and lid != "A"
            else (0.0 if lid == "A" else None)
        )

    trigger_events = sum(len(v) for v in trigger_by_date.values())
    return {
        "date_start": date_start,
        "date_end": date_end,
        "hypothesis": "H1 · RRG trigger then first VCP pivot within max_lag · hold7",
        "trigger_gate": trigger_gate,
        "max_lag": max_lag,
        "vcp_gate": {
            "min_composite": VCP_PIVOT_GATE["min_composite"],
            "execution_states": list(VCP_PIVOT_GATE["execution_states"]),
        },
        "trigger_stats": {
            "trigger_events": trigger_events,
            "mean_triggers_per_day": round(trigger_events / max(1, len(trade_dates)), 2),
        },
        "legs": legs,
    }


def run_h1_lag_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    lags: tuple[int, ...] = (5, 10, 15, 20),
    trigger_gate: RrgTriggerGate = "mono_tier2_new",
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for lag in lags:
        payload = run_h1_comparison(
            conn,
            date_start=date_start,
            date_end=date_end,
            max_lag=lag,
            trigger_gate=trigger_gate,
        )
        leg_key = f"H1_lag{lag}"
        item = payload["legs"][leg_key]
        rows.append(
            {
                "max_lag": lag,
                "n_periods": item["n_periods"],
                "mean_excess_pct": item["summary"].get("mean_excess_pct"),
                "win_rate_vs_bench_pct": item["summary"].get("win_rate_vs_bench_pct"),
                "mean_lag_days": item["summary"].get("mean_lag_days"),
                "filled_from_trigger_pct": item["summary"].get("fill_rate_pct"),
                "delta_vs_a_pp": item.get("delta_vs_a_pp"),
            }
        )
    base = run_h1_comparison(
        conn, date_start=date_start, date_end=date_end, max_lag=lags[0], trigger_gate=trigger_gate
    )
    return {
        "date_start": date_start,
        "date_end": date_end,
        "trigger_gate": trigger_gate,
        "lag_sweep": rows,
        "reference": {
            "A_mean_excess_pct": base["legs"]["A"]["summary"].get("mean_excess_pct"),
            "D0_mean_excess_pct": base["legs"]["D0"]["summary"].get("mean_excess_pct"),
        },
    }
