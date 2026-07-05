"""C18acc rotation-exit · Phase D0 swap block audit."""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

import pandas as pd

from market_benchmark import load_benchmark_close
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, LENGTH
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    _buy_threshold_score,
    _effective_min_hold_days,
    _position_score,
    _quad_sell_bypass_accel,
    _stock_end_q,
    _trading_days_between,
    close_champion_score_swap_c_config,
    simulate_score_swap_c,
)
from rrg_mono_daily_brief import ScanRow
from rrg_rotation import compute_rrg_panel

SwapBlockReason = Literal[
    "min_hold_lock",
    "accel_sell_negative_only",
    "no_challenger",
    "would_swap",
    "not_worst_held",
    "max_swaps_exhausted",
    "swap_zone_blocked",
]

WATCH_STOCKS = ("4979", "8996", "2467", "6274", "3293")
ROTATION_QUADS = ("weakening", "lagging")


def classify_daily_swap_block(
    *,
    hold_days: int,
    effective_min_hold: int,
    avg_accel: float | None,
    accel_sell_negative_only: bool,
    sort_key: str,
    quad_bypass: bool,
    sell_eligible: bool,
    is_worst_held: bool,
    has_challenger: bool,
    swaps_today: int,
    max_swaps_per_day: int,
    swap_zone_ok: bool,
) -> SwapBlockReason:
    """Classify why a held leg did not swap on as_of."""
    if hold_days < effective_min_hold:
        return "min_hold_lock"
    if (
        accel_sell_negative_only
        and sort_key in ("accel_decel", "avg_accel_decel")
        and not quad_bypass
        and not sell_eligible
    ):
        return "accel_sell_negative_only"
    if not sell_eligible:
        return "accel_sell_negative_only"
    if not is_worst_held:
        return "not_worst_held"
    if not swap_zone_ok:
        return "swap_zone_blocked"
    if not has_challenger:
        return "no_challenger"
    if swaps_today >= max_swaps_per_day:
        return "max_swaps_exhausted"
    return "would_swap"


def _leg_audit_key(stock_id: str, entry_date: str) -> str:
    return f"{stock_id}|{entry_date}"


def _has_challenger_for_sell(
    sell: dict[str, Any],
    candidates: list[ScanRow],
    *,
    held_ids: set[str],
    config: ScoreSwapCConfig,
    held_today: dict[str, float],
    challenger_avg_accel: dict[str, float],
) -> bool:
    margin = config.effective_margin
    key = config.sort_key
    eligible = [r for r in candidates if r.stock_id not in held_ids]
    if not eligible:
        return False
    use_seg_buy = key in ("accel_decel", "avg_accel_decel")
    threshold = _buy_threshold_score(sell, key) + margin
    if use_seg_buy or config.buy_sort_key is not None:
        return any(float(r.seg_last) > threshold for r in eligible)
    held_sc = _position_score(sell, key, held_today=held_today)
    return any(
        float(r.seg_last) > held_sc + margin
        if key == "seg_last"
        else _position_score({"stock_id": r.stock_id, "seg_last": r.seg_last}, key) > held_sc + margin
        for r in eligible
    )


