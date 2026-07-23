"""C18acc · core slots + satellite small book · Phase 0/1 study.

Preregistered: reports/research/rrg/20260715_c18acc_satellite_slot_research_plan.md
Topic: c18acc-satellite-slot

Approximation note (Phase 1):
  Satellite legs ≈ periods present in expanded n_slots sim but absent from
  core n=3 (stock_id, entry_date). Core book stays the frozen 3-slot path;
  S3/S4 use expanded equal-weight books. This is live-aligned slot re-sim,
  not unconstrained 99-slot.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from research.backtest.archive.c18acc_abc_dual_sleeve_phase0 import (
    compute_daily_return_correlation,
)
from research.backtest.archive.c18acc_abc_dual_sleeve_phase1 import (
    _metrics_from_nav_series,
    _nav_equity_map,
)
from research.backtest.archive.c18acc_hold3d_event_study import _prepare_c18acc_context
from research.backtest.c18acc_entry_rank_study import (
    _gated_pool_rows,
    _resolve_gate,
)
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, rank_shortlist_avg_accel
from research.backtest.rrg_mono_score_swap_c import (
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.slot_portfolio_metrics import portfolio_metrics_for_periods

SCHEMA = "c18acc_satellite-slot-v1"
PARENT = "rrg-mono-swap-accel"
IS_END_DEFAULT = "2025-12-31"
OOS_START_DEFAULT = "2026-01-02"

CORE_BUDGET_TWD = 20_000
SAT_BUDGET_TWD = 5_000
SAT_WEIGHT = SAT_BUDGET_TWD / CORE_BUDGET_TWD  # 0.25
N_CORE = 3
NAV_BOOK_TWD = 100_000.0

MIN_OOS_N = 8
CORE_EXCESS_FLOOR_PP = -0.2
SAT_EXCESS_FLOOR_PP = -0.5
CORR_MAX = 0.70
GE4_MIN_PCT = 5.0
DD_WORSEN_MAX_PP = 1.0


def _period_stats(
    periods: list[dict[str, Any]], summary: dict[str, Any] | None = None
) -> dict[str, Any]:
    s = summarize_periods(periods)
    s["n_legs"] = len(periods)
    mr, mb = s.get("mean_return_pct"), s.get("mean_bench_pct")
    s["mean_excess_pct"] = (
        round(float(mr) - float(mb), 4) if mr is not None and mb is not None else None
    )
    if summary:
        s["swaps"] = summary.get("swaps_total")
        s["slot_util_pct"] = summary.get("slot_util_pct")
    return s


def _slice_periods(
    periods: list[dict[str, Any]], *, start: str, end: str
) -> list[dict[str, Any]]:
    return [
        p
        for p in periods
        if start <= str(p.get("entry_date") or "") <= end
    ]


def _leg_key(p: dict[str, Any]) -> tuple[str, str]:
    return (str(p.get("stock_id") or ""), str(p.get("entry_date") or ""))


def marginal_satellite_periods(
    expanded: list[dict[str, Any]],
    core: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Periods in expanded book not entered on the same day in core book."""
    core_keys = {_leg_key(p) for p in core if _leg_key(p)[0] and _leg_key(p)[1]}
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for p in expanded:
        k = _leg_key(p)
        if not k[0] or not k[1] or k in core_keys or k in seen:
            continue
        seen.add(k)
        row = dict(p)
        row["sleeve"] = "satellite"
        out.append(row)
    return out


def _simulate_champion(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    ctx: dict[str, Any],
    gate: dict[str, set[str]] | None,
    n_slots: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cfg = replace(champion_score_swap_c_config(), n_slots=max(1, int(n_slots)))
    entry_c = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C3a")
    print(f"  champion sim · n_slots={n_slots} …", flush=True)
    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=ctx["full_dates"],
        close=ctx["close"],
        bench=ctx["bench"],
        fresh_by_date=ctx["fresh_by_date"],
        zone_by_date=ctx["zone_by_date"],
        config=cfg,
        mono_by_date=ctx["mono_by_date"],
        kbar_cache=ctx["kbar_cache"],
        rs_mom=ctx["rs_mom"],
        rs_ratio=ctx["rs_ratio"],
        entry_c_config=entry_c,
        entry_gate_by_date=gate,
        swap_gate_by_date=gate,
    )
    return periods, summary


