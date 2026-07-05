"""POOL1 graduation showcase · S2 vs POOL1 poll_5m diff bundle for HTML."""

from __future__ import annotations

import sqlite3
from typing import Any

from research.backtest.rrg_mono_score_swap_c import (
    build_c18acc_poll5m_s2_timeline_legs,
)


def _leg_fingerprint(leg: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(leg.get("stock_id", "")),
        str(leg.get("entry_date", "")),
        str(leg.get("exit_date", "")),
        str(leg.get("exit_reason") or ""),
    )


def _pct_delta(a: Any, b: Any) -> float | None:
    if a is None or b is None:
        return None
    return round(float(b) - float(a), 4)


def build_pool1_showcase_bundle(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    n_slots: int = 3,
    capital_ntd: float = 10_000.0,
) -> dict[str, Any]:
    """Run S2-P5M vs POOL1-P5M on same window · diff legs + KPI rows."""
    s2_legs, s2_exec, _, s2_meta = build_c18acc_poll5m_s2_timeline_legs(
        conn,
        dates,
        candidate_pool="fresh",
        variant_id="S2-P5M",
        n_slots=n_slots,
        capital_ntd=capital_ntd,
    )
    p1_legs, p1_exec, _, p1_meta = build_c18acc_poll5m_s2_timeline_legs(
        conn,
        dates,
        candidate_pool="fresh_union_accel",
        variant_id="POOL1-P5M",
        n_slots=n_slots,
        capital_ntd=capital_ntd,
    )

    s2_fp = {_leg_fingerprint(lg) for lg in s2_legs}
    p1_fp = {_leg_fingerprint(lg) for lg in p1_legs}
    pool1_only: list[dict[str, Any]] = []
    s2_only: list[dict[str, Any]] = []
    tagged_p1_legs: list[dict[str, Any]] = []
    for lg in p1_legs:
        row = dict(lg)
        fp = _leg_fingerprint(row)
        row["showcase_tag"] = "pool1_only" if fp not in s2_fp else "shared"
        tagged_p1_legs.append(row)
        if row["showcase_tag"] == "pool1_only":
            pool1_only.append(row)
    for lg in s2_legs:
        fp = _leg_fingerprint(lg)
        if fp not in p1_fp:
            s2_only.append(dict(lg))

    comparison = {
        "s2_mean_excess_pct": s2_meta.get("mean_excess_pct"),
        "pool1_mean_excess_pct": p1_meta.get("mean_excess_pct"),
        "delta_excess_pp": _pct_delta(s2_meta.get("mean_excess_pct"), p1_meta.get("mean_excess_pct")),
        "s2_swaps": s2_meta.get("swaps_total"),
        "pool1_swaps": p1_meta.get("swaps_total"),
        "delta_swaps": int(p1_meta.get("swaps_total") or 0) - int(s2_meta.get("swaps_total") or 0),
        "s2_force_exits": s2_meta.get("force_exits"),
        "pool1_force_exits": p1_meta.get("force_exits"),
    }

    case_cards = []
    for lg in pool1_only:
        case_cards.append(
            {
                "stock_id": lg["stock_id"],
                "stock_name": lg.get("stock_name", ""),
                "pool_tag": lg.get("pool_tag", "—"),
                "entry_date": lg["entry_date"],
                "exit_date": lg["exit_date"],
                "entry_px": lg.get("entry_px"),
                "exit_px": lg.get("exit_px"),
                "entry_minute": lg.get("entry_minute"),
                "exit_minute": lg.get("exit_minute"),
                "exit_reason": lg.get("exit_reason") or "—",
                "return_pct": lg.get("return_pct"),
                "pool_tag_note": (
                    "fresh mono 新进"
                    if lg.get("pool_tag") == "fresh"
                    else "mono_tier2 · 4d avg accel>0 · 非 fresh"
                ),
            }
        )

    return {
        "schema": "c18acc_pool1_showcase-v1",
        "date_start": dates[0] if dates else None,
        "date_end": dates[-1] if dates else None,
        "n_trade_dates": len(dates),
        "timing_mode": "poll_5m",
        "comparison": comparison,
        "s2_meta": s2_meta,
        "pool1_meta": p1_meta,
        "s2_legs": s2_legs,
        "pool1_legs": tagged_p1_legs,
        "pool1_only_legs": pool1_only,
        "s2_only_legs": s2_only,
        "case_cards": case_cards,
    }