def record_swap_block_audit_day(
    swap_block_audit: dict[str, list[dict[str, Any]]],
    *,
    as_of: str,
    slots: list[dict[str, Any]],
    swap_shortlist: list[ScanRow],
    config: ScoreSwapCConfig,
    close: pd.DataFrame,
    full_dates: list[str],
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    held_today: dict[str, float],
    held_accel_bypass: set[str],
    swaps_today: int,
    swap_zone_ok: bool,
) -> None:
    """Append daily block classification for legs in weakening/lagging."""
    if not slots:
        return
    held_ids = {str(p["stock_id"]) for p in slots}
    key = config.sort_key
    sell_pool = list(slots)
    if config.accel_sell_negative_only and key in ("accel_decel", "avg_accel_decel"):
        sell_pool = [
            p
            for p in sell_pool
            if _position_score(p, key, held_today=held_today) < 0
            or str(p["stock_id"]) in held_accel_bypass
        ]
    worst: dict[str, Any] | None = None
    if sell_pool:
        worst = min(sell_pool, key=lambda p: _position_score(p, key, held_today=held_today))

    for pos in slots:
        sid = str(pos["stock_id"])
        eq = _stock_end_q(rs_ratio, rs_mom, full_dates, as_of, sid)
        if eq not in ROTATION_QUADS:
            continue
        hold_days = _trading_days_between(full_dates, str(pos["entry_date"]), as_of)
        eff_min = _effective_min_hold_days(config, close, full_dates, pos, as_of)
        avg_accel = held_today.get(sid)
        quad_bypass = sid in held_accel_bypass or _quad_sell_bypass_accel(
            config,
            rs_ratio=rs_ratio,
            rs_mom=rs_mom,
            full_dates=full_dates,
            as_of=as_of,
            stock_id=sid,
            entry_date=str(pos["entry_date"]),
        )
        sell_eligible = pos in sell_pool
        is_worst = worst is not None and str(worst["stock_id"]) == sid
        has_ch = False
        if is_worst and sell_eligible:
            has_ch = _has_challenger_for_sell(
                pos,
                swap_shortlist,
                held_ids=held_ids,
                config=config,
                held_today=held_today,
                challenger_avg_accel=held_today,
            )
        reason = classify_daily_swap_block(
            hold_days=hold_days,
            effective_min_hold=eff_min,
            avg_accel=avg_accel,
            accel_sell_negative_only=config.accel_sell_negative_only,
            sort_key=key,
            quad_bypass=quad_bypass,
            sell_eligible=sell_eligible,
            is_worst_held=is_worst,
            has_challenger=has_ch,
            swaps_today=swaps_today,
            max_swaps_per_day=config.max_swaps_per_day,
            swap_zone_ok=swap_zone_ok,
        )
        audit_key = _leg_audit_key(sid, str(pos["entry_date"]))
        swap_block_audit.setdefault(audit_key, []).append(
            {
                "date": as_of,
                "quadrant": eq,
                "hold_days": hold_days,
                "effective_min_hold": eff_min,
                "avg_accel": avg_accel,
                "quad_bypass": quad_bypass,
                "block_reason": reason,
            }
        )


def _first_rotation_date(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    *,
    entry_date: str,
    exit_date: str,
    stock_id: str,
) -> str | None:
    for d in full_dates:
        if d <= entry_date or d > exit_date:
            continue
        q = _stock_end_q(rs_ratio, rs_mom, full_dates, d, stock_id)
        if q in ROTATION_QUADS:
            return d
    return None


def _days_to_rotation(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    *,
    entry_date: str,
    exit_date: str,
    stock_id: str,
) -> int | None:
    first = _first_rotation_date(
        rs_ratio, rs_mom, full_dates,
        entry_date=entry_date, exit_date=exit_date, stock_id=stock_id,
    )
    if first is None:
        return None
    return _trading_days_between(full_dates, entry_date, first)