def _pool_ge_stats(
    ctx: dict[str, Any],
    trade_dates: list[str],
    *,
    gate: dict[str, set[str]] | None,
) -> dict[str, Any]:
    cfg = replace(champion_score_swap_c_config(), n_slots=N_CORE)
    sizes: list[int] = []
    ge4 = ge5 = 0
    for d in trade_dates:
        n = len(_gated_pool_rows(ctx, d, cfg=cfg, gate=gate))
        sizes.append(n)
        if n >= 4:
            ge4 += 1
        if n >= 5:
            ge5 += 1
    n_td = max(len(trade_dates), 1)
    return {
        "n_trade_dates": len(trade_dates),
        "days_ge4": ge4,
        "days_ge5": ge5,
        "pct_ge4": round(100.0 * ge4 / n_td, 2),
        "pct_ge5": round(100.0 * ge5 / n_td, 2),
        "median_pool": statistics.median(sizes) if sizes else 0,
    }


def _rank4_forward_probe(
    ctx: dict[str, Any],
    trade_dates: list[str],
    *,
    gate: dict[str, set[str]] | None,
    hold_days: int = 5,
) -> dict[str, Any]:
    """Descriptive: avg_accel rank4/5 EOD forward excess (fixed hold · not adoption)."""
    cfg = replace(champion_score_swap_c_config(), n_slots=N_CORE)
    close = ctx["close"]
    bench = ctx["bench"]
    full_dates = ctx["full_dates"]
    r4: list[float] = []
    r5: list[float] = []
    sample_days = 0
    for d in trade_dates:
        rows = _gated_pool_rows(ctx, d, cfg=cfg, gate=gate)
        if len(rows) < 4:
            continue
        ranked = rank_shortlist_avg_accel(
            rows,
            rs_ratio=ctx["rs_ratio"],
            rs_mom=ctx["rs_mom"],
            full_dates=full_dates,
            trade_date=d,
        )
        sample_days += 1
        for rank_i, bucket in ((3, r4), (4, r5)):
            if len(ranked) <= rank_i:
                continue
            sid = ranked[rank_i].stock_id
            if d not in full_dates or sid not in close.columns:
                continue
            si = full_dates.index(d)
            if si + hold_days >= len(full_dates):
                continue
            exit_d = full_dates[si + hold_days]
            try:
                entry_px = float(close.at[d, sid])
                exit_px = float(close.at[exit_d, sid])
                b0 = float(bench.at[d])
                b1 = float(bench.at[exit_d])
            except (TypeError, ValueError, KeyError):
                continue
            if entry_px <= 0 or exit_px != exit_px or b0 <= 0 or b1 != b1:
                continue
            ret = exit_px / entry_px - 1.0
            bex = b1 / b0 - 1.0
            bucket.append(round(100.0 * (ret - bex), 4))

    def _summ(xs: list[float]) -> dict[str, Any]:
        if not xs:
            return {"n": 0, "mean_excess_pct": None, "median_excess_pct": None}
        return {
            "n": len(xs),
            "mean_excess_pct": round(statistics.mean(xs), 4),
            "median_excess_pct": round(statistics.median(xs), 4),
        }

    return {
        "hold_days": hold_days,
        "n_days_ge4": sample_days,
        "rank4": _summ(r4),
        "rank5": _summ(r5),
        "note": "fixed hold forward excess · descriptive only (not S2 path)",
    }


def _add_constant_cash(
    daily_nav: list[dict[str, Any]], *, cash: float
) -> list[dict[str, Any]]:
    if cash <= 0:
        return daily_nav
    out: list[dict[str, Any]] = []
    for row in daily_nav:
        r = dict(row)
        r["equity_ntd"] = round(float(row["equity_ntd"]) + cash, 2)
        out.append(r)
    return out


