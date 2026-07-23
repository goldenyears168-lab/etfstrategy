"""ABC v3+f1 entry · C18 S2 exit overlay · leg-level counterfactual."""

from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime
from typing import Any

from analytics.bench import bench_return_entry_to_exit
from flow_returns import return_pct
from research.backtest.archive.abc_v3_slot_sweep import (
    ABC_V3_HOLD_DAYS,
    _build_abc_v3_intraday_panel,
    collect_abc_v3_skip0900_period_candidates,
    split_train_val_dates,
)
from research.backtest.archive.c18acc_abc_v3_f1_comparison import ABC_F1_PIPELINE_CHAMPION
from research.backtest.archive.c18acc_hold3d_event_study import (
    C18ACC_SLOTS_DEFAULT,
    _prepare_c18acc_context,
    c18acc_live_s2_config,
)
from research.backtest.dual_wma_signal_backtest import ROUND_TRIP_COST_PCT
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_intraday_exit import _trading_days_between
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    _close_return_vs_entry,
    _effective_min_hold_days,
    _swap_px,
    quad_force_exit_eligible,
    quad_force_exit_streak_days,
    simulate_score_swap_c,
)
from research.backtest.triple_wma_pullback_sweep import matches_w3_rv_hl_abc_v3_f1

EXIT_MODE_HOLD3D = "hold_3d"
EXIT_MODE_S2_SINGLE = "s2_single_leg"
EXIT_MODE_C18_ACTUAL = "c18_s2_actual"


def _leg_stats_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    nets = [float(r["net_pct"]) for r in rows if r.get("net_pct") is not None]
    if not nets:
        return {"n": 0}
    holds = [float(r["hold_days"]) for r in rows if r.get("hold_days") is not None]
    wins = sum(1 for x in nets if x > 0)
    stats: dict[str, Any] = {
        "n": len(nets),
        "mean_ret_net_pct": round(sum(nets) / len(nets), 3),
        "win_rate_pct": round(100.0 * wins / len(nets), 2),
        "median_ret_net_pct": round(statistics.median(nets), 3),
    }
    if holds:
        mean_hold = sum(holds) / len(holds)
        stats["mean_hold_days"] = round(mean_hold, 2)
        stats["mean_daily_ret_net_pct"] = round(
            sum(nets) / len(nets) / mean_hold if mean_hold > 0 else 0.0, 4
        )
    return stats


def _exit_reason_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        reason = str(row.get("exit_reason") or "unknown")
        out[reason] = out.get(reason, 0) + 1
    return out