def _summarize_block_days(days: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in days:
        r = str(row.get("block_reason") or "unknown")
        counts[r] = counts.get(r, 0) + 1
    return counts


def run_c18acc_swap_block_audit(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-30",
    config: ScoreSwapCConfig | None = None,
) -> dict[str, Any]:
    """Run champion close backtest · audit swap blocks on rotation-loss legs."""
    from market_breadth_ma import build_breadth_panel

    cfg = config or close_champion_score_swap_c_config()
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    swap_block_audit: dict[str, list[dict[str, Any]]] = {}

    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=cfg,
        kbar_cache={},
        rs_mom=rs_mom,
        rs_ratio=rs_ratio,
        entry_c_config=c0_cfg,
        swap_block_audit=swap_block_audit,
    )

    bad_legs = [
        p
        for p in periods
        if p.get("exit_reason") == "max_hold" and float(p.get("return_pct") or 0) <= -10.0
    ]
    rotation_cohort = []
    for p in periods:
        dtr = _days_to_rotation(
            rs_ratio,
            rs_mom,
            full_dates,
            entry_date=str(p["entry_date"]),
            exit_date=str(p["exit_date"]),
            stock_id=str(p["stock_id"]),
        )
        if dtr is not None and dtr <= 5 and float(p.get("return_pct") or 0) <= -10.0:
            rotation_cohort.append(p)

    leg_audits: list[dict[str, Any]] = []
    for leg in bad_legs:
        sid = str(leg["stock_id"])
        entry = str(leg["entry_date"])
        key = _leg_audit_key(sid, entry)
        days = swap_block_audit.get(key, [])
        first_rot = _first_rotation_date(
            rs_ratio, rs_mom, full_dates,
            entry_date=entry, exit_date=str(leg["exit_date"]), stock_id=sid,
        )
        rot_days = [d for d in days if first_rot and d["date"] >= first_rot]
        leg_audits.append(
            {
                "stock_id": sid,
                "stock_name": leg.get("stock_name", ""),
                "entry_date": entry,
                "exit_date": leg["exit_date"],
                "return_pct": leg.get("return_pct"),
                "excess_pct": leg.get("excess_pct"),
                "first_rotation_date": first_rot,
                "days_to_rotation": _days_to_rotation(
                    rs_ratio, rs_mom, full_dates,
                    entry_date=entry, exit_date=str(leg["exit_date"]), stock_id=sid,
                ),
                "block_reason_counts_from_rotation": _summarize_block_days(rot_days),
                "daily_from_rotation": rot_days,
                "is_watch_stock": sid in WATCH_STOCKS,
            }
        )

    watch_audits = [a for a in leg_audits if a["is_watch_stock"]]
    rot_stats = {
        "n": len(rotation_cohort),
        "mean_return_pct": round(
            sum(float(p.get("return_pct") or 0) for p in rotation_cohort) / max(len(rotation_cohort), 1),
            4,
        ),
        "mean_excess_pct": round(
            sum(float(p.get("excess_pct") or 0) for p in rotation_cohort) / max(len(rotation_cohort), 1),
            4,
        ),
    }

    return {
        "topic": "c18acc-rotation-exit",
        "phase": "D0",
        "date_start": date_start,
        "date_end": date_end,
        "config_variant": cfg.variant_id,
        "summary": summary,
        "bad_leg_criteria": "exit_reason=max_hold AND return_pct<=-10%",
        "n_bad_legs": len(bad_legs),
        "n_rotation_cohort": rot_stats["n"],
        "rotation_cohort_stats": rot_stats,
        "watch_stocks": list(WATCH_STOCKS),
        "watch_leg_audits": watch_audits,
        "bad_leg_audits": leg_audits,
    }


def render_c18acc_swap_block_audit_md(payload: dict[str, Any]) -> str:
    lines = [
        "# C18acc · Phase D0 Swap Block Audit",
        "",
        f"區間：{payload.get('date_start')} .. {payload.get('date_end')}",
        f"Config：{payload.get('config_variant')}",
        "",
        f"Bad legs（max_hold · ret≤-10%）：**{payload.get('n_bad_legs')}**",
        f"Rotation cohort（weakening/lagging ≤5d · ret≤-10%）：**{payload.get('n_rotation_cohort')}**",
        "",
        "## Watch stocks",
        "",
    ]
    for row in payload.get("watch_leg_audits") or []:
        lines.append(
            f"- **{row.get('stock_id')}** · entry {row.get('entry_date')} · "
            f"ret {row.get('return_pct')}% · first rotation {row.get('first_rotation_date')}"
        )
        counts = row.get("block_reason_counts_from_rotation") or {}
        if counts:
            lines.append(f"  - blocks: {counts}")
    lines += ["", "## Bad leg block reason totals", ""]
    totals: dict[str, int] = {}
    for row in payload.get("bad_leg_audits") or []:
        for k, v in (row.get("block_reason_counts_from_rotation") or {}).items():
            totals[k] = totals.get(k, 0) + int(v)
    for k, v in sorted(totals.items(), key=lambda x: -x[1]):
        lines.append(f"- {k}: {v} leg-days")
    lines += [
        "",
        "---",
        "模組：`scripts/run_c18acc_swap_block_audit.py`",
        "",
    ]
    return "\n".join(lines)
