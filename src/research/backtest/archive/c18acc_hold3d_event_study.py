"""C18acc · hold 3d event-level study · aligned with ABC v3+f1 pipeline contract."""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import replace
from datetime import datetime
from typing import Any

from flow_returns import exit_close_date_from_entry, return_pct
from market_benchmark import load_benchmark_close
from research.backtest.archive.abc_v3_slot_sweep import (
    ABC_V3_HOLD_DAYS,
    _build_abc_v3_intraday_panel,
    _unconstrained_leg_stats,
    collect_abc_v3_skip0900_period_candidates,
    split_train_val_dates,
)
from research.backtest.archive.c18acc_abc_v3_f1_comparison import ABC_F1_PIPELINE_CHAMPION
from research.backtest.c18acc_rotation_selective_exit_sweep import rotation_selective_exit_sweep_configs
from research.backtest.dual_wma_signal_backtest import ROUND_TRIP_COST_PCT
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import (
    LOOKBACK,
    build_fresh_mono_calendar,
    build_mono_up_calendar,
    build_mono_up_fresh_calendar,
)
from research.backtest.rrg_mono_intraday_ab import LENGTH
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    _swap_px,
    build_pit_candidate_pool,
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from research.backtest.triple_wma_pullback_sweep import matches_w3_rv_hl_abc_v3_f1
from rrg_rotation import compute_rrg_panel
from rrg_universe_intraday_panel import DEFAULT_POLL_END
from rrg_universe_snapshot import kbar_close_at_minute

C18ACC_SLOTS_DEFAULT = 3
C18ACC_UNCONSTRAINED_SLOTS = 99
EXIT_MINUTE = DEFAULT_POLL_END


def c18acc_live_s2_config(*, n_slots: int) -> ScoreSwapCConfig:
    """Champion poll_5m POOL1 + S2 selective quad force-exit · live-aligned."""
    s2 = {c.variant_id: c for c in rotation_selective_exit_sweep_configs()}["S2"]
    champ = champion_score_swap_c_config()
    return replace(
        s2,
        timing_mode=champ.timing_mode,
        poll_interval_min=champ.poll_interval_min,
        no_trade_before=champ.no_trade_before,
        entry_leg=champ.entry_leg,
        candidate_pool=champ.candidate_pool,
        min_hold_days=champ.min_hold_days,
        max_hold_days=champ.max_hold_days,
        sort_key=champ.sort_key,
        score_margin=champ.score_margin,
        accel_sell_negative_only=champ.accel_sell_negative_only,
        buy_sort_key=champ.buy_sort_key,
        accel_lookback=champ.accel_lookback,
        candidate_lookback=champ.candidate_lookback,
        accel_sell_bypass_on_spread_lost=champ.accel_sell_bypass_on_spread_lost,
        n_slots=max(1, int(n_slots)),
        variant_id="C18acc-S2-P5M",
        label="C18acc champion S2 · poll_5m · fresh∪accel",
    )


def _event_stats_from_nets(
    nets: list[float],
    *,
    hold_days: int | None = None,
) -> dict[str, Any]:
    if not nets:
        return {"n": 0}
    wins = sum(1 for x in nets if x > 0)
    stats: dict[str, Any] = {
        "n": len(nets),
        "mean_ret_net_pct": round(sum(nets) / len(nets), 3),
        "win_rate_pct": round(100.0 * wins / len(nets), 2),
        "median_ret_net_pct": round(statistics.median(nets), 3),
    }
    if hold_days and hold_days > 0:
        stats["mean_daily_ret_net_pct"] = round(sum(nets) / hold_days / len(nets), 4)
    return stats


def compute_hold_nd_event_leg(
    conn: sqlite3.Connection,
    *,
    stock_id: str,
    entry_date: str,
    entry_minute: str | None = None,
    entry_px: float | None = None,
    hold_days: int = ABC_V3_HOLD_DAYS,
) -> dict[str, Any] | None:
    """Counterfactual hold Nd exit at 13:30 · same cost model as ABC event layer."""
    exit_date = exit_close_date_from_entry(conn, entry_date, hold_days)
    if not exit_date:
        return None
    minute_in = entry_minute or "09:30"
    px_in = entry_px
    if px_in is None or px_in <= 0:
        px_in = kbar_close_at_minute(conn, stock_id, entry_date, minute_in)
    px_out = kbar_close_at_minute(conn, stock_id, exit_date, EXIT_MINUTE)
    b_in = kbar_close_at_minute(conn, "IX0001", entry_date, minute_in)
    b_out = kbar_close_at_minute(conn, "IX0001", exit_date, EXIT_MINUTE)
    if not px_in or not px_out or not b_in or not b_out:
        return None
    gross = return_pct(float(px_in), float(px_out))
    bench = return_pct(float(b_in), float(b_out))
    net = gross - ROUND_TRIP_COST_PCT
    return {
        "stock_id": stock_id,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "entry_minute": minute_in,
        "exit_minute": EXIT_MINUTE,
        "entry_px": round(float(px_in), 4),
        "exit_px": round(float(px_out), 4),
        "gross_pct": round(gross, 3),
        "net_pct": round(net, 3),
        "excess_pct": round(net - bench, 3),
        "hold_days": hold_days,
    }


