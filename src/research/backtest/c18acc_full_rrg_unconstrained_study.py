"""C18acc · full_rrg vs PIT fresh · unconstrained all-signals event study."""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import replace
from typing import Any, Literal

from flow_returns import exit_close_date_from_entry, return_pct
from market_benchmark import load_benchmark_close
from research.backtest.archive.abc_v3_slot_sweep import _unconstrained_leg_stats
from research.backtest.archive.c18acc_hold3d_event_study import (
    EXIT_MINUTE,
    _prepare_c18acc_context,
)
from research.backtest.archive.c18acc_pool1_showcase import _ensure_avoid_mixed_gate
from research.backtest.dual_wma_signal_backtest import ROUND_TRIP_COST_PCT
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_intraday_ab import (
    CVariantConfig,
    DEFAULT_C_SWEEP,
    _kbar_px,
    _trading_offset,
)
from rrg_mono_daily_brief import LENGTH, ScanRow, _feat, _mono_tier2
from rrg_rotation import compute_rrg_panel
from stock_db.kbar import load_kbar_day_5m_closes, price_at_or_before_minute

VariantId = Literal["V0", "V1", "V2", "V3", "V4"]

DEFAULT_GATE_CACHE = (
    "reports/research/rrg/"
    "20260711_c18acc_avoid_mixed_gate_live_poll5m_2025-01-02_2026-07-09.json"
)
CONFIRM_ANCHOR_MINUTE = "09:30"
G2_OOS_MIN_N = 30
G2_OOS_MIN_DELTA_PP = 0.3
G3_IS_MIN_DELTA_PP = -0.2
G5_MIN_COVERAGE_PCT = 70.0


def _entry_event_set(events: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(str(e["stock_id"]), str(e["entry_date"])) for e in events}


def _leg_stats(legs: list[dict[str, Any]], *, hold_days: int) -> dict[str, Any]:
    """Event-level stats · aligned with `_unconstrained_leg_stats` + excess."""
    base = _unconstrained_leg_stats(
        [{"net_pct": lg.get("net_pct"), **lg} for lg in legs if lg.get("net_pct") is not None]
    )
    if not base.get("n"):
        return {"n": 0, "hold_days": hold_days}
    excess = [float(lg["excess_pct"]) for lg in legs if lg.get("excess_pct") is not None]
    if excess:
        base["mean_excess_pct"] = round(sum(excess) / len(excess), 3)
        base["median_excess_pct"] = round(statistics.median(excess), 3)
    if hold_days > 0 and base.get("mean_ret_net_pct") is not None:
        base["mean_daily_ret_net_pct"] = round(
            float(base["mean_ret_net_pct"]) / hold_days, 4
        )
    base["mean_ret_pct"] = base.pop("mean_ret_net_pct", None)
    base["win_rate_pct"] = base.get("win_rate_pct")
    base["hold_days"] = hold_days
    return base


def _slice_legs(
    legs: list[dict[str, Any]], *, start: str, end: str
) -> list[dict[str, Any]]:
    return [
        lg
        for lg in legs
        if start <= str(lg.get("entry_date") or "") <= end
    ]


def _variant_slices(
    legs: list[dict[str, Any]],
    *,
    date_start: str,
    date_end: str,
    is_end: str,
    oos_start: str | None,
    hold_days: int,
) -> dict[str, dict[str, Any]]:
    full = _leg_stats(legs, hold_days=hold_days)
    is_stats = _leg_stats(
        _slice_legs(legs, start=date_start, end=is_end),
        hold_days=hold_days,
    )
    if oos_start:
        oos_stats = _leg_stats(
            _slice_legs(legs, start=oos_start, end=date_end),
            hold_days=hold_days,
        )
    else:
        oos_stats = {"n": 0, "hold_days": hold_days}
    return {"full": full, "is": is_stats, "oos": oos_stats}


