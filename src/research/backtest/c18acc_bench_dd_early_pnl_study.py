"""C18acc · early entry when bench draws down from RTH high (low trigger).

Rule (PIT):
- Default entry @13:00 with same-day provisional mono fresh（現行近收）.
- Walk RTH 5m bars ≤13:00; if bar.low / running_session_high − 1 ≤ −thresh,
  trigger on that bar and fill empty slots on the **next** 5m bar close.
- Bench: prefer IX0001 5m（Yahoo ^TWII ≈60d）；else 0050 proxy（較長窗）.
- Exits/swaps: champion close stack（隔離進場擇時）.

Sweep thresh ∈ {2.5%, 2.6%, …, 3.0%}.
Vignette: 2026-07-14 morning dip ~−3.8% from open high.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
from report_paths import RESEARCH_RRG
from research.backtest.archive.c18acc_avoid_mixed_slot_resim import load_gate_cache
from research.backtest.archive.c18acc_pool1_showcase import _ensure_avoid_mixed_gate
from research.backtest.c18acc_open_timing_study import (
    ADOPT_IS_MIN_DELTA_PP,
    ADOPT_OOS_MIN_DELTA_PP,
    ADOPT_OOS_MIN_N,
    DEFAULT_GATE_CACHE,
    _run_sim,
    _variant_row,
)
from research.backtest.c18acc_provisional_decaying_early_pnl_study import (
    _prior_ret_ok,
    _stub_fresh_calendar,
    build_provisional_fresh_score_cache,
)
from research.backtest.c18acc_provisional_fresh_stability_study import BENCH_ID
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_score_swap_c import champion_score_swap_c_config
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from rrg_mono_daily_brief import LENGTH
from rrg_rotation import compute_rrg_panel
from stock_db.etf import load_etf_constituent_watchlist
from stock_db.kbar import (
    load_kbar_day_5m_bars,
    load_kbar_day_closes,
    price_at_or_before_minute,
)

DEFAULT_ENTRY = "13:00"
# Align with champion / order ``no_trade_before`` — accumulate run_high from open,
# but only arm the low-trigger after this minute.
MIN_TRIGGER_MINUTE = "09:30"
# Historical intraday 「大盤」proxy：0050（Yahoo IX0001 5m ≈最近 60 交易日）
BENCH_PROXY_ID = "0050"
BENCH_PREF_ORDER = (BENCH_ID, BENCH_PROXY_ID)
# Dense grid focused on 2.5%–3%（先前掃到 3% soft-best）
THRESHOLDS = (0.025, 0.026, 0.027, 0.028, 0.029, 0.03)


def _minute_key(m: str) -> str:
    return m[:5] if len(m) >= 5 else m


def _bars_for_bench_day(
    conn: sqlite3.Connection,
    trade_date: str,
    *,
    prefer: tuple[str, ...] = BENCH_PREF_ORDER,
) -> tuple[str | None, tuple]:
    for sid in prefer:
        bars = load_kbar_day_5m_bars(conn, sid, trade_date)
        if bars:
            return sid, bars
    return None, ()


def resolve_low_trigger_next_fill(
    bars: tuple,
    *,
    thresh: float,
    default: str = DEFAULT_ENTRY,
) -> tuple[str, str, dict[str, Any]]:
    """Trigger on bar.low vs run_high; fill at next 5m bar.

    Returns (entry_minute, reason, meta).
    reason: dd_low_next | triggered_no_next | default
    """
    run_hi = -1e18
    for i, b in enumerate(bars):
        mk = _minute_key(str(b.minute))
        if mk > default:
            break
        hi = float(b.high or 0.0)
        lo = float(b.low or 0.0)
        if hi > 0:
            run_hi = max(run_hi, hi)
        if mk < MIN_TRIGGER_MINUTE:
            continue
        if run_hi <= 0 or lo <= 0:
            continue
        dd = lo / run_hi - 1.0
        if dd > -float(thresh):
            continue
        meta: dict[str, Any] = {
            "trigger_minute": mk,
            "dd_low": dd,
            "run_high": run_hi,
            "trigger_low": lo,
        }
        if i + 1 >= len(bars):
            return default, "triggered_no_next", meta
        next_mk = _minute_key(str(bars[i + 1].minute))
        return next_mk, "dd_low_next", {**meta, "fill_minute": next_mk}
    return default, "default", {}


def build_entry_plan_low_trigger(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    thresholds: tuple[float, ...] = THRESHOLDS,
) -> tuple[dict[str, dict[float, dict[str, Any]]], dict[str, str]]:
    """date → thresh → {entry_minute, reason, ...}; bench_id_by_date."""
    plan: dict[str, dict[float, dict[str, Any]]] = {}
    bench_used: dict[str, str] = {}
    for i, as_of in enumerate(trade_dates):
        if (i + 1) % 40 == 0 or i == 0:
            print(f"  entry-plan day {i + 1}/{len(trade_dates)} {as_of} …", flush=True)
        sid, bars = _bars_for_bench_day(conn, as_of)
        day_plan: dict[float, dict[str, Any]] = {}
        if not bars or sid is None:
            for t in thresholds:
                day_plan[t] = {
                    "entry_minute": DEFAULT_ENTRY,
                    "reason": "default",
                    "bench_id": None,
                }
            plan[as_of] = day_plan
            continue
        bench_used[as_of] = sid
        for t in thresholds:
            minute, reason, meta = resolve_low_trigger_next_fill(bars, thresh=t)
            # Only count as early if fill is strictly before default
            if reason == "dd_low_next" and minute >= DEFAULT_ENTRY:
                minute, reason = DEFAULT_ENTRY, "default"
                meta = {**meta, "collapsed_to_default": True}
            day_plan[t] = {
                "entry_minute": minute,
                "reason": reason,
                "bench_id": sid,
                **meta,
            }
        plan[as_of] = day_plan
    return plan, bench_used


def _needed_score_clocks(
    entry_plan: dict[str, dict[float, dict[str, Any]]],
    thresholds: tuple[float, ...],
) -> tuple[str, ...]:
    needed = {DEFAULT_ENTRY}
    for day_plan in entry_plan.values():
        for t in thresholds:
            row = day_plan.get(t) or {}
            m = str(row.get("entry_minute") or DEFAULT_ENTRY)
            needed.add(_minute_key(m))
    return tuple(sorted(needed))


@contextmanager
def _patch_dd_early_fill(
    *,
    score_cache: dict[str, dict[str, dict[str, float]]],
    entry_plan: dict[str, dict[float, dict[str, Any]]],
    thresh: float | None,
    gate: dict[str, set[str]] | None,
    name_map: dict[str, str],
    close,
    full_dates: list[str],
    prior_ret_days: int,
    prior_ret_max: float,
    stats: dict[str, int],
) -> Iterator[None]:
    """thresh=None → always fill @13:00 only (baseline)."""
    import research.backtest.rrg_mono_score_swap_c as ssc

    orig = ssc._fill_empty_slots

    def _fill(
        conn: sqlite3.Connection,
        *,
        as_of: str,
        fresh_mono: list,
        slots: list[dict[str, Any]],
        close,
        bench,
        full_dates: list[str],
        config,
        kbar_cache: dict,
        kbar_stats: dict,
        entry_c_config=None,
        n_slots: int | None = None,
        rs_ratio=None,
        rs_mom=None,
        accel_lookback: int = 4,
    ) -> None:
        del fresh_mono, bench, config, entry_c_config, rs_ratio, rs_mom, accel_lookback
        slot_cap = int(n_slots if n_slots is not None else 3)
        used = {int(p["slot"]) for p in slots}
        held = {str(p["stock_id"]) for p in slots}
        free = [i for i in range(slot_cap) if i not in used]
        if not free:
            return

        if thresh is None:
            minute, reason = DEFAULT_ENTRY, "baseline"
        else:
            row = ((entry_plan.get(as_of) or {}).get(float(thresh)) or {})
            minute = str(row.get("entry_minute") or DEFAULT_ENTRY)
            reason = str(row.get("reason") or "default")
        stats["n_fill_days"] = int(stats.get("n_fill_days") or 0) + 1
        if reason == "dd_low_next" and minute < DEFAULT_ENTRY:
            stats["n_early_days"] = int(stats.get("n_early_days") or 0) + 1
            stats[f"early_at_{minute}"] = int(stats.get(f"early_at_{minute}") or 0) + 1
            trig = ((entry_plan.get(as_of) or {}).get(float(thresh)) or {}).get(
                "trigger_minute"
            )
            if trig:
                stats[f"trig_at_{trig}"] = int(stats.get(f"trig_at_{trig}") or 0) + 1
        else:
            stats["n_default_days"] = int(stats.get("n_default_days") or 0) + 1

        scores = (score_cache.get(as_of) or {}).get(minute) or {}
        # fallback: if no provisional at fill minute, try 13:00 scores
        if not scores and minute != DEFAULT_ENTRY:
            scores = (score_cache.get(as_of) or {}).get(DEFAULT_ENTRY) or {}
            minute = DEFAULT_ENTRY
            reason = "fallback_1300"
            stats["n_fallback_1300"] = int(stats.get("n_fallback_1300") or 0) + 1

        gate_day = gate.get(as_of) if gate is not None else None
        ranked = sorted(scores.items(), key=lambda kv: (-float(kv[1]), kv[0]))
        for sid, seg in ranked:
            if not free:
                break
            if sid in held:
                continue
            if gate_day is not None and sid not in gate_day:
                continue
            if not _prior_ret_ok(
                close,
                full_dates,
                as_of,
                sid,
                days=prior_ret_days,
                max_ret=prior_ret_max,
            ):
                continue
            key = (sid, as_of)
            if key not in kbar_cache:
                kbar_cache[key] = load_kbar_day_closes(conn, sid, as_of)
                kbar_stats["loads"] = int(kbar_stats.get("loads") or 0) + 1
            px = price_at_or_before_minute(kbar_cache[key], minute)
            if px is None or float(px) <= 0:
                continue
            slot = free.pop(0)
            slots.append(
                {
                    "slot": slot,
                    "stock_id": sid,
                    "stock_name": name_map.get(sid, ""),
                    "signal_date": as_of,
                    "entry_date": as_of,
                    "entry_px": float(px),
                    "seg_last": round(float(seg), 4),
                    "disp": 1.5,
                    "rs_momentum": 0.0,
                    "seg_step_delta": 0.0,
                    "entry_minute": minute,
                    "entry_leg": f"bench_dd_{reason}",
                }
            )
            held.add(sid)

    ssc._fill_empty_slots = _fill  # type: ignore[assignment]
    try:
        yield
    finally:
        ssc._fill_empty_slots = orig  # type: ignore[assignment]


def _adopt(*, n_oos: int, d_oos: float | None, d_is: float | None) -> str:
    ok = (
        n_oos >= ADOPT_OOS_MIN_N
        and d_oos is not None
        and float(d_oos) >= ADOPT_OOS_MIN_DELTA_PP
        and d_is is not None
        and float(d_is) >= ADOPT_IS_MIN_DELTA_PP
    )
    return "GO" if ok else "NO_GO"


def _vignette_20260714(conn: sqlite3.Connection) -> dict[str, Any]:
    sid, bars = _bars_for_bench_day(conn, "2026-07-14")
    if not bars:
        return {"date": "2026-07-14", "ok": False}
    run_hi = -1e18
    worst: dict[str, Any] | None = None
    for b in bars:
        run_hi = max(run_hi, float(b.high))
        dd_l = float(b.low) / run_hi - 1.0
        dd_c = float(b.close) / run_hi - 1.0
        if worst is None or dd_l < float(worst["dd_low_raw"]):
            worst = {
                "minute": _minute_key(str(b.minute)),
                "run_high": round(run_hi, 2),
                "low": round(float(b.low), 2),
                "close": round(float(b.close), 2),
                "dd_low_raw": dd_l,
                "dd_close_raw": dd_c,
                "dd_low": round(dd_l * 100, 2),
                "dd_close": round(dd_c * 100, 2),
            }
    triggers: dict[str, Any] = {}
    for t in THRESHOLDS:
        m, reason, meta = resolve_low_trigger_next_fill(bars, thresh=t)
        if reason == "dd_low_next" and m >= DEFAULT_ENTRY:
            m, reason = DEFAULT_ENTRY, "default"
        triggers[f"{t * 100:.1f}%"] = {
            "entry_minute": m,
            "reason": reason,
            "trigger_minute": meta.get("trigger_minute"),
            "dd_low_pct": (
                None
                if meta.get("dd_low") is None
                else round(float(meta["dd_low"]) * 100, 2)
            ),
        }
    return {
        "date": "2026-07-14",
        "ok": True,
        "bench_id": sid,
        "n_bars": len(bars),
        "worst": {k: v for k, v in (worst or {}).items() if not str(k).endswith("_raw")},
        "trigger_if_thresh": triggers,
        "note": "low/run_high trigger → next 5m fill; prefer IX0001 else 0050",
    }


def _bench_coverage_end(conn: sqlite3.Connection) -> str | None:
    """Latest trade_date with IX0001 or 0050 5m."""
    row = conn.execute(
        """
        SELECT MAX(trade_date) FROM stock_kbar_5m
        WHERE stock_id IN (?, ?)
        """,
        (BENCH_ID, BENCH_PROXY_ID),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def run_bench_dd_early_pnl_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str = "2025-06-30",
    n_slots: int = 3,
    gate_cache_path: str | Path | None = DEFAULT_GATE_CACHE,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]

    gate_path = Path(gate_cache_path) if gate_cache_path else None
    if gate_path and gate_path.is_file() and date_end is None:
        _, _, c0, c1 = load_gate_cache(str(gate_path))
        if trade_dates and c0 == trade_dates[0] and c1 < trade_dates[-1]:
            end = c1
            trade_dates = [d for d in full_dates if date_start <= d <= end]
            print(f"truncated trade window to gate cache end {end}", flush=True)

    # Prefer IX0001 ∪ 0050 coverage（Yahoo 指數 5m 已補最近 ~60d）
    cov_end = _bench_coverage_end(conn)
    if cov_end and end > cov_end:
        end = cov_end
        trade_dates = [d for d in full_dates if date_start <= d <= end]
        print(f"trim window to bench 5m coverage end {end}", flush=True)

    watch = load_etf_constituent_watchlist(conn)
    name_map = {str(w["stock_id"]): str(w.get("stock_name") or "") for w in watch}
    universe = [str(w["stock_id"]) for w in watch]

    vignette = _vignette_20260714(conn)

    print("building low-trigger next-fill entry plan …", flush=True)
    entry_plan, bench_used = build_entry_plan_low_trigger(
        conn, trade_dates, THRESHOLDS
    )
    n_proxy = sum(1 for v in bench_used.values() if v == BENCH_PROXY_ID)
    n_ix = sum(1 for v in bench_used.values() if v == BENCH_ID)
    print(
        f"  bench days used: IX0001={n_ix} · 0050={n_proxy} · "
        f"none={len(trade_dates) - len(bench_used)}",
        flush=True,
    )

    clocks = _needed_score_clocks(entry_plan, THRESHOLDS)
    print(
        f"building provisional fresh score cache · clocks={len(clocks)} …",
        flush=True,
    )
    score_cache = build_provisional_fresh_score_cache(
        conn,
        trade_dates=trade_dates,
        clocks=clocks,
        close=close,
        bench=bench,
        universe=universe,
    )

    fire_stats: dict[str, Any] = {}
    for t in THRESHOLDS:
        n_fire = 0
        first_clocks: dict[str, int] = {}
        trig_clocks: dict[str, int] = {}
        for d in trade_dates:
            row = (entry_plan.get(d) or {}).get(t) or {}
            m = str(row.get("entry_minute") or DEFAULT_ENTRY)
            reason = str(row.get("reason") or "default")
            if reason == "dd_low_next" and m < DEFAULT_ENTRY:
                n_fire += 1
                first_clocks[m] = first_clocks.get(m, 0) + 1
                tm = str(row.get("trigger_minute") or "?")
                trig_clocks[tm] = trig_clocks.get(tm, 0) + 1
        fire_stats[f"{t * 100:.1f}%"] = {
            "n_early_days": n_fire,
            "pct_days": round(100.0 * n_fire / max(1, len(trade_dates)), 2),
            "fill_clock_hist": first_clocks,
            "trigger_clock_hist": trig_clocks,
        }

    fresh_by_date = _stub_fresh_calendar(score_cache, trade_dates, name_map)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=trade_dates[0], date_end=trade_dates[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    champ = replace(
        champion_score_swap_c_config(),
        candidate_pool="fresh",
        n_slots=max(1, int(n_slots)),
        timing_mode="close",
        entry_leg="A",
        no_trade_before="09:30",
    )
    ctx: dict[str, Any] = {
        "close": close,
        "bench": bench,
        "rs_ratio": rs_ratio,
        "rs_mom": rs_mom,
        "full_dates": full_dates,
        "fresh_by_date": fresh_by_date,
        "mono_by_date": mono_by_date,
        "zone_by_date": zone_by_date,
        "kbar_cache": {},
        "spread_rs_panel": None,
    }
    oos_start = next((d for d in trade_dates if d > is_end), None)
    if not oos_start:
        raise ValueError(f"no OOS after {is_end}")

    gate: dict[str, set[str]] | None = None
    gate_meta: dict[str, Any] = {}
    if gate_path and gate_path.is_file():
        try:
            gate, gate_meta = _ensure_avoid_mixed_gate(
                conn,
                trade_dates,
                gate_cache_path=gate_path,
                live_aligned=True,
                n_slots=n_slots,
            )
        except ValueError:
            raw, meta, c0, c1 = load_gate_cache(str(gate_path))
            gate = {d: set(raw.get(d) or set()) for d in trade_dates}
            gate_meta = {**(meta or {}), "subset_from": f"{c0}..{c1}"}
    else:
        gate, gate_meta = _ensure_avoid_mixed_gate(
            conn, trade_dates, gate_cache_path=None, live_aligned=True, n_slots=n_slots
        )
    if gate is None:
        raise ValueError("avoid_mixed gate required")

    prior_days = int(getattr(champ, "entry_prior_ret_days", 5) or 5)
    prior_max = float(getattr(champ, "entry_prior_ret_max", 0.12) or 0.12)

    variant_specs: list[tuple[str, float | None, str]] = [
        ("B0_1300", None, "基線 · 固定 13:00 暫定 fresh"),
    ]
    for t in THRESHOLDS:
        pct = f"{t * 100:.1f}".replace(".", "p")
        variant_specs.append(
            (
                f"DD_{pct}",
                t,
                f"low≤−{t * 100:.1f}% vs run_high → 下一根5m成交 · 否則 13:00",
            )
        )

    variants: list[dict[str, Any]] = []
    base_full = base_is = base_oos = None
    for vid, thresh, label in variant_specs:
        fill_stats: dict[str, int] = {}
        print(f"  sim {vid} · thresh={thresh} …", flush=True)
        cfg = replace(champ, variant_id=vid, label=label)
        with _patch_dd_early_fill(
            score_cache=score_cache,
            entry_plan=entry_plan,
            thresh=thresh,
            gate=gate,
            name_map=name_map,
            close=close,
            full_dates=full_dates,
            prior_ret_days=prior_days,
            prior_ret_max=prior_max,
            stats=fill_stats,
        ):
            periods, summary, _ = _run_sim(
                conn,
                ctx={**ctx, "kbar_cache": {}},
                trade_dates=trade_dates,
                cfg=cfg,
                confirm_bars=1,
                gate=gate,
                label=vid,
            )
        minute_counts: dict[str, int] = {}
        for p in periods:
            m = str(p.get("entry_minute") or "?")
            minute_counts[m] = minute_counts.get(m, 0) + 1
        row = _variant_row(
            variant_id=vid,
            label=label,
            periods=periods,
            summary=summary,
            date_start=date_start,
            is_end=is_end,
            oos_start=oos_start,
            end=end,
            base_full=base_full,
            base_is=base_is,
            base_oos=base_oos,
            extra={
                "thresh": thresh,
                "entry_minute_hist": minute_counts,
                "fill_stats": fill_stats,
                "pit_clean": True,
            },
        )
        if vid == "B0_1300":
            base_full = row["slices"]["full"]
            base_is = row["slices"]["is"]
            base_oos = row["slices"]["oos"]
            row["delta_full_excess_pp"] = None
            row["delta_is_excess_pp"] = None
            row["delta_oos_excess_pp"] = None
            row["adopt"] = "BASE"
        variants.append(row)

    best = None
    notes: list[str] = []
    for row in variants:
        if row.get("variant_id") == "B0_1300":
            continue
        d_oos = row.get("delta_oos_excess_pp")
        d_is = row.get("delta_is_excess_pp")
        n_oos = int(((row.get("slices") or {}).get("oos") or {}).get("n_legs") or 0)
        decision = _adopt(n_oos=n_oos, d_oos=d_oos, d_is=d_is)
        row["adopt"] = decision
        notes.append(
            f"{row.get('variant_id')} OOS Δ={d_oos}pp · n={n_oos} → {decision}"
        )
        if decision == "GO" and (
            best is None
            or float(d_oos or -1e9) > float(best.get("delta_oos_excess_pp") or -1e9)
        ):
            best = row

    soft = None
    for row in variants:
        if row.get("variant_id") == "B0_1300":
            continue
        d = row.get("delta_oos_excess_pp")
        if d is None:
            continue
        if soft is None or float(d) > float(soft.get("delta_oos_excess_pp") or -1e9):
            soft = row

    verdict = "KEEP_B0_1300"
    if best is not None:
        verdict = f"GO_ADOPT_{best.get('variant_id')}"

    return {
        "study_id": "c18acc_bench_dd_early_pnl",
        "schema": "c18acc_bench_dd_early_pnl-v2-low-next5m",
        "pit": {
            "bench": f"prefer {BENCH_ID} else {BENCH_PROXY_ID}",
            "trigger": "5m low ≤ −thresh vs running RTH high",
            "min_trigger_minute": MIN_TRIGGER_MINUTE,
            "fill": "next 5m bar close",
            "default_entry": DEFAULT_ENTRY,
            "pool": "same-day provisional mono fresh at fill minute",
            "exits": "champion close stack",
        },
        "bench_coverage": {
            "IX0001_days": n_ix,
            "0050_days": n_proxy,
            "missing_days": len(trade_dates) - len(bench_used),
            "IX0001_note": "Yahoo ^TWII 5m backfill ≈60d (2026-05-18..)",
            "score_clocks": list(clocks),
        },
        "window": {
            "date_start": date_start,
            "date_end": end,
            "is_end": is_end,
            "oos_start": oos_start,
            "n_stocks": len(universe),
            "n_days": len(trade_dates),
        },
        "vignette_20260714": vignette,
        "fire_stats": fire_stats,
        "gate_meta": gate_meta,
        "variants": variants,
        "verdict": {
            "summary": verdict,
            "best_go": best.get("variant_id") if best else None,
            "best_oos_even_if_nogo": soft.get("variant_id") if soft else None,
            "best_oos_delta_pp": soft.get("delta_oos_excess_pp") if soft else None,
            "notes": notes,
        },
    }


def render_bench_dd_early_pnl_md(payload: dict[str, Any]) -> str:
    w = payload.get("window") or {}
    v = payload.get("verdict") or {}
    vig = payload.get("vignette_20260714") or {}
    cov = payload.get("bench_coverage") or {}
    pit = payload.get("pit") or {}
    lines = [
        "# C18acc · 大盤自高點回撤 → 提早進場（低點觸發 · 下一根 5m）",
        "",
        f"窗口：**{w.get('date_start')}** .. **{w.get('date_end')}** · "
        f"IS ≤ **{w.get('is_end')}** · OOS ≥ **{w.get('oos_start')}** · "
        f"days={w.get('n_days')} · watchlist_n=**{w.get('n_stocks')}**",
        "",
        f"Schema：`{payload.get('schema')}`",
        "",
        "## 規則（PIT）",
        "",
        f"- 預設進場：**{DEFAULT_ENTRY}** 暫定 mono fresh（對齊現行 near-close）。",
        f"- 觸發：大盤（優先 `{BENCH_ID}`，否則 `{BENCH_PROXY_ID}`）5m "
        f"**low / running high − 1 ≤ −thresh**（run_high 自開盤累積；"
        f"觸發 ≥ `{MIN_TRIGGER_MINUTE}`）。",
        f"- 成交：觸發 bar **下一根** 5m close（`{pit.get('fill')}`）。",
        "- 出場／換倉：champion 收盤棧（只比較進場擇時）。",
        f"- Bench 覆蓋：IX0001={cov.get('IX0001_days')} · 0050={cov.get('0050_days')} · "
        f"missing={cov.get('missing_days')} · {cov.get('IX0001_note')}",
        "",
        "## 個案：2026-07-14",
        "",
    ]
    if vig.get("ok"):
        worst = vig.get("worst") or {}
        lines.append(
            f"- bench=`{vig.get('bench_id')}` · 最深回撤：`{worst.get('minute')}` · "
            f"run_high={worst.get('run_high')} → low={worst.get('low')}"
            f"（**{worst.get('dd_low')}%**）· close dd **{worst.get('dd_close')}%**"
        )
        lines.append("- 若用各門檻（low 觸發 → 下一根成交）：")
        for k, info in (vig.get("trigger_if_thresh") or {}).items():
            lines.append(
                f"  - ≥{k} → fill `{info.get('entry_minute')}` "
                f"（trig `{info.get('trigger_minute')}` · {info.get('reason')} · "
                f"dd_low={info.get('dd_low_pct')}%）"
            )
    else:
        lines.append("- （無 2026-07-14 bench kbar）")

    lines.extend(
        [
            "",
            "## 門檻觸發頻率（全日）",
            "",
            "| thresh | 提早日數 | 佔交易日% | 成交鐘分布 | 觸發鐘分布 |",
            "|-------:|---------:|----------:|------------|------------|",
        ]
    )
    for k, st in (payload.get("fire_stats") or {}).items():
        lines.append(
            f"| {k} | {st.get('n_early_days')} | {st.get('pct_days')} | "
            f"`{st.get('fill_clock_hist')}` | `{st.get('trigger_clock_hist')}` |"
        )

    lines.extend(
        [
            "",
            f"## 結論：`{v.get('summary')}`",
            "",
            f"- 過門檻最佳：`{v.get('best_go')}`",
            f"- OOS Δ 最高（含 NO_GO）：`{v.get('best_oos_even_if_nogo')}` · "
            f"Δ=**{v.get('best_oos_delta_pp')}pp**",
            "",
            "> 敏感度：若拿掉觸發 ≥09:30 地板，開盤低點噪聲曾出現假性 GO（DD_3p0 "
            "OOS ~+0.57pp）——**不採納**；報告以對齊 live `no_trade_before` 為準。",
            "",
        ]
    )
    for note in v.get("notes") or []:
        lines.append(f"- {note}")

    lines.extend(
        [
            "",
            "## 結果（Δ vs B0_1300）",
            "",
            "| variant | window | n | mean_excess% | Δ vs B0 | adopt |",
            "|---------|--------|--:|-------------:|--------:|:-----:|",
        ]
    )
    for row in payload.get("variants") or []:
        for sl_name in ("full", "is", "oos"):
            sl = (row.get("slices") or {}).get(sl_name) or {}
            delta = {
                "full": row.get("delta_full_excess_pp"),
                "is": row.get("delta_is_excess_pp"),
                "oos": row.get("delta_oos_excess_pp"),
            }[sl_name]
            delta_s = "—" if delta is None else f"{float(delta):+.4f}pp"
            lines.append(
                f"| {row.get('variant_id')} | {sl_name.upper()} | {sl.get('n_legs')} | "
                f"{sl.get('mean_excess_pct')} | {delta_s} | "
                f"{row.get('adopt') if sl_name == 'oos' else ''} |"
            )
    lines.extend(
        [
            "",
            f"門檻：OOS n≥{ADOPT_OOS_MIN_N} · Δ≥+{ADOPT_OOS_MIN_DELTA_PP}pp · "
            f"IS Δ≥{ADOPT_IS_MIN_DELTA_PP}pp",
            "",
            "## 進場分鐘分布",
            "",
        ]
    )
    for row in payload.get("variants") or []:
        hist = row.get("entry_minute_hist") or {}
        fs = row.get("fill_stats") or {}
        lines.append(
            f"- **{row.get('variant_id')}**：legs=`{hist}` · fill_days early="
            f"{fs.get('n_early_days', 0)}/{fs.get('n_fill_days', 0)}"
        )
    lines.extend(
        [
            "",
            "模組：`src/research/backtest/c18acc_bench_dd_early_pnl_study.py`",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse

    from stock_db import DEFAULT_DB_PATH, connect

    ap = argparse.ArgumentParser(
        description="Bench DD low-trigger next-5m early-entry PnL sweep (2.5–3%)"
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-01-02")
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--is-end", default="2025-06-30")
    ap.add_argument("--n-slots", type=int, default=3)
    ap.add_argument("--gate-cache", type=Path, default=Path(DEFAULT_GATE_CACHE))
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_bench_dd_early_pnl_study(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            n_slots=args.n_slots,
            gate_cache_path=args.gate_cache if args.gate_cache.is_file() else None,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = RESEARCH_RRG / f"{stamp}_c18acc_bench_dd_early_pnl.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_bench_dd_early_pnl_md(payload), encoding="utf-8")
    print((payload.get("verdict") or {}).get("summary", ""))
    for note in (payload.get("verdict") or {}).get("notes") or []:
        print(note)
    print(f"Wrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