def _legs_to_hold3d_stats(
    conn: sqlite3.Connection,
    entries: list[dict[str, Any]],
    *,
    hold_days: int = ABC_V3_HOLD_DAYS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    legs: list[dict[str, Any]] = []
    for row in entries:
        leg = compute_hold_nd_event_leg(
            conn,
            stock_id=str(row["stock_id"]),
            entry_date=str(row["entry_date"]),
            entry_minute=row.get("entry_minute"),
            entry_px=row.get("entry_px"),
            hold_days=hold_days,
        )
        if leg is None:
            continue
        legs.append(leg)
    nets = [float(lg["net_pct"]) for lg in legs if lg.get("net_pct") is not None]
    return legs, _event_stats_from_nets(nets, hold_days=hold_days)


def _prepare_c18acc_context(
    conn: sqlite3.Connection,
    trade_dates: list[str],
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    from market_breadth_ma import build_breadth_panel

    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates, lookback=LOOKBACK)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    mono_up_by_date = build_mono_up_calendar(conn, trade_dates, require_disp=True)
    mono_up_fresh_by_date = build_mono_up_fresh_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=trade_dates[0], date_end=trade_dates[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    return {
        "close": close,
        "bench": bench,
        "rs_ratio": rs_ratio,
        "rs_mom": rs_mom,
        "full_dates": full_dates,
        "fresh_by_date": fresh_by_date,
        "mono_by_date": mono_by_date,
        "mono_up_by_date": mono_up_by_date,
        "mono_up_fresh_by_date": mono_up_fresh_by_date,
        "zone_by_date": zone_by_date,
        "kbar_cache": {},
    }


def collect_c18acc_simulation_entries(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    n_slots: int,
    ctx: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """C18acc slot simulation entries · entry_date/px/minute from settled legs."""
    context = ctx or _prepare_c18acc_context(conn, trade_dates)
    cfg = c18acc_live_s2_config(n_slots=n_slots)
    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=context["full_dates"],
        close=context["close"],
        bench=context["bench"],
        fresh_by_date=context["fresh_by_date"],
        zone_by_date=context["zone_by_date"],
        config=cfg,
        mono_by_date=context["mono_by_date"],
        mono_up_by_date=context["mono_up_by_date"],
        mono_up_fresh_by_date=context["mono_up_fresh_by_date"],
        kbar_cache=context["kbar_cache"],
        rs_mom=context["rs_mom"],
        rs_ratio=context["rs_ratio"],
    )
    entries = [
        {
            "stock_id": str(p["stock_id"]),
            "stock_name": p.get("stock_name") or "",
            "entry_date": str(p["entry_date"]),
            "entry_minute": p.get("entry_minute"),
            "entry_px": p.get("entry_px"),
            "actual_exit_date": str(p.get("exit_date") or ""),
            "actual_return_pct": p.get("return_pct"),
            "exit_reason": p.get("exit_reason"),
            "n_slots": n_slots,
        }
        for p in periods
    ]
    meta = {
        "n_slots": n_slots,
        "n_entries": len(entries),
        "sim_summary": summary,
    }
    return entries, meta


def collect_c18acc_daily_pool_entries(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    ctx: dict[str, Any] | None = None,
    top_n: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Each day · each stock in PIT entry pool (top-N) · poll_5m entry px."""
    context = ctx or _prepare_c18acc_context(conn, trade_dates)
    cfg = c18acc_live_s2_config(n_slots=1)
    limit = top_n if top_n is not None else int(cfg.candidate_top_n)
    entries: list[dict[str, Any]] = []
    for as_of in trade_dates:
        fresh_mono = context["fresh_by_date"].get(as_of, [])
        mono_rows = context["mono_by_date"].get(as_of, [])
        pool = build_pit_candidate_pool(
            fresh_mono=fresh_mono,
            mono_rows=mono_rows,
            rs_ratio=context["rs_ratio"],
            rs_mom=context["rs_mom"],
            full_dates=context["full_dates"],
            as_of=as_of,
            config=cfg,
        )
        for rank, row in enumerate(pool[:limit], start=1):
            px, minute = _swap_px(
                conn,
                close=context["close"],
                stock_id=row.stock_id,
                trade_date=as_of,
                config=cfg,
                kbar_cache=context["kbar_cache"],
            )
            if px is None or px <= 0:
                continue
            entries.append(
                {
                    "stock_id": row.stock_id,
                    "stock_name": row.stock_name,
                    "entry_date": as_of,
                    "entry_minute": minute,
                    "entry_px": round(float(px), 4),
                    "pool_rank": rank,
                }
            )
    return entries, {"pool_top_n": limit, "n_entries": len(entries)}


def run_c18acc_hold3d_event_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-01",
    date_end: str | None = None,
    intraday_tail_days: int = 90,
    train_days: int = 60,
    val_days: int = 30,
    long_window: bool = False,
) -> dict[str, Any]:
    """C18acc hold-3d event study vs ABC f1 champion · same window contract as pipeline."""
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    if long_window:
        sample_dates = trade_dates
    elif intraday_tail_days > 0 and len(trade_dates) > intraday_tail_days:
        sample_dates = trade_dates[-intraday_tail_days:]
    else:
        sample_dates = trade_dates
    if len(sample_dates) < 5:
        raise ValueError(f"insufficient sample dates: {len(sample_dates)}")

    train_dates, val_dates = split_train_val_dates(
        sample_dates, train_days=train_days, val_days=val_days
    )
    train_set = set(train_dates)
    val_set = set(val_dates)

    ctx = _prepare_c18acc_context(conn, sample_dates)

    entries_uncon, meta_uncon = collect_c18acc_simulation_entries(
        conn, sample_dates, n_slots=C18ACC_UNCONSTRAINED_SLOTS, ctx=ctx
    )
    entries_3slot, meta_3slot = collect_c18acc_simulation_entries(
        conn, sample_dates, n_slots=C18ACC_SLOTS_DEFAULT, ctx=ctx
    )
    pool_entries, meta_pool = collect_c18acc_daily_pool_entries(
        conn, sample_dates, ctx=ctx
    )

    legs_uncon, stats_uncon = _legs_to_hold3d_stats(conn, entries_uncon)
    legs_3slot, stats_3slot = _legs_to_hold3d_stats(conn, entries_3slot)
    legs_pool, stats_pool = _legs_to_hold3d_stats(conn, pool_entries)

    actual_nets = [
        float(e["actual_return_pct"])
        for e in entries_3slot
        if e.get("actual_return_pct") is not None
    ]
    stats_actual = _event_stats_from_nets(actual_nets)
    if stats_actual.get("n"):
        stats_actual["hold_note"] = "S2 actual exit · not 3d"

    frames, trajectories, panel_meta, stock_maps, bench = _build_abc_v3_intraday_panel(
        conn, dates=sample_dates
    )
    panel_kw = {
        "frames": frames,
        "trajectories": trajectories,
        "meta": panel_meta,
        "stock_maps": stock_maps,
        "bench": bench,
    }
    abc_cands, _, _ = collect_abc_v3_skip0900_period_candidates(
        conn,
        dates=sample_dates,
        matcher=matches_w3_rv_hl_abc_v3_f1,
        **panel_kw,
    )
    abc_all = _unconstrained_leg_stats(abc_cands)
    abc_val = _unconstrained_leg_stats(
        [c for c in abc_cands if str(c.get("entry_date")) in val_set]
    )
    abc_train = _unconstrained_leg_stats(
        [c for c in abc_cands if str(c.get("entry_date")) in train_set]
    )

    def _slice_stats(legs: list[dict[str, Any]], dates: set[str]) -> dict[str, Any]:
        subset = [lg for lg in legs if str(lg.get("entry_date")) in dates]
        nets = [float(lg["net_pct"]) for lg in subset if lg.get("net_pct") is not None]
        return _event_stats_from_nets(nets, hold_days=ABC_V3_HOLD_DAYS)

    c18_slices = {
        "unconstrained": {
            "full": stats_uncon,
            "train": _slice_stats(legs_uncon, train_set),
            "val": _slice_stats(legs_uncon, val_set),
        },
        "slot_3": {
            "full": stats_3slot,
            "train": _slice_stats(legs_3slot, train_set),
            "val": _slice_stats(legs_3slot, val_set),
        },
        "pool_daily": {
            "full": stats_pool,
            "train": _slice_stats(legs_pool, train_set),
            "val": _slice_stats(legs_pool, val_set),
        },
    }

    champ_mean = float(ABC_F1_PIPELINE_CHAMPION["mean_ret_3d_net_pct"])
    best_c18 = max(
        float(stats_uncon.get("mean_ret_net_pct") or -999),
        float(stats_3slot.get("mean_ret_net_pct") or -999),
        float(stats_pool.get("mean_ret_net_pct") or -999),
    )
    delta_vs_champion = round(best_c18 - champ_mean, 3) if best_c18 > -900 else None

    capture_pct = None
    if entries_uncon and entries_3slot:
        uncon_keys = {(e["stock_id"], e["entry_date"]) for e in entries_uncon}
        slot_keys = {(e["stock_id"], e["entry_date"]) for e in entries_3slot}
        capture_pct = round(100.0 * len(slot_keys) / len(uncon_keys), 1) if uncon_keys else None

    verdict_reasons = [
        f"C18 unconstrained hold3d n={stats_uncon.get('n')} ret={stats_uncon.get('mean_ret_net_pct')}%",
        f"C18 3-slot hold3d n={stats_3slot.get('n')} ret={stats_3slot.get('mean_ret_net_pct')}%",
        f"C18 pool-daily hold3d n={stats_pool.get('n')} ret={stats_pool.get('mean_ret_net_pct')}%",
        f"ABC f1 same-window unconstrained n={abc_all.get('n')} ret={abc_all.get('mean_ret_net_pct')}%",
        f"Champion OOS reference ret={champ_mean}% · Δ best C18 − champion = {delta_vs_champion}pp",
    ]
    if capture_pct is not None:
        verdict_reasons.append(
            f"3-slot captures {capture_pct}% of unconstrained entries "
            f"({len(entries_3slot)}/{len(entries_uncon)})"
        )

    return {
        "schema": "c18acc_hold3d_event_study-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "contract": {
            "hold_days": ABC_V3_HOLD_DAYS,
            "exit_minute": EXIT_MINUTE,
            "cost_round_trip_pct": ROUND_TRIP_COST_PCT,
            "execution": "poll_5m · C18acc champion S2 · fresh∪accel",
            "abc_reference": "w3_rv_hl_abc_v3_f1 · skip09 · min_poll_idx>=1",
        },
        "window": {
            "date_start": sample_dates[0],
            "date_end": sample_dates[-1],
            "n_trading_days": len(sample_dates),
            "long_window": long_window,
            "intraday_tail_days": None if long_window else intraday_tail_days,
            "train_dates": train_dates,
            "val_dates": val_dates,
        },
        "champion_reference": dict(ABC_F1_PIPELINE_CHAMPION),
        "c18acc": {
            "config": "champion S2 · poll_5m · fresh∪accel",
            "unconstrained_slots": C18ACC_UNCONSTRAINED_SLOTS,
            "default_slots": C18ACC_SLOTS_DEFAULT,
            "entries_unconstrained": meta_uncon,
            "entries_3slot": meta_3slot,
            "entries_pool_daily": meta_pool,
            "hold3d": c18_slices,
            "actual_exit_3slot": stats_actual,
            "slot_capture_pct": capture_pct,
        },
        "abc_f1": {
            "hold3d": {
                "full": abc_all,
                "train": abc_train,
                "val": abc_val,
            },
        },
        "verdict": {
            "beats_champion_event_mean": (
                best_c18 > champ_mean if delta_vs_champion is not None else None
            ),
            "delta_best_c18_vs_champion_pp": delta_vs_champion,
            "best_c18_hold3d_mean_pct": round(best_c18, 3) if best_c18 > -900 else None,
            "reasons": verdict_reasons,
            "summary": (
                f"C18acc hold3d event study · best C18 {round(best_c18, 3) if best_c18 > -900 else '—'}% "
                f"vs ABC champion {champ_mean}% "
                f"({'beats' if delta_vs_champion and delta_vs_champion > 0 else 'does not beat'} champion · "
                f"Δ {delta_vs_champion}pp)"
            ),
        },
    }


def render_c18acc_hold3d_event_study_md(payload: dict[str, Any]) -> str:
    w = payload.get("window") or {}
    champ = payload.get("champion_reference") or {}
    c18 = payload.get("c18acc") or {}
    abc = (payload.get("abc_f1") or {}).get("hold3d") or {}
    v = payload.get("verdict") or {}
    contract = payload.get("contract") or {}

    def _row(label: str, block: dict[str, Any]) -> str:
        return (
            f"| {label} | {block.get('n', '—')} | {block.get('mean_ret_net_pct', '—')} | "
            f"{block.get('mean_daily_ret_net_pct', '—')} | {block.get('win_rate_pct', '—')} | "
            f"{block.get('median_ret_net_pct', '—')} |"
        )

    h3 = c18.get("hold3d") or {}
    uncon = (h3.get("unconstrained") or {}).get("full") or {}
    slot3 = (h3.get("slot_3") or {}).get("full") or {}
    pool = (h3.get("pool_daily") or {}).get("full") or {}
    actual = c18.get("actual_exit_3slot") or {}
    abc_full = abc.get("full") or {}
    abc_val = abc.get("val") or {}

    lines = [
        "# C18acc · hold 3d event-level study",
        "",
        f"> {v.get('summary', '')}",
        "",
        "## Contract（对齐 ABC v3+f1 pipeline）",
        "",
        f"- Hold: **{contract.get('hold_days')}** trading days → **{contract.get('exit_minute')}**",
        f"- Cost: round-trip **{contract.get('cost_round_trip_pct')}%**",
        f"- C18: {contract.get('execution')}",
        f"- ABC ref: {contract.get('abc_reference')}",
        f"- Window: **{w.get('date_start')} → {w.get('date_end')}** "
        f"({w.get('n_trading_days')} td)"
        + (" · long window" if w.get("long_window") else f" · tail {w.get('intraday_tail_days')}d"),
        "",
        "## Champion reference（ABC f1 · 採納 SSOT）",
        "",
        "| 口徑 | n | 每筆 3d % | 勝率 % |",
        "|------|--:|----------:|-------:|",
        f"| Pipeline OOS champion | **{champ.get('n')}** | **{champ.get('mean_ret_3d_net_pct')}** | "
        f"**{champ.get('win_rate_pct')}** |",
        "",
        "## C18acc · hold 3d（counterfactual exit）",
        "",
        "| 口徑 | n | 每筆 3d % | 日均 % | 勝率 % | 中位數 % |",
        "|------|--:|----------:|-------:|-------:|---------:|",
        _row(f"Unconstrained ({c18.get('unconstrained_slots')} slots)", uncon),
        _row(f"3-slot executed", slot3),
        _row("Daily pool · all top-N", pool),
        "",
        f"- 3-slot capture vs unconstrained: **{c18.get('slot_capture_pct')}%**",
        "",
        "### C18 · actual S2 exit（非 3d · 对照）",
        "",
        f"- n={actual.get('n')} · mean={actual.get('mean_ret_net_pct')}% · "
        f"win={actual.get('win_rate_pct')}% · {actual.get('hold_note', '')}",
        "",
        "## ABC v3+f1 · same window",
        "",
        "| 口徑 | n | 每筆 3d % | 日均 % | 勝率 % |",
        "|------|--:|----------:|-------:|-------:|",
        f"| Full unconstrained | {abc_full.get('n')} | {abc_full.get('mean_ret_net_pct')} | "
        f"{abc_full.get('mean_daily_ret_net_pct')} | {abc_full.get('win_rate_pct')} |",
        f"| Val hold-out | {abc_val.get('n')} | {abc_val.get('mean_ret_net_pct')} | "
        f"{abc_val.get('mean_daily_ret_net_pct')} | {abc_val.get('win_rate_pct')} |",
        "",
        "## Verdict",
        "",
        f"- Beats champion event mean: **{v.get('beats_champion_event_mean')}**",
        f"- Best C18 hold3d: **{v.get('best_c18_hold3d_mean_pct')}%** · "
        f"Δ vs champion: **{v.get('delta_best_c18_vs_champion_pp')}** pp",
        "",
        "### Notes",
        "",
        "- **Unconstrained** = slot sim with 99 slots · approximates「想下就下」",
        "- **Pool daily** = each day every stock in PIT entry pool (top-N) · no slot competition",
        "- Champion 2.845% is **90d OOS pipeline** · compare same-window rows for fair read",
        "",
        "模組：`scripts/research/archive/run_c18acc_hold3d_event_study.py`",
    ]
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    return "\n".join(lines) + "\n"