def _strict_kbar_close(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    minute: str,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> float | None:
    key = (stock_id, trade_date)
    if key not in kbar_cache:
        kbar_cache[key] = load_kbar_day_5m_closes(conn, stock_id, trade_date)
    return price_at_or_before_minute(kbar_cache[key], minute)


def _qualify_v0_pit_fresh_ids(ctx: dict[str, Any], signal_as_of: str) -> set[str]:
    rows = ctx["fresh_by_date"].get(signal_as_of, [])
    return {r.stock_id for r in rows}


def _pit_fresh_shortlist(ctx: dict[str, Any], signal_as_of: str) -> list[ScanRow]:
    """Same universe as V0 · PIT fresh mono @ T-1."""
    return list(ctx["fresh_by_date"].get(signal_as_of, []))


def _qualify_full_rrg_mono_ids(
    shortlist: list[ScanRow],
    *,
    conn: sqlite3.Connection,
    close: Any,
    bench: Any,
    trade_date: str,
    minute: str,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
    full_rrg_cache: dict[tuple[Any, ...], set[str]] | None = None,
) -> set[str]:
    """PIT shortlist revalidated via full_rrg · still mono_tier2 @ poll (not _fresh_mono).

    Note: ``_fresh_mono`` on entry day contradicts PIT-fresh@T-1 (fresh is a one-day
    transition). We test whether intraday RRG still supports **mono tier2** structure.
    """
    if not shortlist or trade_date not in close.index:
        return set()
    sid_key = tuple(sorted(r.stock_id for r in shortlist))
    cache_key = (trade_date, minute, sid_key, "mono_tier2")
    if full_rrg_cache is not None and cache_key in full_rrg_cache:
        return set(full_rrg_cache[cache_key])

    prov = close.copy()
    bench_p = bench.reindex(prov.index).astype(float)
    allow = {r.stock_id for r in shortlist}
    passed: set[str] = set()
    for row in shortlist:
        sid = row.stock_id
        px = _kbar_px(conn, sid, trade_date, minute, kbar_cache, close)
        if px is not None and px > 0:
            prov.at[trade_date, sid] = float(px)

    rs_r, rs_m, _ = compute_rrg_panel(prov, bench_p, length=LENGTH)
    full_dates = prov.index.astype(str).tolist()
    si = full_dates.index(trade_date)
    for sid in allow:
        f = _feat(rs_r, rs_m, full_dates, si, sid)
        if f is not None and _mono_tier2(f):
            passed.add(sid)
    if full_rrg_cache is not None:
        full_rrg_cache[cache_key] = set(passed)
    return passed


def _apply_gate(
    ids: set[str],
    entry_date: str,
    gate: dict[str, set[str]] | None,
) -> set[str]:
    if gate is None:
        return ids
    allowed = gate.get(entry_date, set())
    return {sid for sid in ids if sid in allowed}


def _confirmed_v1_ids(
    ctx: dict[str, Any],
    conn: sqlite3.Connection,
    *,
    entry_date: str,
    signal_as_of: str,
    entry_minute: str,
    confirm_anchor_minute: str,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
    full_rrg_cache: dict[tuple[Any, ...], set[str]] | None,
) -> set[str]:
    shortlist = _pit_fresh_shortlist(ctx, signal_as_of)
    kw = {
        "conn": conn,
        "close": ctx["close"],
        "bench": ctx["bench"],
        "trade_date": entry_date,
        "kbar_cache": kbar_cache,
        "full_rrg_cache": full_rrg_cache,
    }
    at_anchor = _qualify_full_rrg_mono_ids(
        shortlist, minute=confirm_anchor_minute, **kw
    )
    at_entry = _qualify_full_rrg_mono_ids(shortlist, minute=entry_minute, **kw)
    return at_anchor & at_entry


def _events_from_ids(
    ids: set[str],
    *,
    entry_date: str,
    signal_as_of: str,
    variant: VariantId,
    ctx: dict[str, Any],
) -> list[dict[str, Any]]:
    name_map: dict[str, str] = {}
    for rows in (ctx["fresh_by_date"].get(signal_as_of, []), ctx["mono_by_date"].get(signal_as_of, [])):
        for r in rows:
            name_map[r.stock_id] = r.stock_name
    return [
        {
            "stock_id": sid,
            "stock_name": name_map.get(sid, ""),
            "entry_date": entry_date,
            "signal_as_of": signal_as_of,
            "variant": variant,
        }
        for sid in sorted(ids)
    ]


def derive_set_variant_events(
    v0_events: list[dict[str, Any]],
    v1_events: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """V2 intersection · V3 full_rrg-only-new · V4 PIT-only-dropped."""
    s0 = _entry_event_set(v0_events)
    s1 = _entry_event_set(v1_events)
    by_key = {(str(e["stock_id"]), str(e["entry_date"])): e for e in v0_events + v1_events}

    def _pick(keys: set[tuple[str, str]], variant: VariantId) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for key in sorted(keys):
            row = dict(by_key[key])
            row["variant"] = variant
            out.append(row)
        return out

    return {
        "V2": _pick(s0 & s1, "V2"),
        "V3": _pick(s1 - s0, "V3"),
        "V4": _pick(s0 - s1, "V4"),
    }


def compute_unconstrained_leg(
    conn: sqlite3.Connection,
    *,
    stock_id: str,
    entry_date: str,
    entry_minute: str,
    hold_days: int,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
    bench: Any | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Hold-Nd leg · strict kbar entry (no daily-close fallback)."""
    exit_date = exit_close_date_from_entry(conn, entry_date, hold_days)
    if not exit_date:
        return None, "no_exit_date"
    px_in = _strict_kbar_close(conn, stock_id, entry_date, entry_minute, kbar_cache)
    if px_in is None or px_in <= 0:
        return None, "missing_entry_kbar"
    px_out = _strict_kbar_close(conn, stock_id, exit_date, EXIT_MINUTE, kbar_cache)
    if px_out is None or px_out <= 0:
        return None, "missing_exit_kbar"
    if bench is None:
        close, _, _ = load_price_panels(conn)
        bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    b_in = _strict_kbar_close(conn, "IX0001", entry_date, entry_minute, kbar_cache)
    b_out = _strict_kbar_close(conn, "IX0001", exit_date, EXIT_MINUTE, kbar_cache)
    if (b_in is None or b_in <= 0) and entry_date in bench.index:
        b_in = float(bench.at[entry_date])
    if (b_out is None or b_out <= 0) and exit_date in bench.index:
        b_out = float(bench.at[exit_date])
    if not b_in or not b_out:
        return None, "missing_bench_px"
    gross = return_pct(float(px_in), float(px_out))
    bench_ret = return_pct(float(b_in), float(b_out))
    net = gross - ROUND_TRIP_COST_PCT
    return (
        {
            "stock_id": stock_id,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "entry_minute": entry_minute,
            "exit_minute": EXIT_MINUTE,
            "entry_px": round(float(px_in), 4),
            "exit_px": round(float(px_out), 4),
            "gross_pct": round(gross, 3),
            "net_pct": round(net, 3),
            "excess_pct": round(net - bench_ret, 3),
            "hold_days": hold_days,
        },
        None,
    )


def events_to_legs(
    conn: sqlite3.Connection,
    events: list[dict[str, Any]],
    *,
    entry_minute: str,
    hold_days: int,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
    bench: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    legs: list[dict[str, Any]] = []
    skips: dict[str, int] = {}
    for ev in events:
        leg, reason = compute_unconstrained_leg(
            conn,
            stock_id=str(ev["stock_id"]),
            entry_date=str(ev["entry_date"]),
            entry_minute=entry_minute,
            hold_days=hold_days,
            kbar_cache=kbar_cache,
            bench=bench,
        )
        if leg is None:
            skips[reason or "unknown"] = skips.get(reason or "unknown", 0) + 1
            continue
        legs.append({**leg, "variant": ev.get("variant"), "signal_as_of": ev.get("signal_as_of")})
    return legs, skips


def collect_daily_event_sets(
    ctx: dict[str, Any],
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    entry_minute: str = "09:35",
    confirm_anchor_minute: str = CONFIRM_ANCHOR_MINUTE,
    gate: dict[str, set[str]] | None = None,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] | None = None,
    full_rrg_cache: dict[tuple[Any, ...], set[str]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kb = kbar_cache if kbar_cache is not None else {}
    rrg_cache = full_rrg_cache if full_rrg_cache is not None else {}
    v0_events: list[dict[str, Any]] = []
    v1_events: list[dict[str, Any]] = []
    full_dates = ctx["full_dates"]

    for entry_date in trade_dates:
        signal_as_of = _trading_offset(full_dates, entry_date, -1)
        if not signal_as_of:
            continue
        v0_ids = _apply_gate(
            _qualify_v0_pit_fresh_ids(ctx, signal_as_of),
            entry_date,
            gate,
        )
        v1_ids = _apply_gate(
            _confirmed_v1_ids(
                ctx,
                conn,
                entry_date=entry_date,
                signal_as_of=signal_as_of,
                entry_minute=entry_minute,
                confirm_anchor_minute=confirm_anchor_minute,
                kbar_cache=kb,
                full_rrg_cache=rrg_cache,
            ),
            entry_date,
            gate,
        )
        v0_events.extend(
            _events_from_ids(
                v0_ids,
                entry_date=entry_date,
                signal_as_of=signal_as_of,
                variant="V0",
                ctx=ctx,
            )
        )
        v1_events.extend(
            _events_from_ids(
                v1_ids,
                entry_date=entry_date,
                signal_as_of=signal_as_of,
                variant="V1",
                ctx=ctx,
            )
        )
    return v0_events, v1_events


def _entry_c_config(confirm_bars: int) -> CVariantConfig:
    base = next(c for c in DEFAULT_C_SWEEP if c.variant_id == ("C0" if confirm_bars <= 1 else "C3"))
    if int(base.confirm_bars) == int(confirm_bars):
        return base
    return replace(base, confirm_bars=int(confirm_bars))


def _build_verdict_gates(payload: dict[str, Any]) -> dict[str, Any]:
    v0 = payload.get("variants", {}).get("V0", {})
    v1 = payload.get("variants", {}).get("V1", {})
    v3 = payload.get("variants", {}).get("V3", {})
    v4 = payload.get("variants", {}).get("V4", {})
    shared = payload.get("set_ops", {}).get("shared_n", 0)

    v0_oos = (v0.get("slices") or {}).get("oos") or {}
    v1_oos = (v1.get("slices") or {}).get("oos") or {}
    v0_is = (v0.get("slices") or {}).get("is") or {}
    v1_is = (v1.get("slices") or {}).get("is") or {}
    v3_full = (v3.get("slices") or {}).get("full") or {}
    v4_full = (v4.get("slices") or {}).get("full") or {}
    shared_stats = (payload.get("set_ops") or {}).get("shared_stats") or {}

    delta_oos = None
    if v0_oos.get("mean_excess_pct") is not None and v1_oos.get("mean_excess_pct") is not None:
        delta_oos = round(float(v1_oos["mean_excess_pct"]) - float(v0_oos["mean_excess_pct"]), 4)
    delta_is = None
    if v0_is.get("mean_excess_pct") is not None and v1_is.get("mean_excess_pct") is not None:
        delta_is = round(float(v1_is["mean_excess_pct"]) - float(v0_is["mean_excess_pct"]), 4)

    oos_n = int(v1_oos.get("n") or 0)
    g2_pass = (
        oos_n >= G2_OOS_MIN_N
        and delta_oos is not None
        and delta_oos >= G2_OOS_MIN_DELTA_PP
    )
    g3_pass = delta_is is None or delta_is >= G3_IS_MIN_DELTA_PP

    v3_ex = v3_full.get("mean_excess_pct")
    v4_ex = v4_full.get("mean_excess_pct")
    shared_ex = shared_stats.get("mean_excess_pct")
    g4_v3_pass = v3_ex is None or float(v3_ex) >= 0.0
    g4_v4_pass = (
        v4_ex is None
        or shared_ex is None
        or float(v4_ex) <= float(shared_ex) + 0.2
    )

    v0_n_events = int((payload.get("event_counts") or {}).get("V0") or 0)
    v1_n_legs = int(v1.get("n_legs") or 0)
    coverage_pct = round(100.0 * v1_n_legs / max(v0_n_events, 1), 2) if v0_n_events else None
    g5_pass = coverage_pct is not None and coverage_pct >= G5_MIN_COVERAGE_PCT

    return {
        "G2_oos": {
            "threshold_pp": G2_OOS_MIN_DELTA_PP,
            "min_n": G2_OOS_MIN_N,
            "delta_pp": delta_oos,
            "n": oos_n,
            "pass": g2_pass,
        },
        "G3_is": {
            "threshold_pp": G3_IS_MIN_DELTA_PP,
            "delta_pp": delta_is,
            "pass": g3_pass,
        },
        "G4_boundary": {
            "v3_mean_excess_pct": v3_ex,
            "v4_mean_excess_pct": v4_ex,
            "shared_mean_excess_pct": shared_ex,
            "v3_pass": g4_v3_pass,
            "v4_pass": g4_v4_pass,
            "pass": g4_v3_pass and g4_v4_pass,
        },
        "G5_coverage": {
            "min_pct": G5_MIN_COVERAGE_PCT,
            "coverage_pct": coverage_pct,
            "pass": g5_pass,
        },
        "G6_live_feasible": {"status": "pending", "pass": None},
        "shared_events_n": shared,
    }


def _build_variant_payload(
    conn: sqlite3.Connection,
    events: list[dict[str, Any]],
    *,
    variant_id: VariantId,
    label: str,
    date_start: str,
    date_end: str,
    is_end: str,
    oos_start: str | None,
    entry_minute: str,
    hold_days: int,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
    bench: Any,
) -> dict[str, Any]:
    legs, skips = events_to_legs(
        conn,
        events,
        entry_minute=entry_minute,
        hold_days=hold_days,
        kbar_cache=kbar_cache,
        bench=bench,
    )
    return {
        "variant_id": variant_id,
        "label": label,
        "n_events": len(events),
        "n_legs": len(legs),
        "skip_reasons": skips,
        "slices": _variant_slices(
            legs,
            date_start=date_start,
            date_end=date_end,
            is_end=is_end,
            oos_start=oos_start,
            hold_days=hold_days,
        ),
        "legs": legs,
    }


def run_c18acc_full_rrg_unconstrained_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str = "2025-12-31",
    hold_days: int = 5,
    entry_minute: str = "09:35",
    confirm_bars: int = 2,
    confirm_anchor_minute: str = CONFIRM_ANCHOR_MINUTE,
    gate_cache_path: str | None = DEFAULT_GATE_CACHE,
    avoid_spread_mixed: bool = True,
) -> dict[str, Any]:
    """Unconstrained event study · V0 PIT fresh vs V1 full_rrg mono_tier2 revalidation."""
    if confirm_bars != 2:
        raise ValueError("Phase 0 supports confirm_bars=2 only (09:30+09:35 anchor)")
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    if date_end is None:
        date_end = full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    if len(trade_dates) < 5:
        raise ValueError(f"need ≥5 trade dates, got {len(trade_dates)}")

    oos_start = next((d for d in trade_dates if d > is_end), None)
    ctx = _prepare_c18acc_context(conn, trade_dates)
    bench = ctx["bench"]

    gate: dict[str, set[str]] | None = None
    gate_meta: dict[str, Any] = {}
    if avoid_spread_mixed and gate_cache_path:
        gate, gate_meta = _ensure_avoid_mixed_gate(
            conn,
            trade_dates,
            gate_cache_path=gate_cache_path,
            live_aligned=True,
            n_slots=3,
        )

    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    full_rrg_cache: dict[tuple[Any, ...], set[str]] = {}

    v0_events, v1_events = collect_daily_event_sets(
        ctx,
        conn,
        trade_dates,
        entry_minute=entry_minute,
        confirm_anchor_minute=confirm_anchor_minute,
        gate=gate,
        kbar_cache=kbar_cache,
        full_rrg_cache=full_rrg_cache,
    )
    set_events = derive_set_variant_events(v0_events, v1_events)

    variants: dict[str, Any] = {}
    variants["V0"] = _build_variant_payload(
        conn,
        v0_events,
        variant_id="V0",
        label="PIT fresh @ T-1 · scale qualification",
        date_start=date_start,
        date_end=date_end,
        is_end=is_end,
        oos_start=oos_start,
        entry_minute=entry_minute,
        hold_days=hold_days,
        kbar_cache=kbar_cache,
        bench=bench,
    )
    variants["V1"] = _build_variant_payload(
        conn,
        v1_events,
        variant_id="V1",
        label=f"PIT fresh ∩ full_rrg mono_tier2 @ {entry_minute}",
        date_start=date_start,
        date_end=date_end,
        is_end=is_end,
        oos_start=oos_start,
        entry_minute=entry_minute,
        hold_days=hold_days,
        kbar_cache=kbar_cache,
        bench=bench,
    )
    for vid, label in (
        ("V2", "PIT ∩ full_rrg-pass"),
        ("V3", "full_rrg-only-new (V1 \\ V0)"),
        ("V4", "PIT-only-dropped (V0 \\ V1)"),
    ):
        variants[vid] = _build_variant_payload(
            conn,
            set_events[vid],
            variant_id=vid,  # type: ignore[arg-type]
            label=label,
            date_start=date_start,
            date_end=date_end,
            is_end=is_end,
            oos_start=oos_start,
            entry_minute=entry_minute,
            hold_days=hold_days,
            kbar_cache=kbar_cache,
            bench=bench,
        )

    s0 = _entry_event_set(v0_events)
    s1 = _entry_event_set(v1_events)
    shared_keys = s0 & s1
    shared_legs = [
        lg
        for lg in variants["V0"]["legs"]
        if (str(lg["stock_id"]), str(lg["entry_date"])) in shared_keys
    ]

    entry_cfg = _entry_c_config(confirm_bars)
    payload: dict[str, Any] = {
        "schema": "c18acc_full_rrg_unconstrained-v1",
        "date_start": date_start,
        "date_end": date_end,
        "is_end": is_end,
        "oos_start": oos_start,
        "n_trade_dates": len(trade_dates),
        "hold_days": hold_days,
        "entry_minute": entry_minute,
        "confirm_bars": confirm_bars,
        "confirm_anchor_minute": confirm_anchor_minute,
        "entry_variant_id": entry_cfg.variant_id,
        "avoid_spread_mixed": avoid_spread_mixed,
        "gate_cache_path": gate_cache_path if avoid_spread_mixed else None,
        "gate_meta": gate_meta,
        "event_counts": {
            "V0": len(v0_events),
            "V1": len(v1_events),
            "V2": len(set_events["V2"]),
            "V3": len(set_events["V3"]),
            "V4": len(set_events["V4"]),
        },
        "set_ops": {
            "shared_n": len(shared_keys),
            "v0_only_n": len(s0 - s1),
            "v1_only_n": len(s1 - s0),
            "shared_stats": _leg_stats(shared_legs, hold_days=hold_days),
        },
        "variants": variants,
        "cost_round_trip_pct": ROUND_TRIP_COST_PCT,
    }
    payload["verdict_gates"] = _build_verdict_gates(payload)
    delta_full = None
    v0_full = variants["V0"]["slices"]["full"]
    v1_full = variants["V1"]["slices"]["full"]
    if v0_full.get("mean_excess_pct") is not None and v1_full.get("mean_excess_pct") is not None:
        delta_full = round(float(v1_full["mean_excess_pct"]) - float(v0_full["mean_excess_pct"]), 4)
    payload["comparison"] = {
        "v1_vs_v0_full_delta_excess_pp": delta_full,
        "v0_n_events": len(v0_events),
        "v1_n_events": len(v1_events),
        "v0_n_legs": variants["V0"]["n_legs"],
        "v1_n_legs": variants["V1"]["n_legs"],
    }
    return payload


def render_c18acc_full_rrg_unconstrained_md(payload: dict[str, Any]) -> str:
    gates = payload.get("verdict_gates") or {}
    cmp_ = payload.get("comparison") or {}
    lines = [
        "# C18acc · full_rrg vs PIT fresh · unconstrained event study",
        "",
        f"窗口：**{payload['date_start']}** .. **{payload['date_end']}** · "
        f"hold **{payload.get('hold_days')}d** · entry **{payload.get('entry_minute')}** · "
        f"confirm_bars={payload.get('confirm_bars')} "
        f"({payload.get('confirm_anchor_minute')}+entry)",
        "",
        f"OOS 起點：{payload.get('oos_start') or '—'}（IS 截止 {payload.get('is_end')}）",
        "",
        "> V1 = PIT fresh 候選 · 09:30+09:35 盤中 full_rrg 重算 · 仍符合 **mono_tier2**"
        "（非 `_fresh_mono`：昨收 fresh 與當日 fresh 轉換互斥）。",
        "",
        "## 事件集合",
        "",
        "| set | n events | n legs |",
        "|-----|--------:|-------:|",
    ]
    for vid in ("V0", "V1", "V2", "V3", "V4"):
        v = (payload.get("variants") or {}).get(vid) or {}
        lines.append(f"| {vid} · {v.get('label', vid)} | {v.get('n_events', '—')} | {v.get('n_legs', '—')} |")
    so = payload.get("set_ops") or {}
    lines += [
        "",
        f"- 共有事件：{so.get('shared_n')} · PIT-only：{so.get('v0_only_n')} · full_rrg-only：{so.get('v1_only_n')}",
        f"- V1 vs V0 FULL Δexcess：**{cmp_.get('v1_vs_v0_full_delta_excess_pp')}pp**",
        "",
        "## 分段 · V0 vs V1",
        "",
        "| window | V0 n | V0 excess% | V1 n | V1 excess% | Δ pp |",
        "|--------|-----:|-----------:|-----:|-----------:|-----:|",
    ]
    for win in ("full", "is", "oos"):
        v0 = (payload.get("variants") or {}).get("V0", {}).get("slices", {}).get(win) or {}
        v1 = (payload.get("variants") or {}).get("V1", {}).get("slices", {}).get(win) or {}
        d = None
        if v0.get("mean_excess_pct") is not None and v1.get("mean_excess_pct") is not None:
            d = round(float(v1["mean_excess_pct"]) - float(v0["mean_excess_pct"]), 4)
        lines.append(
            f"| {win.upper()} | {v0.get('n', '—')} | {v0.get('mean_excess_pct', '—')} "
            f"| {v1.get('n', '—')} | {v1.get('mean_excess_pct', '—')} | {d if d is not None else '—'} |"
        )
    lines += [
        "",
        "## 採納閘門（Research · Phase 1 判定）",
        "",
        "| gate | 門檻 | 實測 | pass |",
        "|------|------|------|:----:|",
    ]
    g2 = gates.get("G2_oos") or {}
    g3 = gates.get("G3_is") or {}
    g4 = gates.get("G4_boundary") or {}
    g5 = gates.get("G5_coverage") or {}
    lines += [
        f"| G2 OOS | Δ≥{g2.get('threshold_pp')}pp · n≥{g2.get('min_n')} "
        f"| Δ={g2.get('delta_pp')}pp n={g2.get('n')} | {'✓' if g2.get('pass') else '—'} |",
        f"| G3 IS | Δ≥{g3.get('threshold_pp')}pp | Δ={g3.get('delta_pp')}pp | "
        f"{'✓' if g3.get('pass') else '—'} |",
        f"| G4 boundary | V3≥0 · V4≤shared+0.2pp | V3={g4.get('v3_mean_excess_pct')} "
        f"V4={g4.get('v4_mean_excess_pct')} | {'✓' if g4.get('pass') else '—'} |",
        f"| G5 coverage | ≥{g5.get('min_pct')}% | {g5.get('coverage_pct')}% | "
        f"{'✓' if g5.get('pass') else '—'} |",
        f"| G6 live | pending | — | — |",
        "",
        f"模組：`src/research/backtest/c18acc_full_rrg_unconstrained_study.py`",
    ]
    return "\n".join(lines) + "\n"
