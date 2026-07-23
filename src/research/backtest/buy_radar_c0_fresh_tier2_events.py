"""Buy-signal-radar C0 · fresh / tier2 event study (Part B).

Pool = prior-session PIT (matches morning radar lock via _signal_date_for_session).
Trigger = first poll ≥09:05 where C0 scale confirm≥1 fires as pool entry
(same evaluate_pool_observation contract). One event per (stock, session).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from market_benchmark import load_benchmark_close
from research.backtest.c18acc_old_vs_live_gap_attribution import _is_cut_date
from research.backtest.c18acc_open_timing_study import _delta_pp
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import rank_shortlist_scale
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from rrg_mono_swap_accel_screen import _signal_date_for_session
from stock_db.kbar import load_kbar_day_closes, price_at_or_before_minute

TOPIC_ID = "buy-radar-c0-fresh-tier2-events"
SCHEMA = "buy_radar_c0_fresh_tier2_events-v1"
HOLD_DAYS = (1, 3, 5, 10)
POLL_START = "09:05"
POLL_END = "13:20"
CONFIRM_BARS = 1
CASE_STOCK = "6505"
CASE_DATE = "2026-07-07"
SIGNAL_EDGE_OOS_N = 20
SIGNAL_EDGE_OOS_5D_MIN = 0.3


def _poll_minutes(*, start: str = POLL_START, end: str = POLL_END, step: int = 5) -> list[str]:
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    t, end_t = sh * 60 + sm, eh * 60 + em
    out: list[str] = []
    while t <= end_t:
        out.append(f"{t // 60:02d}:{t % 60:02d}")
        t += step
    return out


def _pool_ids(rows: list[Any]) -> set[str]:
    return {str(r.stock_id) for r in rows}


def _tag(sid: str, fresh_ids: set[str], tier2_ids: set[str]) -> str:
    in_f, in_t = sid in fresh_ids, sid in tier2_ids
    if in_f and in_t:
        return "overlap"
    if in_f:
        return "fresh_only"
    return "tier2_only"


def _exit_date(full_dates: list[str], entry_date: str, hold_days: int) -> str | None:
    if entry_date not in full_dates:
        return None
    i = full_dates.index(entry_date)
    j = i + int(hold_days)
    if j >= len(full_dates):
        return None
    return full_dates[j]


def _fwd_excess(
    close,
    bench,
    full_dates: list[str],
    *,
    stock_id: str,
    entry_date: str,
    entry_px: float,
    hold_days: int,
) -> float | None:
    ex = _exit_date(full_dates, entry_date, hold_days)
    if not ex or stock_id not in close.columns:
        return None
    try:
        px_out = float(close.at[ex, stock_id])
        b0 = float(bench.at[entry_date])
        b1 = float(bench.at[ex])
    except (KeyError, TypeError, ValueError):
        return None
    if entry_px <= 0 or px_out <= 0 or b0 <= 0 or b1 <= 0:
        return None
    if px_out != px_out or b0 != b0 or b1 != b1:
        return None
    stock_ret = (px_out / entry_px - 1.0) * 100.0
    bench_ret = (b1 / b0 - 1.0) * 100.0
    return round(stock_ret - bench_ret, 4)


def _fire_c0_entry(
    conn: sqlite3.Connection,
    *,
    pool: list[Any],
    session: str,
    poll_minute: str,
    close,
    kbar_cache: dict,
    confirm: dict[str, int],
    need: int,
) -> tuple[str | None, str, float | None]:
    """Mirror buy_observation.evaluate_pool_observation C0 path · returns (sid, name, px)."""
    if not pool:
        return None, "", None
    ranked = rank_shortlist_scale(
        pool,
        conn=conn,
        close=close,
        trade_date=session,
        minute=poll_minute,
        kbar_cache=kbar_cache,
    )
    if not ranked:
        return None, "", None
    top_ids = {r.stock_id for r in ranked[:3]}
    for sid in list(confirm.keys()):
        if sid not in top_ids:
            confirm[sid] = 0
    for row in ranked:
        if row.stock_id in top_ids:
            confirm[row.stock_id] = int(confirm.get(row.stock_id) or 0) + 1
    for row in ranked:
        sid = row.stock_id
        if int(confirm.get(sid) or 0) < need:
            continue
        key = (sid, session)
        if key not in kbar_cache:
            kbar_cache[key] = load_kbar_day_closes(conn, sid, session)
        px = price_at_or_before_minute(kbar_cache[key], poll_minute)
        if px is None or float(px) <= 0:
            continue
        return sid, str(row.stock_name or ""), float(px)
    return None, "", None


def _layer_stats(events: list[dict[str, Any]], hold: int) -> dict[str, Any]:
    xs = [
        float(e["fwd"][str(hold)])
        for e in events
        if e.get("fwd", {}).get(str(hold)) is not None
    ]
    if not xs:
        return {"n": 0, "mean_excess_pct": None, "hit_rate_pct": None}
    hits = sum(1 for x in xs if x > 0)
    return {
        "n": len(xs),
        "mean_excess_pct": round(sum(xs) / len(xs), 4),
        "hit_rate_pct": round(100.0 * hits / len(xs), 2),
    }


def run_buy_radar_c0_fresh_tier2_events(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str | None = None,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    if len(trade_dates) < 10:
        raise ValueError(f"need ≥10 trade dates, got {len(trade_dates)}")

    cut = is_end or _is_cut_date(trade_dates, ratio=0.7)
    oos_start = next((d for d in trade_dates if d > cut), None)
    if not oos_start:
        raise ValueError(f"no OOS after {cut}")

    # Calendars keyed by signal date (PIT as-of) · include priors before window
    i0 = full_dates.index(trade_dates[0]) if trade_dates[0] in full_dates else 0
    i1 = full_dates.index(trade_dates[-1]) if trade_dates[-1] in full_dates else len(full_dates) - 1
    cal_dates = full_dates[max(0, i0 - 5) : i1 + 1]
    print("building fresh / tier2 calendars …", flush=True)
    fresh_by = build_fresh_mono_calendar(conn, cal_dates)
    tier2_by = build_mono_tier2_calendar(conn, cal_dates, close=close, bench=bench)

    polls = _poll_minutes()
    need = CONFIRM_BARS
    events: list[dict[str, Any]] = []
    kbar_cache: dict[tuple[str, str], tuple] = {}

    for i, session in enumerate(trade_dates):
        if (i + 1) % 40 == 0 or i == 0:
            print(f"  events day {i + 1}/{len(trade_dates)} {session} …", flush=True)
        pit = _signal_date_for_session(full_dates, session)
        if not pit:
            continue
        fresh_rows = list(fresh_by.get(pit) or [])
        tier2_rows = list(tier2_by.get(pit) or [])
        if not fresh_rows and not tier2_rows:
            continue
        fresh_ids = _pool_ids(fresh_rows)
        tier2_ids = _pool_ids(tier2_rows)
        # Ensure session row exists for scale (radar injects NaN row if missing)
        if session not in close.index:
            close = close.copy()
            close.loc[session] = float("nan")

        confirm_f: dict[str, int] = {}
        confirm_t: dict[str, int] = {}
        seen: set[str] = set()
        for minute in polls:
            for pool, confirm, _src in (
                (fresh_rows, confirm_f, "fresh"),
                (tier2_rows, confirm_t, "tier2"),
            ):
                sid, name, px = _fire_c0_entry(
                    conn,
                    pool=pool,
                    session=session,
                    poll_minute=minute,
                    close=close,
                    kbar_cache=kbar_cache,
                    confirm=confirm,
                    need=need,
                )
                if not sid or sid in seen or px is None:
                    continue
                seen.add(sid)
                fwd = {
                    str(h): _fwd_excess(
                        close,
                        bench,
                        full_dates,
                        stock_id=sid,
                        entry_date=session,
                        entry_px=float(px),
                        hold_days=h,
                    )
                    for h in HOLD_DAYS
                }
                events.append(
                    {
                        "stock_id": sid,
                        "stock_name": name,
                        "session_date": session,
                        "pool_as_of": pit,
                        "entry_minute": minute,
                        "entry_px": round(float(px), 4),
                        "layer": _tag(sid, fresh_ids, tier2_ids),
                        "fwd": fwd,
                    }
                )

    def _slice(evs: list[dict[str, Any]], *, start: str, end: str) -> list[dict[str, Any]]:
        return [e for e in evs if start <= str(e.get("session_date") or "") <= end]

    layers = ("overlap", "fresh_only", "tier2_only", "all")
    slices = {
        "full": (date_start, end),
        "is": (date_start, cut),
        "oos": (oos_start, end),
    }
    by_layer: dict[str, dict[str, Any]] = {}
    for layer in layers:
        base = events if layer == "all" else [e for e in events if e.get("layer") == layer]
        by_layer[layer] = {}
        for sk, (a, b) in slices.items():
            sub = _slice(base, start=a, end=b)
            by_layer[layer][sk] = {
                "n_events": len(sub),
                "by_hold": {str(h): _layer_stats(sub, h) for h in HOLD_DAYS},
            }

    oos_overlap_5 = (
        (by_layer.get("overlap") or {}).get("oos", {}).get("by_hold", {}).get("5") or {}
    )
    edge = (
        int(oos_overlap_5.get("n") or 0) >= SIGNAL_EDGE_OOS_N
        and oos_overlap_5.get("mean_excess_pct") is not None
        and float(oos_overlap_5["mean_excess_pct"]) >= SIGNAL_EDGE_OOS_5D_MIN
    )

    case = next(
        (
            e
            for e in events
            if e.get("stock_id") == CASE_STOCK and e.get("session_date") == CASE_DATE
        ),
        None,
    )
    if case is None:
        # still report membership that day
        pit_case = _signal_date_for_session(full_dates, CASE_DATE)
        f_ids = _pool_ids(list(fresh_by.get(pit_case) or [])) if pit_case else set()
        t_ids = _pool_ids(list(tier2_by.get(pit_case) or [])) if pit_case else set()
        case = {
            "stock_id": CASE_STOCK,
            "session_date": CASE_DATE,
            "found": False,
            "in_fresh": CASE_STOCK in f_ids,
            "in_tier2": CASE_STOCK in t_ids,
            "pool_as_of": pit_case,
        }
    else:
        case = {**case, "found": True}

    verdict = {
        "signal_edge": "SIGNAL_EDGE" if edge else "NO_EDGE",
        "gates": {
            "oos_overlap_n_min": SIGNAL_EDGE_OOS_N,
            "oos_overlap_5d_mean_excess_min_pp": SIGNAL_EDGE_OOS_5D_MIN,
        },
        "oos_overlap_5d": oos_overlap_5,
        "summary": (
            f"{'SIGNAL_EDGE' if edge else 'NO_EDGE'} · OOS overlap 5d "
            f"n={oos_overlap_5.get('n')} excess={oos_overlap_5.get('mean_excess_pct')}"
        ),
    }

    # Slim events for JSON (keep all; file size OK for ~few k events)
    return {
        "schema": SCHEMA,
        "topic": TOPIC_ID,
        "date_start": date_start,
        "date_end": end,
        "is_end": cut,
        "oos_start": oos_start,
        "n_trade_dates": len(trade_dates),
        "pool_mode": "prior_session_PIT",
        "poll_start": POLL_START,
        "confirm_bars": CONFIRM_BARS,
        "n_events": len(events),
        "by_layer": by_layer,
        "case_6505": case,
        "verdict": verdict,
        "events_sample": events[:50],
        "events": events,
    }


def render_buy_radar_c0_fresh_tier2_events_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    lines = [
        "# Buy-signal-radar C0 · fresh / tier2 events (Part B)",
        "",
        f"- Topic：`{payload.get('topic')}`",
        f"- Window：`{payload.get('date_start')}` → `{payload.get('date_end')}` · "
        f"{payload.get('n_trade_dates')} TD",
        f"- Pool：`{payload.get('pool_mode')}` · polls ≥`{payload.get('poll_start')}` · "
        f"confirm={payload.get('confirm_bars')}",
        f"- Events：{payload.get('n_events')}",
        f"- Verdict：{v.get('summary')}",
        "",
        "## Layer KPI（5d mean excess）",
        "",
        "| layer | FULL n | FULL 5d | OOS n | OOS 5d | OOS hit% |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for layer in ("overlap", "fresh_only", "tier2_only", "all"):
        bl = (payload.get("by_layer") or {}).get(layer) or {}
        f5 = (bl.get("full") or {}).get("by_hold", {}).get("5") or {}
        o5 = (bl.get("oos") or {}).get("by_hold", {}).get("5") or {}
        lines.append(
            f"| `{layer}` | {f5.get('n')} | {f5.get('mean_excess_pct')} | "
            f"{o5.get('n')} | {o5.get('mean_excess_pct')} | {o5.get('hit_rate_pct')} |"
        )
    case = payload.get("case_6505") or {}
    lines += [
        "",
        "## Case · 6505 台塑化 @ 2026-07-07",
        "",
        f"- found：`{case.get('found')}` · layer=`{case.get('layer')}` · "
        f"minute=`{case.get('entry_minute')}` · px=`{case.get('entry_px')}`",
        f"- pool_as_of=`{case.get('pool_as_of')}` · fwd=`{case.get('fwd')}`",
        "",
        "## Gates",
        "",
        f"- OOS overlap 5d mean_excess ≥ {SIGNAL_EDGE_OOS_5D_MIN}pp · n≥{SIGNAL_EDGE_OOS_N}",
        f"- Decision：`{v.get('signal_edge')}` · **不改 Order**",
        "",
        "---",
        "模組：`src/research/backtest/buy_radar_c0_fresh_tier2_events.py`",
        "",
    ]
    return "\n".join(lines)