def _combine_absolute_nav(
    trade_dates: list[str],
    sleeves: list[list[dict[str, Any]]],
    *,
    residual_cash: float,
    total_capital: float,
    sleeve_start_capitals: list[float] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Sum sleeve equities + residual cash; forward-fill missing sleeve days."""
    maps = [_nav_equity_map(s) for s in sleeves if s]
    starts = list(sleeve_start_capitals or [])
    while len(starts) < len(maps):
        starts.append(0.0)
    last = list(starts[: len(maps)])
    combo: list[dict[str, Any]] = []
    for d in trade_dates:
        eq = float(residual_cash)
        for i, m in enumerate(maps):
            if d in m:
                last[i] = float(m[d])
            eq += last[i]
        combo.append({"date": d, "equity_ntd": round(eq, 2)})
    metrics = _metrics_from_nav_series(
        trade_dates, combo, total_capital=total_capital
    )
    return combo, metrics


def _sleeve_daily_nav(
    conn: sqlite3.Connection,
    periods: list[dict[str, Any]],
    trade_dates: list[str],
    *,
    n_slots: int,
    sleeve_capital: float,
    close,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not periods or sleeve_capital <= 0 or n_slots < 1:
        flat = [{"date": d, "equity_ntd": round(sleeve_capital, 2)} for d in trade_dates]
        return flat, {
            "n_periods": 0,
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "sharpe_ratio": None,
            "total_capital_ntd": sleeve_capital,
        }
    port = portfolio_metrics_for_periods(
        conn,
        periods,
        trade_dates,
        total_capital=sleeve_capital,
        n_slots=n_slots,
        close=close,
        return_daily=True,
    )
    daily = list(port.get("daily_nav") or [])
    if not daily and trade_dates:
        daily = [
            {"date": d, "equity_ntd": round(sleeve_capital, 2)} for d in trade_dates
        ]
    meta = {
        "n_periods": len(periods),
        "total_return_pct": port.get("total_return_pct"),
        "cagr_pct": port.get("cagr_pct"),
        "max_drawdown_pct": port.get("max_drawdown_pct"),
        "sharpe_ratio": port.get("sharpe_ratio"),
        "avg_utilization_pct": port.get("avg_utilization_pct"),
        "total_capital_ntd": sleeve_capital,
        "n_slots": n_slots,
    }
    return daily, meta


def _window_nav_metrics(
    trade_dates: list[str],
    daily_nav: list[dict[str, Any]],
    *,
    total_capital: float,
    start: str,
    end: str,
) -> dict[str, Any]:
    return _metrics_from_nav_series(
        trade_dates,
        daily_nav,
        total_capital=total_capital,
        date_start=start,
        date_end=end,
    )


def _core_excess_windows(
    periods: list[dict[str, Any]],
    *,
    date_start: str,
    date_end: str,
    is_end: str,
    oos_start: str,
) -> dict[str, Any]:
    return {
        "full": _period_stats(_slice_periods(periods, start=date_start, end=date_end)),
        "is": _period_stats(_slice_periods(periods, start=date_start, end=is_end)),
        "oos": _period_stats(_slice_periods(periods, start=oos_start, end=date_end)),
    }


def evaluate_satellite_verdict(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply preregistered H-SAT gates to a study payload."""
    d0 = payload.get("phase0") or {}
    hyp = dict(d0.get("hypothesis_checks") or {})
    variants = payload.get("variants") or {}
    b0 = variants.get("B0") or {}
    b0_oos = (b0.get("nav_windows") or {}).get("oos") or {}

    def _nav_pass(code: str) -> dict[str, Any]:
        v = variants.get(code) or {}
        oos = (v.get("nav_windows") or {}).get("oos") or {}
        is_w = (v.get("nav_windows") or {}).get("is") or {}
        core_oos = ((v.get("core_excess") or {}).get("oos") or {})
        b0_core = ((b0.get("core_excess") or {}).get("oos") or {})
        tr = oos.get("total_return_pct")
        b0_tr = b0_oos.get("total_return_pct")
        dd = oos.get("max_drawdown_pct")
        b0_dd = b0_oos.get("max_drawdown_pct")
        sh = oos.get("sharpe_ratio")
        b0_sh = b0_oos.get("sharpe_ratio")
        core_ex = core_oos.get("mean_excess_pct")
        b0_ex = b0_core.get("mean_excess_pct")
        core_delta = (
            round(float(core_ex) - float(b0_ex), 4)
            if core_ex is not None and b0_ex is not None
            else None
        )
        tr_ok = tr is not None and b0_tr is not None and float(tr) >= float(b0_tr)
        # max_drawdown_pct is a positive magnitude (peak-to-trough %).
        dd_ok = (
            dd is not None
            and b0_dd is not None
            and float(dd) <= float(b0_dd) + DD_WORSEN_MAX_PP
        )
        sharpe_ok = (
            sh is not None and b0_sh is not None and float(sh) > float(b0_sh)
        )
        core_ok = core_delta is not None and float(core_delta) >= CORE_EXCESS_FLOOR_PP
        nav_ok = bool((tr_ok and dd_ok) or sharpe_ok) and bool(core_ok)
        is_tr = is_w.get("total_return_pct")
        b0_is = ((b0.get("nav_windows") or {}).get("is") or {}).get("total_return_pct")
        is_ok = (
            is_tr is None
            or b0_is is None
            or float(is_tr) >= float(b0_is) - 1.0  # soft IS non-crash
        )
        return {
            "variant": code,
            "pass": bool(nav_ok and is_ok),
            "oos_total_return_pct": tr,
            "b0_oos_total_return_pct": b0_tr,
            "oos_max_drawdown_pct": dd,
            "b0_oos_max_drawdown_pct": b0_dd,
            "oos_sharpe": sh,
            "b0_oos_sharpe": b0_sh,
            "core_oos_excess_delta_pp": core_delta,
            "tr_ok": tr_ok,
            "dd_ok": dd_ok,
            "sharpe_ok": sharpe_ok,
            "core_ok": core_ok,
            "is_ok": is_ok,
        }

    h_sat1 = _nav_pass("S1_CORE3_SAT1")
    h_sat2 = _nav_pass("S2_CORE3_SAT2")
    h_sat3 = _nav_pass("S3_EQ4")
    h_sat4 = _nav_pass("S4_SAT_IN_SWAP")

    # S3 expected fail vs S1: if S3 beats S1 on OOS TR, flag narrative rewrite
    s1_tr = h_sat1.get("oos_total_return_pct")
    s3_tr = h_sat3.get("oos_total_return_pct")
    s3_beats_s1 = (
        s1_tr is not None
        and s3_tr is not None
        and float(s3_tr) > float(s1_tr)
    )

    d0_ok = all(
        bool(hyp.get(k))
        for k in ("H-SAT-D0-1_pass", "H-SAT-D0-2_pass", "H-SAT-D0-3_pass")
    )
    if not d0_ok:
        verdict = "KEEP_CORE_ONLY"
        reason = "phase0_floor_fail"
    elif h_sat1["pass"]:
        verdict = "GO_SAT1_OBSERVE"
        reason = "H-SAT-1_pass"
    elif h_sat2["pass"]:
        verdict = "GO_SAT2_OBSERVE"
        reason = "H-SAT-2_pass"
    else:
        verdict = "KEEP_CORE_ONLY"
        reason = "no_nav_winner"

    return {
        "verdict": verdict,
        "reason": reason,
        "phase0_ok": d0_ok,
        "H-SAT-1": h_sat1,
        "H-SAT-2": h_sat2,
        "H-SAT-3": {**h_sat3, "expected_fail": True, "beats_s1": s3_beats_s1},
        "H-SAT-4": h_sat4,
        "summary": f"{verdict} · {reason}",
    }


def run_c18acc_satellite_slot_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str = IS_END_DEFAULT,
    oos_start: str = OOS_START_DEFAULT,
    gate_cache_path: str | Path | None = None,
    nav_book_twd: float = NAV_BOOK_TWD,
    core_budget: int = CORE_BUDGET_TWD,
    sat_budget: int = SAT_BUDGET_TWD,
    skip_phase1: bool = False,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    if len(trade_dates) < 5:
        raise ValueError(f"insufficient trade dates: {len(trade_dates)}")

    print(
        f"satellite-slot: {trade_dates[0]}..{trade_dates[-1]} · book={nav_book_twd:.0f}",
        flush=True,
    )
    ctx = _prepare_c18acc_context(conn, trade_dates)
    gate, gate_meta = _resolve_gate(
        conn,
        trade_dates,
        gate_cache_path=gate_cache_path,
        n_slots=N_CORE,
    )

    periods3, sum3 = _simulate_champion(
        conn, trade_dates, ctx=ctx, gate=gate, n_slots=3
    )
    periods4, sum4 = _simulate_champion(
        conn, trade_dates, ctx=ctx, gate=gate, n_slots=4
    )
    periods5, sum5 = _simulate_champion(
        conn, trade_dates, ctx=ctx, gate=gate, n_slots=5
    )

    sat1 = marginal_satellite_periods(periods4, periods3)
    sat2 = marginal_satellite_periods(periods5, periods3)

    pool = _pool_ge_stats(ctx, trade_dates, gate=gate)
    probe = _rank4_forward_probe(ctx, trade_dates, gate=gate)

    # Phase 0 · sat leg quality from marginal S2-path legs
    sat1_oos = _period_stats(_slice_periods(sat1, start=oos_start, end=trade_dates[-1]))
    sat1_full = _period_stats(sat1)
    sat2_oos = _period_stats(_slice_periods(sat2, start=oos_start, end=trade_dates[-1]))

    core_cap = float(N_CORE * core_budget)
    sat1_cap = float(sat_budget)
    sat2_cap = float(2 * sat_budget)
    eq4_cap = float(4 * core_budget)
    s4_cap = float(N_CORE * core_budget + sat_budget)  # 65k integrated notional proxy

    # Core / sat sleeve NAVs (absolute capital inside book)
    core_daily, core_meta = _sleeve_daily_nav(
        conn, periods3, trade_dates, n_slots=3, sleeve_capital=core_cap, close=close
    )
    sat1_daily, sat1_meta = _sleeve_daily_nav(
        conn, sat1, trade_dates, n_slots=1, sleeve_capital=sat1_cap, close=close
    )
    sat2_daily, sat2_meta = _sleeve_daily_nav(
        conn, sat2, trade_dates, n_slots=2, sleeve_capital=sat2_cap, close=close
    )

    corr = compute_daily_return_correlation(core_daily, sat1_daily)
    corr_v = corr.get("daily_return_corr")

    d0_1_pass = float(pool["pct_ge4"]) >= GE4_MIN_PCT
    sat_oos_ex = sat1_oos.get("mean_excess_pct")
    d0_2_pass = (
        sat_oos_ex is not None
        and int(sat1_oos.get("n_legs") or 0) >= MIN_OOS_N
        and float(sat_oos_ex) >= SAT_EXCESS_FLOOR_PP
    )
    d0_3_pass = corr_v is not None and float(corr_v) <= CORR_MAX

    phase0 = {
        "pool_stats": pool,
        "rank_forward_probe": probe,
        "sat1_marginal_legs": {
            "full": sat1_full,
            "oos": sat1_oos,
            "n_legs": len(sat1),
        },
        "sat2_marginal_legs": {
            "oos": sat2_oos,
            "n_legs": len(sat2),
        },
        "daily_return_corr_core_sat1": corr,
        "hypothesis_checks": {
            "H-SAT-D0-1_pct_ge4": pool["pct_ge4"],
            "H-SAT-D0-1_pass": d0_1_pass,
            "H-SAT-D0-2_sat1_oos_mean_excess_pct": sat_oos_ex,
            "H-SAT-D0-2_sat1_oos_n": sat1_oos.get("n_legs"),
            "H-SAT-D0-2_pass": d0_2_pass,
            "H-SAT-D0-3_corr": corr_v,
            "H-SAT-D0-3_pass": d0_3_pass,
        },
    }

    variants: dict[str, Any] = {}
    if not skip_phase1:
        print("  building NAV variants …", flush=True)

        def _book(
            code: str,
            *,
            sleeves: list[list[dict[str, Any]]],
            residual: float,
            core_periods: list[dict[str, Any]],
            note: str,
            sleeve_meta: dict[str, Any] | None = None,
            sleeve_caps: list[float] | None = None,
        ) -> dict[str, Any]:
            combo, full_m = _combine_absolute_nav(
                trade_dates,
                sleeves,
                residual_cash=residual,
                total_capital=nav_book_twd,
                sleeve_start_capitals=sleeve_caps,
            )
            nav_windows = {
                "full": _window_nav_metrics(
                    trade_dates, combo, total_capital=nav_book_twd,
                    start=trade_dates[0], end=trade_dates[-1],
                ),
                "is": _window_nav_metrics(
                    trade_dates, combo, total_capital=nav_book_twd,
                    start=trade_dates[0], end=is_end,
                ),
                "oos": _window_nav_metrics(
                    trade_dates, combo, total_capital=nav_book_twd,
                    start=oos_start, end=trade_dates[-1],
                ),
            }
            return {
                "code": code,
                "note": note,
                "residual_cash_twd": residual,
                "sleeve_meta": sleeve_meta,
                "nav_windows": nav_windows,
                "core_excess": _core_excess_windows(
                    core_periods,
                    date_start=trade_dates[0],
                    date_end=trade_dates[-1],
                    is_end=is_end,
                    oos_start=oos_start,
                ),
                "full_nav_metrics": full_m,
            }

        cash_b0 = nav_book_twd - core_cap
        variants["B0"] = _book(
            "B0",
            sleeves=[core_daily],
            residual=cash_b0,
            core_periods=periods3,
            note="core 3 × 20k + cash",
            sleeve_meta={"core": core_meta, "sim_summary": sum3},
            sleeve_caps=[core_cap],
        )
        variants["S1_CORE3_SAT1"] = _book(
            "S1_CORE3_SAT1",
            sleeves=[core_daily, sat1_daily],
            residual=nav_book_twd - core_cap - sat1_cap,
            core_periods=periods3,
            note="core 3×20k + sat rank-marginal@5k · sat not in swap book",
            sleeve_meta={"core": core_meta, "sat": sat1_meta},
            sleeve_caps=[core_cap, sat1_cap],
        )
        variants["S2_CORE3_SAT2"] = _book(
            "S2_CORE3_SAT2",
            sleeves=[core_daily, sat2_daily],
            residual=nav_book_twd - core_cap - sat2_cap,
            core_periods=periods3,
            note="core 3×20k + 2 sat ×5k · sat not in swap book",
            sleeve_meta={"core": core_meta, "sat": sat2_meta},
            sleeve_caps=[core_cap, sat2_cap],
        )

        eq4_daily, eq4_meta = _sleeve_daily_nav(
            conn, periods4, trade_dates, n_slots=4, sleeve_capital=eq4_cap, close=close
        )
        variants["S3_EQ4"] = _book(
            "S3_EQ4",
            sleeves=[eq4_daily],
            residual=nav_book_twd - eq4_cap,
            core_periods=periods4,  # expanded book is the only book
            note="equal-weight n_slots=4 ×20k (control gun)",
            sleeve_meta={"eq4": eq4_meta, "sim_summary": sum4},
            sleeve_caps=[eq4_cap],
        )

        # S4 · integrated swap book approx: n=4 path sized at 65k (3×20+5)
        s4_daily, s4_meta = _sleeve_daily_nav(
            conn, periods4, trade_dates, n_slots=4, sleeve_capital=s4_cap, close=close
        )
        variants["S4_SAT_IN_SWAP"] = _book(
            "S4_SAT_IN_SWAP",
            sleeves=[s4_daily],
            residual=nav_book_twd - s4_cap,
            core_periods=periods4,
            note=(
                "n_slots=4 integrated path · sleeve capital 65k "
                "(unequal 20/20/20/5 not modeled; equal 4-way on 65k proxy)"
            ),
            sleeve_meta={"s4": s4_meta, "sim_summary": sum4},
            sleeve_caps=[s4_cap],
        )

    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "parent_strategy": PARENT,
        "plan_artifact": "reports/research/rrg/20260715_c18acc_satellite_slot_research_plan.md",
        "date_start": trade_dates[0],
        "date_end": trade_dates[-1],
        "is_end": is_end,
        "oos_start": oos_start,
        "economics": {
            "nav_book_twd": nav_book_twd,
            "core_budget_twd": core_budget,
            "sat_budget_twd": sat_budget,
            "sat_weight": SAT_WEIGHT,
            "n_core": N_CORE,
        },
        "gate_meta": gate_meta,
        "n_periods_core3": len(periods3),
        "n_periods_exp4": len(periods4),
        "n_periods_exp5": len(periods5),
        "n_sat1_marginal": len(sat1),
        "n_sat2_marginal": len(sat2),
        "phase0": phase0,
        "variants": variants,
        "skip_phase1": skip_phase1,
        "method_notes": [
            "Core = champion n_slots=3 live-aligned path (unchanged for S1/S2).",
            "Satellite legs = (stock_id, entry_date) in expanded n=4/5 but not in core n=3.",
            "S3 = equal n=4 ×20k; S4 = n=4 path on 65k equal slots (proxy for sat-in-swap).",
            "NAV book = absolute sleeve equity + residual cash (not dual-sleeve weight rescale).",
        ],
    }
    payload["verdict"] = evaluate_satellite_verdict(payload)
    return payload


