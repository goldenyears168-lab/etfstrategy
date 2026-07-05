"""強勢回踩 · 3槽回測 showcase bundle（vs C18acc+S2 · pool1 同構）。"""

from __future__ import annotations

import sqlite3
from typing import Any

from research.backtest.dual_wma_signal_backtest import (
    build_lead_pullback_executed_legs_for_timeline,
    default_lead_pullback_entry,
    default_lead_pullback_exit,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_score_swap_c import build_c18acc_s2_executed_legs_for_timeline
from research.backtest.slot_portfolio_metrics import simulate_slot_portfolio


def _pct_delta(a: Any, b: Any) -> float | None:
    if a is None or b is None:
        return None
    return round(float(b) - float(a), 4)


def _mean_leg_excess(legs: list[dict[str, Any]]) -> float | None:
    vals: list[float] = []
    for lg in legs:
        ret = lg.get("return_pct")
        bench = lg.get("bench_return_pct")
        if ret is None or bench is None:
            continue
        vals.append(float(ret) - float(bench))
    if not vals:
        return None
    return round(sum(vals) / len(vals), 4)


def _periods_from_legs(legs: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "entry_date": str(lg["entry_date"]),
            "exit_date": str(lg["exit_date"]),
            "stock_id": str(lg["stock_id"]),
        }
        for lg in legs
    ]


def _portfolio_metrics(
    conn: sqlite3.Connection,
    dates: list[str],
    legs: list[dict[str, Any]],
    *,
    n_slots: int,
    capital_ntd: float,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    total = n_slots * capital_ntd
    return simulate_slot_portfolio(
        conn,
        close,
        dates,
        _periods_from_legs(legs),
        total_capital=total,
        n_slots=n_slots,
    )


def _trade_ledger(legs: list[dict[str, Any]], *, total_capital: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cum_pnl = 0.0
    for i, lg in enumerate(
        sorted(legs, key=lambda x: (x.get("entry_date", ""), x.get("stock_id", ""))),
        1,
    ):
        pnl = float(lg.get("pnl_ntd") or 0)
        cum_pnl += pnl
        ret = lg.get("return_pct")
        bench = lg.get("bench_return_pct")
        excess = (float(ret) - float(bench)) if ret is not None and bench is not None else None
        rows.append(
            {
                "idx": i,
                "stock_id": lg.get("stock_id"),
                "stock_name": lg.get("stock_name", ""),
                "spread_dist": lg.get("seg_last"),
                "entry_px": lg.get("entry_px"),
                "exit_px": lg.get("exit_px"),
                "entry_date": lg.get("entry_date"),
                "exit_date": lg.get("exit_date"),
                "entry_minute": lg.get("entry_minute"),
                "exit_minute": lg.get("exit_minute"),
                "return_pct": ret,
                "bench_return_pct": bench,
                "excess_pct": round(excess, 3) if excess is not None else None,
                "pnl_ntd": round(pnl, 2),
                "cum_return_pct": round(cum_pnl / total_capital * 100.0, 3) if total_capital else None,
                "slot_id": lg.get("slot_id"),
                "exit_reason": lg.get("exit_reason"),
            }
        )
    return rows


def _case_cards(legs: list[dict[str, Any]], *, top_n: int = 4) -> list[dict[str, Any]]:
    ranked = sorted(
        legs,
        key=lambda lg: (-(float(lg.get("return_pct") or -999)), str(lg.get("stock_id"))),
    )
    cards: list[dict[str, Any]] = []
    for lg in ranked[:top_n]:
        cards.append(
            {
                "stock_id": lg["stock_id"],
                "stock_name": lg.get("stock_name", ""),
                "spread_dist": lg.get("seg_last"),
                "entry_date": lg["entry_date"],
                "exit_date": lg["exit_date"],
                "entry_px": lg.get("entry_px"),
                "exit_px": lg.get("exit_px"),
                "return_pct": lg.get("return_pct"),
                "exit_reason": lg.get("exit_reason") or "hold_5d",
                "tag": "winner" if float(lg.get("return_pct") or 0) >= 0 else "loser",
            }
        )
    return cards


def build_lead_pullback_showcase_bundle(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    n_slots: int = 3,
    capital_ntd: float = 10_000.0,
) -> dict[str, Any]:
    """LPB 3槽 vs C18acc+S2 · 同窗 · showcase KPI + 交易 ledger。"""
    lpb_legs, lpb_exec, lpb_skip, lpb_meta = build_lead_pullback_executed_legs_for_timeline(
        conn,
        dates,
        n_slots=n_slots,
        capital_ntd=capital_ntd,
    )
    s2_legs, s2_exec, s2_skip, s2_meta = build_c18acc_s2_executed_legs_for_timeline(
        conn,
        dates,
        n_slots=n_slots,
        capital_ntd=capital_ntd,
        use_close_timing=True,
    )

    total_capital = n_slots * capital_ntd
    lpb_pf = _portfolio_metrics(conn, dates, lpb_legs, n_slots=n_slots, capital_ntd=capital_ntd)
    s2_pf = _portfolio_metrics(conn, dates, s2_legs, n_slots=n_slots, capital_ntd=capital_ntd)
    lpb_mean_ex = _mean_leg_excess(lpb_legs)
    s2_mean_ex = s2_meta.get("mean_excess_pct") or _mean_leg_excess(s2_legs)

    comparison = {
        "s2_total_return_pct": s2_pf.get("total_return_pct"),
        "lpb_total_return_pct": lpb_pf.get("total_return_pct"),
        "delta_total_return_pp": _pct_delta(s2_pf.get("total_return_pct"), lpb_pf.get("total_return_pct")),
        "s2_mean_excess_pct": s2_mean_ex,
        "lpb_mean_excess_pct": lpb_mean_ex,
        "delta_excess_pp": _pct_delta(s2_mean_ex, lpb_mean_ex),
        "s2_n_executed": s2_meta.get("n_executed"),
        "lpb_n_executed": lpb_meta.get("n_executed"),
        "lpb_n_skipped": lpb_meta.get("n_skipped"),
        "lpb_capture_pct": lpb_meta.get("signal_capture_pct"),
        "s2_cagr_pct": s2_pf.get("cagr_pct"),
        "lpb_cagr_pct": lpb_pf.get("cagr_pct"),
    }

    ep = default_lead_pullback_entry()
    ex = default_lead_pullback_exit()
    lpb_meta = dict(lpb_meta)
    lpb_meta.update(
        {
            "mean_excess_pct": lpb_mean_ex,
            "total_return_pct": lpb_pf.get("total_return_pct"),
            "cagr_pct": lpb_pf.get("cagr_pct"),
            "portfolio": lpb_pf,
        }
    )

    return {
        "schema": "lead_pullback_showcase-v1",
        "date_start": dates[0] if dates else None,
        "date_end": dates[-1] if dates else None,
        "n_trade_dates": len(dates),
        "entry_spec": {
            "signal": ep.signal,
            "pullback_min": ep.pullback_min,
            "pullback_max": ep.pullback_max,
            "require_w5_improving": ep.require_w5_improving,
        },
        "exit_spec": {"rule_id": ex.rule_id, "label": ex.label},
        "comparison": comparison,
        "s2_meta": s2_meta,
        "lpb_meta": lpb_meta,
        "s2_legs": s2_legs,
        "lpb_legs": lpb_legs,
        "lpb_executed": lpb_exec,
        "lpb_skipped": lpb_skip,
        "trade_ledger": _trade_ledger(lpb_legs, total_capital=total_capital),
        "case_cards": _case_cards(lpb_legs),
    }