def simulate_single_leg_s2_exit(
    conn: sqlite3.Connection,
    entry: dict[str, Any],
    *,
    full_dates: list[str],
    close: Any,
    rs_ratio: Any,
    rs_mom: Any,
    config: ScoreSwapCConfig,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> dict[str, Any] | None:
    """Walk-forward one leg · S2 quad force-exit + max_hold · no swap."""
    stock_id = str(entry["stock_id"])
    entry_date = str(entry["entry_date"])
    entry_px = float(entry.get("entry_px") or 0)
    if entry_px <= 0 or entry_date not in full_dates:
        return None

    pos = {
        "stock_id": stock_id,
        "stock_name": entry.get("stock_name") or "",
        "entry_date": entry_date,
        "entry_px": entry_px,
        "entry_minute": entry.get("entry_minute"),
        "signal_date": entry_date,
    }
    start_i = full_dates.index(entry_date)
    exit_date: str | None = None
    exit_px: float | None = None
    exit_minute: str | None = None
    exit_reason: str | None = None
    streak = 0

    for as_of in full_dates[start_i:]:
        hold_days = _trading_days_between(full_dates, entry_date, as_of)
        eff_min = _effective_min_hold_days(config, close, full_dates, pos, as_of)

        streak = quad_force_exit_streak_days(
            rs_ratio,
            rs_mom,
            full_dates,
            as_of=as_of,
            stock_id=stock_id,
            entry_date=entry_date,
            mode=config.quad_force_exit_mode,
        )
        unrealized = _close_return_vs_entry(
            close,
            entry_date=entry_date,
            entry_px=entry_px,
            stock_id=stock_id,
            as_of=as_of,
        )
        if quad_force_exit_eligible(
            config,
            hold_days=hold_days,
            effective_min_hold=eff_min,
            streak_days=streak,
            unrealized_ret=unrealized,
        ):
            px, minute = _swap_px(
                conn,
                close=close,
                stock_id=stock_id,
                trade_date=as_of,
                config=config,
                kbar_cache=kbar_cache,
            )
            if px is not None and px > 0:
                exit_date, exit_px, exit_minute, exit_reason = as_of, float(px), minute, "quad_force_exit"
                break

        if hold_days >= config.max_hold_days:
            px, minute = _swap_px(
                conn,
                close=close,
                stock_id=stock_id,
                trade_date=as_of,
                config=config,
                kbar_cache=kbar_cache,
            )
            if px is not None and px > 0:
                exit_date, exit_px, exit_minute, exit_reason = as_of, float(px), minute, "max_hold"
                break

    if exit_date is None or exit_px is None:
        as_of = full_dates[min(start_i + config.max_hold_days, len(full_dates) - 1)]
        px, minute = _swap_px(
            conn,
            close=close,
            stock_id=stock_id,
            trade_date=as_of,
            config=config,
            kbar_cache=kbar_cache,
        )
        if px is None or px <= 0:
            return None
        exit_date, exit_px, exit_minute, exit_reason = as_of, float(px), minute, "window_end"

    gross = return_pct(entry_px, exit_px)
    bench = bench_return_entry_to_exit(conn, entry_date, exit_date, entry_price_mode="close")
    if bench is None:
        return None
    net = gross - ROUND_TRIP_COST_PCT
    hold_days = _trading_days_between(full_dates, entry_date, exit_date)
    return {
        "stock_id": stock_id,
        "stock_name": pos.get("stock_name") or "",
        "entry_date": entry_date,
        "exit_date": exit_date,
        "entry_minute": pos.get("entry_minute"),
        "exit_minute": exit_minute,
        "entry_px": round(entry_px, 4),
        "exit_px": round(exit_px, 4),
        "gross_pct": round(gross, 3),
        "net_pct": round(net, 3),
        "excess_pct": round(net - bench, 3),
        "hold_days": hold_days,
        "exit_reason": exit_reason,
        "quad_force_exit_streak": streak if exit_reason == "quad_force_exit" else None,
        "entry_source": str(entry.get("source_strategy") or "abc_v3_f1_skip09"),
        "exit_mode": EXIT_MODE_S2_SINGLE,
    }


def _abc_hold3d_rows(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cand in candidates:
        net = cand.get("net_pct")
        if net is None:
            continue
        rows.append(
            {
                "stock_id": cand.get("stock_id"),
                "stock_name": cand.get("stock_name") or "",
                "entry_date": cand.get("entry_date"),
                "exit_date": cand.get("exit_date"),
                "net_pct": float(net),
                "hold_days": ABC_V3_HOLD_DAYS,
                "exit_reason": "hold_3d",
                "exit_mode": EXIT_MODE_HOLD3D,
                "entry_source": cand.get("source_strategy"),
            }
        )
    return rows


def _c18_actual_rows(periods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for period in periods:
        ret = period.get("return_pct")
        if ret is None:
            continue
        rows.append(
            {
                "stock_id": period.get("stock_id"),
                "stock_name": period.get("stock_name") or "",
                "entry_date": period.get("entry_date"),
                "exit_date": period.get("exit_date"),
                "net_pct": float(ret),
                "hold_days": period.get("hold_days"),
                "exit_reason": period.get("exit_reason"),
                "exit_mode": EXIT_MODE_C18_ACTUAL,
                "entry_source": "c18acc_s2",
            }
        )
    return rows


def run_abc_f1_c18_s2_exit_overlay_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-01",
    date_end: str | None = None,
    intraday_tail_days: int = 90,
    train_days: int = 60,
    val_days: int = 30,
    long_window: bool = False,
) -> dict[str, Any]:
    """ABC f1 entries · hold 3d vs C18 S2 single-leg exit · C18 actual reference."""
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
    train_set, val_set = set(train_dates), set(val_dates)

    frames, trajectories, panel_meta, stock_maps, bench_ix = _build_abc_v3_intraday_panel(
        conn, dates=sample_dates
    )
    panel_kw = {
        "frames": frames,
        "trajectories": trajectories,
        "meta": panel_meta,
        "stock_maps": stock_maps,
        "bench": bench_ix,
    }
    abc_cands, _, _ = collect_abc_v3_skip0900_period_candidates(
        conn,
        dates=sample_dates,
        matcher=matches_w3_rv_hl_abc_v3_f1,
        **panel_kw,
    )

    s2_cfg = c18acc_live_s2_config(n_slots=1)
    ctx = _prepare_c18acc_context(conn, sample_dates)
    kbar_cache = ctx["kbar_cache"]

    s2_rows: list[dict[str, Any]] = []
    for cand in abc_cands:
        leg = simulate_single_leg_s2_exit(
            conn,
            cand,
            full_dates=ctx["full_dates"],
            close=ctx["close"],
            rs_ratio=ctx["rs_ratio"],
            rs_mom=ctx["rs_mom"],
            config=s2_cfg,
            kbar_cache=kbar_cache,
        )
        if leg is not None:
            s2_rows.append(leg)

    hold3d_rows = _abc_hold3d_rows(abc_cands)

    c18_cfg = c18acc_live_s2_config(n_slots=C18ACC_SLOTS_DEFAULT)
    c18_periods, c18_sim = simulate_score_swap_c(
        conn,
        trade_dates=sample_dates,
        full_dates=ctx["full_dates"],
        close=ctx["close"],
        bench=ctx["bench"],
        fresh_by_date=ctx["fresh_by_date"],
        zone_by_date=ctx["zone_by_date"],
        config=c18_cfg,
        mono_by_date=ctx["mono_by_date"],
        mono_up_by_date=ctx["mono_up_by_date"],
        mono_up_fresh_by_date=ctx["mono_up_fresh_by_date"],
        kbar_cache=kbar_cache,
        rs_mom=ctx["rs_mom"],
        rs_ratio=ctx["rs_ratio"],
    )
    c18_rows = _c18_actual_rows(c18_periods)

    def _slice(rows: list[dict[str, Any]], dates: set[str]) -> dict[str, Any]:
        sub = [r for r in rows if str(r.get("entry_date") or "") in dates]
        return _leg_stats_from_rows(sub)

    variants = {
        "abc_f1_hold3d": {
            "label": "ABC f1 · hold 3d → 13:30",
            "entry": "abc_v3_f1",
            "exit": "hold_3d",
            "full": _leg_stats_from_rows(hold3d_rows),
            "train": _slice(hold3d_rows, train_set),
            "val": _slice(hold3d_rows, val_set),
            "exit_reasons": _exit_reason_counts(hold3d_rows),
        },
        "abc_f1_s2_single": {
            "label": "ABC f1 entry · C18 S2 exit (single leg · no swap)",
            "entry": "abc_v3_f1",
            "exit": "s2_quad_force_max_hold",
            "full": _leg_stats_from_rows(s2_rows),
            "train": _slice(s2_rows, train_set),
            "val": _slice(s2_rows, val_set),
            "exit_reasons": _exit_reason_counts(s2_rows),
        },
        "c18_s2_actual": {
            "label": "C18acc · actual S2 + rotation (3-slot)",
            "entry": "c18acc",
            "exit": "s2_portfolio",
            "full": _leg_stats_from_rows(c18_rows),
            "train": _slice(c18_rows, train_set),
            "val": _slice(c18_rows, val_set),
            "exit_reasons": _exit_reason_counts(c18_rows),
        },
    }

    h3 = variants["abc_f1_hold3d"]["full"]
    s2 = variants["abc_f1_s2_single"]["full"]
    c18 = variants["c18_s2_actual"]["full"]
    champ = float(ABC_F1_PIPELINE_CHAMPION["mean_ret_3d_net_pct"])

    delta_s2_vs_h3 = None
    if h3.get("n") and s2.get("n"):
        delta_s2_vs_h3 = round(
            float(s2.get("mean_ret_net_pct") or 0) - float(h3.get("mean_ret_net_pct") or 0),
            3,
        )
    delta_s2_vs_c18 = None
    if s2.get("n") and c18.get("n"):
        delta_s2_vs_c18 = round(
            float(s2.get("mean_ret_net_pct") or 0) - float(c18.get("mean_ret_net_pct") or 0),
            3,
        )

    reasons = [
        f"ABC hold3d n={h3.get('n')} ret={h3.get('mean_ret_net_pct')}% "
        f"daily={h3.get('mean_daily_ret_net_pct')}%",
        f"ABC+S2 single n={s2.get('n')} ret={s2.get('mean_ret_net_pct')}% "
        f"hold={s2.get('mean_hold_days')}d daily={s2.get('mean_daily_ret_net_pct')}%",
        f"C18 actual n={c18.get('n')} ret={c18.get('mean_ret_net_pct')}% "
        f"hold={c18.get('mean_hold_days')}d daily={c18.get('mean_daily_ret_net_pct')}%",
        f"Δ S2−hold3d = {delta_s2_vs_h3}pp · Δ S2−C18 = {delta_s2_vs_c18}pp",
        f"Champion hold3d ref = {champ}%",
    ]

    return {
        "schema": "abc_f1_c18_s2_exit_overlay-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "contract": {
            "abc_entry": "w3_rv_hl_abc_v3_f1 · skip09 · min_poll_idx>=1",
            "s2_exit": (
                "quad_force_exit weakening 2d + loss≥5% · max_hold 10 · min_hold 5 · poll_5m · no swap"
            ),
            "c18_reference": "champion S2 · 3-slot portfolio · rotation included",
            "cost_round_trip_pct": ROUND_TRIP_COST_PCT,
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
        "variants": variants,
        "abc_f1_n_candidates": len(abc_cands),
        "c18_meta": {"n_periods": len(c18_periods), "sim_summary": c18_sim},
        "verdict": {
            "delta_s2_vs_hold3d_pp": delta_s2_vs_h3,
            "delta_s2_vs_c18_actual_pp": delta_s2_vs_c18,
            "s2_beats_hold3d_mean": (
                delta_s2_vs_h3 is not None and delta_s2_vs_h3 > 0
            ),
            "s2_beats_c18_actual_mean": (
                delta_s2_vs_c18 is not None and delta_s2_vs_c18 > 0
            ),
            "reasons": reasons,
            "summary": (
                f"ABC f1 + S2 single-leg · ret {s2.get('mean_ret_net_pct')}% "
                f"vs hold3d {h3.get('mean_ret_net_pct')}% "
                f"vs C18 actual {c18.get('mean_ret_net_pct')}%"
            ),
        },
    }


def render_abc_f1_c18_s2_exit_overlay_md(payload: dict[str, Any]) -> str:
    w = payload.get("window") or {}
    v = payload.get("verdict") or {}
    contract = payload.get("contract") or {}
    champ = payload.get("champion_reference") or {}
    variants = payload.get("variants") or {}

    def _stat_row(name: str, block: dict[str, Any]) -> str:
        full = block.get("full") or {}
        return (
            f"| {name} | {full.get('n', '—')} | {full.get('mean_ret_net_pct', '—')} | "
            f"{full.get('mean_hold_days', '—')} | {full.get('mean_daily_ret_net_pct', '—')} | "
            f"{full.get('win_rate_pct', '—')} |"
        )

    lines = [
        "# ABC f1 entry · C18 S2 exit overlay",
        "",
        f"> {v.get('summary', '')}",
        "",
        "## Contract",
        "",
        f"- Window: **{w.get('date_start')} → {w.get('date_end')}** ({w.get('n_trading_days')} td)",
        f"- ABC entry: {contract.get('abc_entry')}",
        f"- S2 overlay: {contract.get('s2_exit')}",
        f"- C18 ref: {contract.get('c18_reference')}",
        "",
        "## Champion reference（ABC hold 3d 採納）",
        "",
        f"- n={champ.get('n')} · **{champ.get('mean_ret_3d_net_pct')}%** / 3d · win {champ.get('win_rate_pct')}%",
        "",
        "## Leg-level comparison",
        "",
        "| Variant | n | mean ret% | hold d | daily% | win% |",
        "|---------|--:|----------:|-------:|-------:|-----:|",
        _stat_row("ABC f1 · hold 3d", variants.get("abc_f1_hold3d") or {}),
        _stat_row("ABC f1 + S2 single", variants.get("abc_f1_s2_single") or {}),
        _stat_row("C18 · actual S2", variants.get("c18_s2_actual") or {}),
        "",
        "## Exit reasons",
        "",
    ]
    for key, block in variants.items():
        reasons = block.get("exit_reasons") or {}
        if not reasons:
            continue
        lines.append(f"### {block.get('label', key)}")
        lines.append("")
        for reason, count in sorted(reasons.items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"- {reason}: **{count}**")
        lines.append("")

    lines.extend(
        [
            "## Verdict",
            "",
            f"- Δ S2 − hold3d: **{v.get('delta_s2_vs_hold3d_pp')}** pp",
            f"- Δ S2 − C18 actual: **{v.get('delta_s2_vs_c18_actual_pp')}** pp",
            f"- S2 beats hold3d mean: **{v.get('s2_beats_hold3d_mean')}**",
            "",
            "### Notes",
            "",
            "- **S2 single** = ABC 进场价 + 单腿 forward S2（无 score_swap 换仓）",
            "- **C18 actual** = 含 3 槽轮动 · 不可与 ABC 单笔直接比部署率",
            "- 若 S2 single > hold3d 但 < C18 actual → 进场池差异 > 出场差异",
            "",
            "模組：`scripts/research/archive/run_abc_f1_c18_s2_exit_overlay.py`",
        ]
    )
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    return "\n".join(lines) + "\n"