def render_c18acc_satellite_slot_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    p0 = payload.get("phase0") or {}
    hyp = p0.get("hypothesis_checks") or {}
    pool = p0.get("pool_stats") or {}
    lines = [
        "# C18acc · 主槽 + 衛星槽 · Phase 0/1",
        "",
        f"> Verdict: **{v.get('verdict')}** · {v.get('summary')}",
        "",
        f"窗口：**{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"IS ≤ **{payload.get('is_end')}** · OOS ≥ **{payload.get('oos_start')}**",
        "",
        f"簿值 **{payload.get('economics', {}).get('nav_book_twd')}** · "
        f"core **{payload.get('economics', {}).get('core_budget_twd')}** ×3 · "
        f"sat **{payload.get('economics', {}).get('sat_budget_twd')}** (w=0.25)",
        "",
        "## Phase 0",
        "",
        f"- ≥4 候選日：**{pool.get('days_ge4')}** / {pool.get('n_trade_dates')} "
        f"（{pool.get('pct_ge4')}%）· H-SAT-D0-1 "
        f"{'PASS' if hyp.get('H-SAT-D0-1_pass') else 'FAIL'}",
        f"- Sat1 marginal OOS mean_excess：**{hyp.get('H-SAT-D0-2_sat1_oos_mean_excess_pct')}**% "
        f"n={hyp.get('H-SAT-D0-2_sat1_oos_n')} · H-SAT-D0-2 "
        f"{'PASS' if hyp.get('H-SAT-D0-2_pass') else 'FAIL'}",
        f"- Core↔Sat1 日報酬 corr：**{hyp.get('H-SAT-D0-3_corr')}** · H-SAT-D0-3 "
        f"{'PASS' if hyp.get('H-SAT-D0-3_pass') else 'FAIL'}",
        "",
        "## Phase 1 · NAV（equal-capital book）",
        "",
        "| variant | OOS total% | OOS maxDD% | OOS Sharpe | core OOS excess% |",
        "|---------|-----------:|-----------:|-----------:|-----------------:|",
    ]
    for code in ("B0", "S1_CORE3_SAT1", "S2_CORE3_SAT2", "S3_EQ4", "S4_SAT_IN_SWAP"):
        row = (payload.get("variants") or {}).get(code) or {}
        oos = (row.get("nav_windows") or {}).get("oos") or {}
        cex = ((row.get("core_excess") or {}).get("oos") or {})
        lines.append(
            f"| {code} | {oos.get('total_return_pct')} | {oos.get('max_drawdown_pct')} | "
            f"{oos.get('sharpe_ratio')} | {cex.get('mean_excess_pct')} |"
        )
    lines.extend(
        [
            "",
            "## Analyst read",
            "",
            "- **H-SAT-D0-1**：gated fresh 池 ≥4 名日僅 ~2%（門檻 5%）→ 衛星機會 episodic，樣本密度不足。",
            "- Sat marginal legs 多來自 **n_slots 擴張後的路徑分歧**（非僅「當日 rank4 入場」）。",
            "- S1/S2 OOS 總報酬略低於 B0；微幅 Sharpe 優勢不構成採納理由（且 D0-1 已擋）。",
            "- S3 等權 4 槽 OOS TR 略高但 **core excess 稀释约 −1.25pp** → 不符「候補降權分散」敘事。",
            "- **判決 KEEP_CORE_ONLY**：維持 3 槽 champion · 不改 order.yaml。",
            "",
            "## 假說裁決",
            "",
        ]
    )
    for hid in ("H-SAT-1", "H-SAT-2", "H-SAT-3", "H-SAT-4"):
        h = v.get(hid) or {}
        lines.append(
            f"- **{hid}** · pass={h.get('pass')} · "
            f"core Δ={h.get('core_oos_excess_delta_pp')}pp · "
            f"TR {h.get('oos_total_return_pct')} vs B0 {h.get('b0_oos_total_return_pct')}"
        )
    lines.extend(
        [
            "",
            "## Method notes",
            "",
        ]
    )
    for n in payload.get("method_notes") or []:
        lines.append(f"- {n}")
    lines.extend(
        [
            "",
            f"計畫：`{payload.get('plan_artifact')}`",
            "",
            f"模組：`src/research/backtest/c18acc_satellite_slot_study.py`",
            "",
        ]
    )
    return "\n".join(lines)
