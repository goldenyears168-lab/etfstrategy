"""C18acc · provisional fresh ALLDAY recovery (Phase 1 · PIT-legal).

B0_live: provisional fill only @13:00 · confirm=1 · G_R5_12 · avoid_mixed@13:00
T1_allday_c1: provisional fill ≥09:30 clocks · confirm=1 · G_R5_12 · gate@09:30
T2_allday_c2: same · confirm=2 consecutive clocks

Pool = same-day provisional mono fresh (rebuilt each clock). Not EOD calendar.
Swaps/exits = champion poll_5m + S2 + spread_lost bypass.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
from research.backtest.archive.c18acc_avoid_mixed_slot_resim import (
    build_avoid_mixed_gate_lookup_live,
    load_gate_cache,
    save_gate_cache,
)
from research.backtest.archive.c18acc_hold3d_event_study import (
    _prepare_c18acc_context,
    c18acc_live_s2_config,
)
from research.backtest.archive.c18acc_pool1_showcase import _live_gate_cache_path
from research.backtest.c18acc_old_vs_live_gap_attribution import (
    _equal_slot_compound_pct,
    _is_cut_date,
)
from research.backtest.c18acc_open_timing_study import (
    ADOPT_IS_MIN_DELTA_PP,
    ADOPT_OOS_MIN_DELTA_PP,
    ADOPT_OOS_MIN_N,
    _delta_pp,
    _entry_minute_hist,
    _period_stats,
    _slice_periods,
)
from research.backtest.c18acc_provisional_decaying_early_pnl_study import (
    build_provisional_fresh_score_cache,
)
from research.backtest.c18acc_provisional_fresh_stability_study import DEFAULT_CLOCKS
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_intraday_ab import champion_entry_c_config
from research.backtest.rrg_mono_score_swap_c import (
    _poll5m_needs_spread_rs_panel,
    build_daily_spread_rs_panel,
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from research.backtest.c18acc_rotation_selective_exit_sweep import (
    rotation_selective_exit_sweep_configs,
)
from rrg_mono_daily_brief import LENGTH, ScanRow
from rrg_rotation import compute_rrg_panel
from stock_db.etf import load_etf_constituent_watchlist
from stock_db.kbar import load_kbar_day_closes, price_at_or_before_minute

TOPIC_ID = "c18acc-provisional-allday-recovery"
SCHEMA = "c18acc_provisional_allday_recovery-v1"
ALLDAY_CLOCKS = DEFAULT_CLOCKS  # 09:30 … 13:20
B0_CLOCKS = ("13:00",)


def _prior_ret_ok(
    close,
    full_dates: list[str],
    as_of: str,
    sid: str,
    *,
    days: int,
    max_ret: float,
) -> bool:
    from research.backtest.rrg_mono_score_swap_c import prior_ret_gate_allows

    return prior_ret_gate_allows(
        close,
        sid,
        full_dates,
        as_of=as_of,
        n_days=days,
        max_ret=max_ret,
    )


def _stub_fresh_calendar(
    cache: dict[str, dict[str, dict[str, float]]],
    trade_dates: list[str],
    name_map: dict[str, str],
) -> dict[str, list[ScanRow]]:
    """Minimal fresh calendar so sim has non-empty days (fill is patched)."""
    out: dict[str, list[ScanRow]] = {}
    for d in trade_dates:
        scores = (cache.get(d) or {}).get("13:00") or (cache.get(d) or {}).get("13:20") or {}
        rows: list[ScanRow] = []
        for sid, seg in list(scores.items())[:5]:
            rows.append(
                ScanRow(
                    stock_id=sid,
                    stock_name=name_map.get(sid, ""),
                    fresh=True,
                    mono=True,
                    seg_last=float(seg),
                    disp=1.5,
                    segs=[],
                    quadrants=[],
                    rs_ratio=100.0,
                    rs_momentum=100.0,
                    daily_pct=None,
                )
            )
        out[d] = rows
    return out


@contextmanager
def _patch_provisional_fill(
    *,
    cache: dict[str, dict[str, dict[str, float]]],
    clocks: tuple[str, ...],
    confirm_bars: int,
    gate: dict[str, set[str]] | None,
    name_map: dict[str, str],
    close,
    full_dates: list[str],
    prior_ret_days: int,
    prior_ret_max: float | None,
) -> Iterator[None]:
    import research.backtest.rrg_mono_score_swap_c as ssc

    orig = ssc._fill_empty_slots

    def _fill(
        conn: sqlite3.Connection,
        *,
        as_of: str,
        fresh_mono: list,
        slots: list[dict[str, Any]],
        close,  # noqa: ANN001
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
        day_scores = cache.get(as_of) or {}
        gate_day = gate.get(as_of) if gate is not None else None
        need = max(1, int(confirm_bars))
        streak: dict[str, int] = {}
        for minute in clocks:
            if not free:
                break
            scores = day_scores.get(minute) or {}
            present = set(scores)
            # confirm streak: consecutive clocks in provisional fresh
            for sid in list(streak.keys()):
                if sid not in present:
                    streak[sid] = 0
            for sid in present:
                streak[sid] = int(streak.get(sid) or 0) + 1
            ready = [sid for sid, k in streak.items() if k >= need and sid not in held]
            ranked = sorted(
                ((sid, float(scores.get(sid, 0.0))) for sid in ready if sid in present),
                key=lambda kv: (-kv[1], kv[0]),
            )
            for sid, seg in ranked:
                if not free:
                    break
                if gate_day is not None and sid not in gate_day:
                    continue
                if prior_ret_max is not None and not _prior_ret_ok(
                    close,
                    full_dates,
                    as_of,
                    sid,
                    days=prior_ret_days,
                    max_ret=float(prior_ret_max),
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
                        "seg_last": round(seg, 4),
                        "disp": 1.5,
                        "rs_momentum": 0.0,
                        "seg_step_delta": 0.0,
                        "entry_minute": minute,
                        "entry_leg": "prov_allday",
                    }
                )
                held.add(sid)
                streak[sid] = 0

    ssc._fill_empty_slots = _fill  # type: ignore[assignment]
    try:
        yield
    finally:
        ssc._fill_empty_slots = orig  # type: ignore[assignment]


def _resolve_gate(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    ntb: str,
    n_slots: int,
) -> tuple[dict[str, set[str]], dict[str, Any], Path]:
    path = _live_gate_cache_path(trade_dates, gate_anchor_minute=ntb)
    if path.is_file():
        gate, meta, c0, c1 = load_gate_cache(str(path))
        if c0 == trade_dates[0] and c1 == trade_dates[-1]:
            cached = str((meta or {}).get("gate_anchor_minute") or "")
            if not cached or cached == ntb:
                return gate, meta or {}, path
    print(
        f"building avoid_mixed gate @{ntb} · {trade_dates[0]}→{trade_dates[-1]} …",
        flush=True,
    )
    ctx = _prepare_c18acc_context(conn, trade_dates)
    cfg = replace(c18acc_live_s2_config(n_slots=n_slots), no_trade_before=ntb)
    gate, meta = build_avoid_mixed_gate_lookup_live(
        conn,
        trade_dates=trade_dates,
        ctx=ctx,
        cfg=cfg,
        no_trade_before=ntb,
        gate_anchor_minute=ntb,
        passthrough_pool=True,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    save_gate_cache(
        str(path),
        gate=gate,
        meta=meta,
        date_start=trade_dates[0],
        date_end=trade_dates[-1],
    )
    return gate, meta, path


def _s2_poll_cfg(*, n_slots: int, ntb: str, variant_id: str, label: str):
    s2 = {c.variant_id: c for c in rotation_selective_exit_sweep_configs()}["S2"]
    champ = champion_score_swap_c_config()
    return replace(
        s2,
        timing_mode="poll_5m",
        no_trade_before=ntb,
        candidate_pool="fresh",
        variant_id=variant_id,
        label=label,
        n_slots=max(1, int(n_slots)),
        sort_key=champ.sort_key,
        buy_sort_key=champ.buy_sort_key,
        score_margin=champ.score_margin,
        accel_sell_negative_only=champ.accel_sell_negative_only,
        accel_lookback=champ.accel_lookback,
        candidate_lookback=champ.candidate_lookback,
        min_hold_days=champ.min_hold_days,
        max_hold_days=champ.max_hold_days,
        accel_sell_bypass_on_spread_lost=champ.accel_sell_bypass_on_spread_lost,
        entry_prior_ret_days=champ.entry_prior_ret_days,
        entry_prior_ret_max=champ.entry_prior_ret_max,
    )


def _adopt(*, n_oos: int, d_oos: float | None, d_is: float | None) -> str:
    ok = (
        n_oos >= ADOPT_OOS_MIN_N
        and d_oos is not None
        and float(d_oos) >= ADOPT_OOS_MIN_DELTA_PP
        and d_is is not None
        and float(d_is) >= ADOPT_IS_MIN_DELTA_PP
    )
    return "GO" if ok else "NO_GO"


def run_provisional_allday_recovery(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str | None = None,
    n_slots: int = 3,
    clocks: tuple[str, ...] = ALLDAY_CLOCKS,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    cut = is_end or _is_cut_date(trade_dates, ratio=0.7)
    oos_start = next((d for d in trade_dates if d > cut), None)
    if not oos_start:
        raise ValueError(f"no OOS after {cut}")

    watch = load_etf_constituent_watchlist(conn)
    name_map = {str(w["stock_id"]): str(w.get("stock_name") or "") for w in watch}
    universe = [str(w["stock_id"]) for w in watch]

    print("building provisional fresh score cache …", flush=True)
    cache = build_provisional_fresh_score_cache(
        conn,
        trade_dates=trade_dates,
        clocks=clocks,
        close=close,
        bench=bench,
        universe=universe,
    )
    fresh_by_date = _stub_fresh_calendar(cache, trade_dates, name_map)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=trade_dates[0], date_end=trade_dates[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    champ = champion_score_swap_c_config()
    spread = (
        build_daily_spread_rs_panel(close, bench)
        if _poll5m_needs_spread_rs_panel(champ)
        else None
    )
    ctx = {
        "close": close,
        "bench": bench,
        "rs_ratio": rs_ratio,
        "rs_mom": rs_mom,
        "full_dates": full_dates,
        "fresh_by_date": fresh_by_date,
        "mono_by_date": mono_by_date,
        "zone_by_date": zone_by_date,
        "spread_rs_panel": spread,
    }

    gate_1300, meta_1300, path_1300 = _resolve_gate(
        conn, trade_dates, ntb="13:00", n_slots=n_slots
    )
    gate_0930, meta_0930, path_0930 = _resolve_gate(
        conn, trade_dates, ntb="09:30", n_slots=n_slots
    )

    variants_spec = (
        ("B0_live_E1300", "現行 · provisional @13:00 · c1", B0_CLOCKS, 1, "13:00", gate_1300),
        ("T1_allday_c1", "provisional ALLDAY · c1", clocks, 1, "09:30", gate_0930),
        ("T2_allday_c2", "provisional ALLDAY · c2", clocks, 2, "09:30", gate_0930),
    )

    prior_days = int(champ.entry_prior_ret_days or 5)
    prior_max = champ.entry_prior_ret_max
    rows: list[dict[str, Any]] = []
    base_full = base_is = base_oos = None

    for vid, label, use_clocks, confirm, ntb, gate in variants_spec:
        cfg = _s2_poll_cfg(n_slots=n_slots, ntb=ntb, variant_id=vid, label=label)
        entry_c = replace(
            champion_entry_c_config(confirm_bars=confirm),
            no_swap_before=ntb,
        )
        print(
            f"  sim {vid} · clocks={','.join(use_clocks)} · confirm={confirm} · ntb={ntb}",
            flush=True,
        )
        with _patch_provisional_fill(
            cache=cache,
            clocks=use_clocks,
            confirm_bars=confirm,
            gate=gate,
            name_map=name_map,
            close=close,
            full_dates=full_dates,
            prior_ret_days=prior_days,
            prior_ret_max=float(prior_max) if prior_max is not None else None,
        ):
            periods, summary = simulate_score_swap_c(
                conn,
                trade_dates=trade_dates,
                full_dates=full_dates,
                close=close,
                bench=bench,
                fresh_by_date=fresh_by_date,
                zone_by_date=zone_by_date,
                config=cfg,
                mono_by_date=mono_by_date,
                kbar_cache={},
                rs_mom=rs_mom,
                rs_ratio=rs_ratio,
                spread_rs_panel=spread,
                entry_c_config=entry_c,
                entry_gate_by_date=gate,
                swap_gate_by_date=gate,
            )
        full = _period_stats(periods, summary)
        is_s = _period_stats(_slice_periods(periods, start=date_start, end=cut))
        oos = _period_stats(_slice_periods(periods, start=oos_start, end=end))
        full["compound_slot_pct"] = _equal_slot_compound_pct(periods)
        is_s["compound_slot_pct"] = _equal_slot_compound_pct(
            _slice_periods(periods, start=date_start, end=cut)
        )
        oos["compound_slot_pct"] = _equal_slot_compound_pct(
            _slice_periods(periods, start=oos_start, end=end)
        )
        row = {
            "variant_id": vid,
            "label": label,
            "confirm_bars": confirm,
            "no_trade_before": ntb,
            "clocks": list(use_clocks),
            "slices": {"full": full, "is": is_s, "oos": oos},
            "entry_minute_hist": _entry_minute_hist(periods),
            "delta_full_excess_pp": _delta_pp(full.get("mean_excess_pct"), (base_full or {}).get("mean_excess_pct"))
            if base_full
            else None,
            "delta_is_excess_pp": _delta_pp(is_s.get("mean_excess_pct"), (base_is or {}).get("mean_excess_pct"))
            if base_is
            else None,
            "delta_oos_excess_pp": _delta_pp(oos.get("mean_excess_pct"), (base_oos or {}).get("mean_excess_pct"))
            if base_oos
            else None,
            "delta_full_compound_pp": _delta_pp(
                full.get("compound_slot_pct"), (base_full or {}).get("compound_slot_pct")
            )
            if base_full
            else None,
        }
        if vid == "B0_live_E1300":
            base_full, base_is, base_oos = full, is_s, oos
        else:
            row["adopt"] = _adopt(
                n_oos=int(oos.get("n_legs") or 0),
                d_oos=row["delta_oos_excess_pp"],
                d_is=row["delta_is_excess_pp"],
            )
        rows.append(row)
        print(
            f"    n={full.get('n_legs')} excess={full.get('mean_excess_pct')} "
            f"oosΔ={row.get('delta_oos_excess_pp')} adopt={row.get('adopt')}",
            flush=True,
        )

    by_id = {r["variant_id"]: r for r in rows}
    t1 = by_id.get("T1_allday_c1") or {}
    t2 = by_id.get("T2_allday_c2") or {}
    go_any = (t1.get("adopt") == "GO") or (t2.get("adopt") == "GO")
    winner = None
    if t1.get("adopt") == "GO" and t2.get("adopt") == "GO":
        # prefer higher OOS excess
        e1 = float(t1.get("delta_oos_excess_pp") or -999)
        e2 = float(t2.get("delta_oos_excess_pp") or -999)
        winner = "T1_allday_c1" if e1 >= e2 else "T2_allday_c2"
    elif t1.get("adopt") == "GO":
        winner = "T1_allday_c1"
    elif t2.get("adopt") == "GO":
        winner = "T2_allday_c2"

    verdict = {
        "decision": "GO_PROVISIONAL_ALLDAY" if go_any else "KEEP_LIVE_E1300",
        "winner_variant": winner,
        "order_action": (
            f"Draft no_trade_before=09:30 · confirm_bars="
            f"{by_id[winner]['confirm_bars']} · provisional pool"
            if winner
            else "No order.yaml / strategy.yaml change · keep E@13:00"
        ),
        "observe_sleeve": None
        if go_any
        else {
            "topic": "c18acc-early-entry-event-observe",
            "action": (
                "Event-observe only · tag early (≈09:30) candidates that live "
                "E@13:00 misses · no order submit · no 4th slot"
            ),
        },
        "gates": {
            "oos_min_n": ADOPT_OOS_MIN_N,
            "oos_min_delta_pp": ADOPT_OOS_MIN_DELTA_PP,
            "is_min_delta_pp": ADOPT_IS_MIN_DELTA_PP,
        },
        "summary": (
            f"{'GO' if go_any else 'NO_GO'} · winner={winner or '—'} · "
            f"T1 oosΔ={t1.get('delta_oos_excess_pp')} · T2 oosΔ={t2.get('delta_oos_excess_pp')}"
        ),
    }

    return {
        "schema": SCHEMA,
        "topic": TOPIC_ID,
        "date_start": date_start,
        "date_end": end,
        "is_end": cut,
        "oos_start": oos_start,
        "n_trade_dates": len(trade_dates),
        "n_slots": n_slots,
        "clocks": list(clocks),
        "gate_meta": {
            "13:00": {**(meta_1300 or {}), "path": str(path_1300)},
            "09:30": {**(meta_0930 or {}), "path": str(path_0930)},
        },
        "variants": rows,
        "verdict": verdict,
        "ctx_note": "provisional mono fresh per clock · not EOD fresh calendar",
    }


def render_provisional_allday_recovery_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    lines = [
        "# C18acc · provisional ALLDAY recovery (Phase 1)",
        "",
        f"- Topic：`{payload.get('topic')}`",
        f"- Window：`{payload.get('date_start')}` → `{payload.get('date_end')}` · "
        f"{payload.get('n_trade_dates')} TD",
        f"- Split：IS ≤ `{payload.get('is_end')}` · OOS ≥ `{payload.get('oos_start')}`",
        f"- Clocks：`{payload.get('clocks')}`",
        f"- Verdict：{v.get('summary')}",
        f"- Decision：`{v.get('decision')}` · winner=`{v.get('winner_variant')}`",
        f"- Order action：{v.get('order_action')}",
        "",
        "## Variants",
        "",
        "| ID | confirm | n FULL | excess FULL | compound | excess OOS | ΔOOS vs B0 | adopt |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in payload.get("variants") or []:
        f = (r.get("slices") or {}).get("full") or {}
        o = (r.get("slices") or {}).get("oos") or {}
        lines.append(
            f"| `{r.get('variant_id')}` | {r.get('confirm_bars')} | {f.get('n_legs')} | "
            f"{f.get('mean_excess_pct')} | {f.get('compound_slot_pct')} | "
            f"{o.get('mean_excess_pct')} | {r.get('delta_oos_excess_pp')} | "
            f"{r.get('adopt') or 'baseline'} |"
        )
    obs = v.get("observe_sleeve") or {}
    lines += [
        "",
        "## GO gates",
        "",
        f"- OOS n≥{ADOPT_OOS_MIN_N} · Δexcess≥{ADOPT_OOS_MIN_DELTA_PP}pp · "
        f"IS Δ≥{ADOPT_IS_MIN_DELTA_PP}pp",
        "",
        "## Decision",
        "",
        f"- `{v.get('decision')}` · order：{v.get('order_action')}",
    ]
    if obs:
        lines += [
            f"- Observe sleeve topic：`{obs.get('topic')}`",
            f"- Observe action：{obs.get('action')}",
            "- 舊版 cinema 僅上限展示，不驅動下單。",
        ]
    lines += [
        "",
        "---",
        "模組：`src/research/backtest/c18acc_provisional_allday_recovery.py`",
        "",
    ]
    return "\n".join(lines)
