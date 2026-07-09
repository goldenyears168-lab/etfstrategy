"""C18acc · POOL1 soft-edge / additive pool sweeps · poll_5m.

Baseline POOL1 requires mono tier2 ending in Leading (RV>100 ∧ MV>100).

Modes:
  · replace: soften whole gate to RV≥rv_min ∧ MV>mv_min (H-RV98-1 · rejected)
  · union_leading: keep Leading ∪ add soft band (e.g. RV>98 ∧ MV>102)
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Literal

import pandas as pd

from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
from project_config import DEFAULT_ETF_CODES
from research.backtest.c18acc_drs_extension_overlay_sweep import (
    IS_END_DEFAULT,
    MIN_OOS_H2_TRADE_DATES_DEFAULT,
    OOS_H1_END_DEFAULT,
    OOS_H2_END_FORWARD_DEFAULT,
    OOS_H2_HISTORICAL_END,
    OOS_H2_HISTORICAL_START,
    OOS_H2_START_DEFAULT,
    OOS_START_DEFAULT,
    OosH2Mode,
    _poll_champion_variant,
    build_daily_spread_rs_panel,
    resolve_oos_h2_window,
)
from research.backtest.c18acc_rotation_force_exit_sweep import (
    _cohort_stats,
    _rotation_cohort_periods,
)
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, LENGTH
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    _fresh_union_accel_pool,
    simulate_score_swap_c,
)
from research.backtest.slot_portfolio_metrics import portfolio_metrics_for_periods
from rrg_mono_daily_brief import LOOKBACK, ScanRow, _feat, _mono_tier2
from rrg_rotation import compute_rrg_panel
from stock_db import load_etf_constituent_watchlist

# Soft-edge defaults · circle projection ≈100
DEFAULT_RV_MIN = 98.0
DEFAULT_MV_MIN_EXCLUSIVE = 100.0  # keep MV > 100 (Leading Y-axis)
DEFAULT_POOL_MODE: Literal["replace", "union_leading"] = "replace"
RvCompare = Literal["ge", "gt"]
PoolMode = Literal["replace", "union_leading"]


def _rv_ok(rv: float, rv_min: float, rv_compare: RvCompare) -> bool:
    return rv > rv_min if rv_compare == "gt" else rv >= rv_min


def mono_soft_rv_gate(
    f: dict[str, Any] | None,
    *,
    rv_min: float = DEFAULT_RV_MIN,
    mv_min_exclusive: float = DEFAULT_MV_MIN_EXCLUSIVE,
    rv_compare: RvCompare = "ge",
    pool_mode: PoolMode = "replace",
) -> bool:
    """Geometry + soft RV/MV · optionally union Leading mono_tier2."""
    if pool_mode == "union_leading" and _mono_tier2(f):
        return True
    return bool(
        f
        and f["trend"] == "up_right"
        and f["mono_up"]
        and 1 <= f["disp"] < 2
        and _rv_ok(float(f["rs_ratio"]), rv_min, rv_compare)
        and float(f["rs_momentum"]) > mv_min_exclusive
    )


def fresh_soft_rv_mono(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    si: int,
    sid: str,
    *,
    lb: int = LOOKBACK,
    rv_min: float = DEFAULT_RV_MIN,
    mv_min_exclusive: float = DEFAULT_MV_MIN_EXCLUSIVE,
    rv_compare: RvCompare = "ge",
    pool_mode: PoolMode = "replace",
) -> bool:
    kw = dict(
        rv_min=rv_min,
        mv_min_exclusive=mv_min_exclusive,
        rv_compare=rv_compare,
        pool_mode=pool_mode,
    )
    f = _feat(rs_ratio, rs_mom, full_dates, si, sid, lb=lb)
    if not mono_soft_rv_gate(f, **kw):
        return False
    prev = _feat(rs_ratio, rs_mom, full_dates, si - 1, sid, lb=lb)
    return not mono_soft_rv_gate(prev, **kw)


def build_soft_rv_mono_calendars(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    close: pd.DataFrame,
    bench: pd.Series,
    rs_ratio: pd.DataFrame | None = None,
    rs_mom: pd.DataFrame | None = None,
    lookback: int = LOOKBACK,
    rrg_length: int = LENGTH,
    rv_min: float = DEFAULT_RV_MIN,
    mv_min_exclusive: float = DEFAULT_MV_MIN_EXCLUSIVE,
    rv_compare: RvCompare = "ge",
    pool_mode: PoolMode = "replace",
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> tuple[dict[str, list[ScanRow]], dict[str, list[ScanRow]]]:
    """Return (fresh_by_date, mono_by_date) under soft RV gate."""
    if rs_ratio is None or rs_mom is None:
        rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=rrg_length)
    daily_pct = close.pct_change(fill_method=None) * 100.0
    full_dates = close.index.astype(str).tolist()
    watch = load_etf_constituent_watchlist(conn, etf_codes)
    name_map = {w["stock_id"]: w.get("stock_name", "") for w in watch}
    universe = [w["stock_id"] for w in watch]
    date_set = set(trade_dates)
    lb = max(2, int(lookback))
    gate_kw = dict(
        rv_min=rv_min,
        mv_min_exclusive=mv_min_exclusive,
        rv_compare=rv_compare,
        pool_mode=pool_mode,
    )

    fresh_out: dict[str, list[ScanRow]] = {d: [] for d in trade_dates}
    mono_out: dict[str, list[ScanRow]] = {d: [] for d in trade_dates}

    for si, as_of in enumerate(full_dates):
        if as_of not in date_set or si < lb:
            continue
        fresh: list[ScanRow] = []
        mono: list[ScanRow] = []
        for sid in universe:
            f = _feat(rs_ratio, rs_mom, full_dates, si, sid, lb=lb)
            if not mono_soft_rv_gate(f, **gate_kw):
                continue
            assert f is not None
            pct = float(daily_pct.at[as_of, sid]) if sid in daily_pct.columns else None
            if pct != pct:
                pct = None
            row = ScanRow(
                stock_id=sid,
                stock_name=name_map.get(sid, ""),
                fresh=False,
                mono=True,
                seg_last=float(f["seg_last"]),
                disp=float(f["disp"]),
                segs=[float(x) for x in f["segs"]],
                quadrants=[q or "?" for q in f["quadrants"]],
                rs_ratio=float(f["rs_ratio"]),
                rs_momentum=float(f["rs_momentum"]),
                daily_pct=pct,
            )
            mono.append(row)
            if fresh_soft_rv_mono(
                rs_ratio,
                rs_mom,
                full_dates,
                si,
                sid,
                lb=lb,
                **gate_kw,
            ):
                fresh.append(
                    ScanRow(
                        stock_id=row.stock_id,
                        stock_name=row.stock_name,
                        fresh=True,
                        mono=True,
                        seg_last=row.seg_last,
                        disp=row.disp,
                        segs=list(row.segs),
                        quadrants=list(row.quadrants),
                        rs_ratio=row.rs_ratio,
                        rs_momentum=row.rs_momentum,
                        daily_pct=pct,
                    )
                )
        fresh.sort(key=lambda r: (-r.seg_last, r.stock_id))
        mono.sort(key=lambda r: (-r.seg_last, r.stock_id))
        fresh_out[as_of] = fresh
        mono_out[as_of] = mono
    return fresh_out, mono_out


def _band_label(rv_min: float, rv_compare: RvCompare) -> str:
    op = "gt" if rv_compare == "gt" else "ge"
    return f"RV{op}{rv_min:g}"


def pool_coverage_stats(
    trade_dates: list[str],
    base_fresh: dict[str, list[ScanRow]],
    base_mono: dict[str, list[ScanRow]],
    soft_fresh: dict[str, list[ScanRow]],
    soft_mono: dict[str, list[ScanRow]],
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    *,
    rv_min: float = DEFAULT_RV_MIN,
    mv_min_exclusive: float = DEFAULT_MV_MIN_EXCLUSIVE,
    rv_compare: RvCompare = "ge",
) -> dict[str, Any]:
    """Mean daily fresh∪accel sizes · soft-only incremental names."""
    base_sizes: list[int] = []
    soft_sizes: list[int] = []
    soft_only_counts: list[int] = []
    soft_only_band: list[int] = []  # additive band: not Leading · passes soft RV/MV

    for as_of in trade_dates:
        base_pool = _fresh_union_accel_pool(
            base_fresh.get(as_of, []),
            base_mono.get(as_of, []),
            rs_ratio,
            rs_mom,
            full_dates,
            as_of,
        )
        soft_pool = _fresh_union_accel_pool(
            soft_fresh.get(as_of, []),
            soft_mono.get(as_of, []),
            rs_ratio,
            rs_mom,
            full_dates,
            as_of,
        )
        base_ids = {r.stock_id for r in base_pool}
        soft_only = [r for r in soft_pool if r.stock_id not in base_ids]
        base_sizes.append(len(base_pool))
        soft_sizes.append(len(soft_pool))
        soft_only_counts.append(len(soft_only))
        soft_only_band.append(
            sum(
                1
                for r in soft_only
                if _rv_ok(float(r.rs_ratio), rv_min, rv_compare)
                and float(r.rs_momentum) > mv_min_exclusive
                and float(r.rs_ratio) <= 100.0
            )
        )

    n = len(trade_dates) or 1
    return {
        "mean_pool_size_baseline": round(sum(base_sizes) / n, 2),
        "mean_pool_size_challenger": round(sum(soft_sizes) / n, 2),
        "mean_pool_size_rv98": round(sum(soft_sizes) / n, 2),  # alias for prior reports
        "mean_soft_only": round(sum(soft_only_counts) / n, 2),
        "mean_soft_only_rv_band": round(sum(soft_only_band) / n, 2),
        "days_soft_larger": sum(1 for a, b in zip(soft_sizes, base_sizes) if a > b),
        "n_trade_dates": len(trade_dates),
    }


def pool_rv98_configs(
    *,
    challenger_id: str = "POOL1-RV98-P5M",
    challenger_label: str | None = None,
) -> list[ScoreSwapCConfig]:
    s2_exit = {
        "quad_force_exit_mode": "weakening",
        "quad_force_exit_min_days": 2,
        "quad_force_exit_loss_pct": -0.05,
    }
    label = challenger_label or (
        "poll_5m · fresh∪accel · soft RV≥98∧MV>100 · S2 · bypass"
    )
    return [
        _poll_champion_variant(
            "POOL1-P5M",
            "poll_5m · fresh∪accel · Leading (RV>100∧MV>100) · S2 · bypass",
            candidate_pool="fresh_union_accel",
            **s2_exit,
        ),
        _poll_champion_variant(
            challenger_id,
            label,
            candidate_pool="fresh_union_accel",
            **s2_exit,
        ),
    ]


def _calendar_years(date_start: str, date_end: str) -> float:
    d0 = datetime.strptime(date_start, "%Y-%m-%d")
    d1 = datetime.strptime(date_end, "%Y-%m-%d")
    return max((d1 - d0).days / 365.25, 1e-6)


def _run_variant_window(
    conn: sqlite3.Connection,
    *,
    cfg: ScoreSwapCConfig,
    date_start: str,
    date_end: str,
    label: str,
    close: pd.DataFrame,
    bench: pd.Series,
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    spread_rs_panel: pd.DataFrame,
    full_dates: list[str],
    fresh_by_date: dict[str, list[ScanRow]],
    mono_by_date: dict[str, list[ScanRow]],
    zone_by_date: dict[str, str],
    c0_cfg: ScoreSwapCConfig,
    kbar_cache: dict,
    min_trade_dates: int = 5,
) -> dict[str, Any]:
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    if len(trade_dates) < min_trade_dates:
        return {
            "label": label,
            "error": "insufficient_trade_dates",
            "variant_id": cfg.variant_id,
            "n_trade_dates": len(trade_dates),
        }

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
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        spread_rs_panel=spread_rs_panel,
        kbar_cache=kbar_cache,
        entry_c_config=c0_cfg,
    )
    leg_sum = summarize_periods(periods)
    port = portfolio_metrics_for_periods(
        conn,
        periods,
        trade_dates,
        total_capital=30_000.0,
        n_slots=cfg.n_slots,
        close=close,
    )
    rot = _cohort_stats(_rotation_cohort_periods(periods, rs_ratio, rs_mom, full_dates))

    return {
        "label": label,
        "variant_id": cfg.variant_id,
        "config_label": cfg.label,
        "date_start": date_start,
        "date_end": date_end,
        "n_trade_dates": len(trade_dates),
        "calendar_years": round(_calendar_years(date_start, date_end), 3),
        "n_periods": summary.get("n_periods"),
        "mean_excess_pct": summary.get("mean_excess_pct"),
        "mean_return_pct": leg_sum.get("mean_return_pct"),
        "win_rate_vs_bench_pct": leg_sum.get("win_rate_vs_bench_pct"),
        "swaps_total": summary.get("swaps_total"),
        "force_exits": summary.get("force_exits"),
        "mean_hold_days": summary.get("mean_hold_days"),
        "rotation_cohort": rot,
        "portfolio": {
            "total_return_pct": port.get("total_return_pct"),
            "cagr_pct": port.get("cagr_pct"),
            "sharpe_ratio": port.get("sharpe_ratio"),
            "max_drawdown_pct": port.get("max_drawdown_pct"),
        },
    }


def run_pool_rv98_sweep_backtest(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2020-01-02",
    date_end: str | None = None,
    is_end: str = IS_END_DEFAULT,
    oos_h1_start: str = OOS_START_DEFAULT,
    oos_h1_end: str = OOS_H1_END_DEFAULT,
    oos_h2_mode: OosH2Mode = "historical",
    oos_h2_start: str = OOS_H2_START_DEFAULT,
    oos_h2_end: str | None = OOS_H2_END_FORWARD_DEFAULT,
    min_oos_h2_trade_dates: int = MIN_OOS_H2_TRADE_DATES_DEFAULT,
    rv_min: float = DEFAULT_RV_MIN,
    mv_min_exclusive: float = DEFAULT_MV_MIN_EXCLUSIVE,
    rv_compare: RvCompare = "ge",
    pool_mode: PoolMode = "replace",
    challenger_id: str | None = None,
) -> dict[str, Any]:
    """POOL1 Leading vs soft / additive edge · poll_5m · IS / OOS / FULL."""
    from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
    from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar

    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    if date_end is None:
        date_end = full_dates[-1]
    if date_end > full_dates[-1]:
        date_end = full_dates[-1]

    h2_start, h2_end, h2_mode = resolve_oos_h2_window(
        oos_h2_mode=oos_h2_mode,
        oos_h2_start=oos_h2_start,
        oos_h2_end=oos_h2_end,
        date_end=date_end,
    )
    h2_trade_dates = [d for d in full_dates if h2_start <= d <= h2_end]
    n_h2_td = len(h2_trade_dates)
    h2_ready = n_h2_td >= min_oos_h2_trade_dates

    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    spread_rs_panel = build_daily_spread_rs_panel(close, bench)
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]

    rv_tag = _band_label(rv_min, rv_compare)
    rv_human = f"RV>{rv_min:g}" if rv_compare == "gt" else f"RV≥{rv_min:g}"
    if challenger_id is None:
        if pool_mode == "union_leading":
            challenger_id = f"POOL1-ADD-{rv_tag}-MV{mv_min_exclusive:g}-P5M"
        else:
            challenger_id = "POOL1-RV98-P5M"

    if pool_mode == "union_leading":
        challenger_desc = f"Leading ∪ ({rv_human} ∧ MV>{mv_min_exclusive:g})"
        challenger_label = (
            f"poll_5m · fresh∪accel · {challenger_desc} · S2 · bypass"
        )
    else:
        challenger_desc = f"soft · {rv_human} ∧ MV>{mv_min_exclusive:g}"
        challenger_label = (
            f"poll_5m · fresh∪accel · {challenger_desc} · S2 · bypass"
        )

    base_fresh = build_fresh_mono_calendar(conn, trade_dates)
    base_mono = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    soft_fresh, soft_mono = build_soft_rv_mono_calendars(
        conn,
        trade_dates,
        close=close,
        bench=bench,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        rv_min=rv_min,
        mv_min_exclusive=mv_min_exclusive,
        rv_compare=rv_compare,
        pool_mode=pool_mode,
    )
    cov = pool_coverage_stats(
        trade_dates,
        base_fresh,
        base_mono,
        soft_fresh,
        soft_mono,
        rs_ratio,
        rs_mom,
        full_dates,
        rv_min=rv_min,
        mv_min_exclusive=mv_min_exclusive,
        rv_compare=rv_compare,
    )

    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    kbar_cache: dict = {}

    windows: list[tuple[str, str, str]] = [
        ("IS", date_start, is_end),
        ("OOS_H1", oos_h1_start, min(oos_h1_end, date_end)),
        ("FULL", date_start, date_end),
    ]
    if h2_ready and h2_start <= h2_end:
        windows.insert(2, ("OOS_H2", h2_start, h2_end))

    configs = pool_rv98_configs(
        challenger_id=challenger_id,
        challenger_label=challenger_label,
    )
    calendars = {
        "POOL1-P5M": (base_fresh, base_mono),
        challenger_id: (soft_fresh, soft_mono),
    }
    rows: list[dict[str, Any]] = []
    for cfg in configs:
        fresh_map, mono_map = calendars[cfg.variant_id]
        for win_label, w_start, w_end in windows:
            if w_start > w_end:
                continue
            min_td = 2 if win_label == "OOS_H2" else 5
            rows.append(
                _run_variant_window(
                    conn,
                    cfg=cfg,
                    date_start=w_start,
                    date_end=w_end,
                    label=win_label,
                    close=close,
                    bench=bench,
                    rs_ratio=rs_ratio,
                    rs_mom=rs_mom,
                    spread_rs_panel=spread_rs_panel,
                    full_dates=full_dates,
                    fresh_by_date=fresh_map,
                    mono_by_date=mono_map,
                    zone_by_date=zone_by_date,
                    c0_cfg=c0_cfg,
                    kbar_cache=kbar_cache,
                    min_trade_dates=min_td,
                )
            )

    by_variant: dict[str, dict[str, Any]] = {}
    for r in rows:
        by_variant.setdefault(str(r.get("variant_id", "")), {})[str(r.get("label", ""))] = r

    base = by_variant.get("POOL1-P5M", {})
    soft = by_variant.get(challenger_id, {})
    base_full = base.get("FULL", {})
    soft_full = soft.get("FULL", {})
    base_h1 = base.get("OOS_H1", {})
    soft_h1 = soft.get("OOS_H1", {})
    base_h2 = base.get("OOS_H2", {})
    soft_h2 = soft.get("OOS_H2", {})

    def _delta(a: dict, b: dict, key: str) -> float | None:
        av, bv = a.get(key), b.get(key)
        if av is None or bv is None:
            return None
        return round(float(av) - float(bv), 4)

    comparison = {
        "delta_excess_full_pp": _delta(soft_full, base_full, "mean_excess_pct"),
        "delta_excess_oos_h1_pp": _delta(soft_h1, base_h1, "mean_excess_pct"),
        "delta_excess_oos_h2_pp": _delta(soft_h2, base_h2, "mean_excess_pct"),
        "delta_swaps_full": int(soft_full.get("swaps_total") or 0)
        - int(base_full.get("swaps_total") or 0),
        "delta_legs_full": int(soft_full.get("n_periods") or 0)
        - int(base_full.get("n_periods") or 0),
        "delta_cagr_full_pp": round(
            float((soft_full.get("portfolio") or {}).get("cagr_pct") or 0)
            - float((base_full.get("portfolio") or {}).get("cagr_pct") or 0),
            2,
        ),
    }

    beat_h1 = (comparison.get("delta_excess_oos_h1_pp") or 0) >= 0
    beat_h2 = (
        (comparison.get("delta_excess_oos_h2_pp") or 0) >= 0 if h2_ready and soft_h2 else None
    )
    go = bool(
        beat_h1
        and (beat_h2 is True or beat_h2 is None)
        and (comparison.get("delta_excess_full_pp") or 0) >= 0
    )

    schema = (
        "c18acc_pool_rv98_mv102_union-v1"
        if pool_mode == "union_leading"
        else "c18acc_pool_rv98_sweep-v1"
    )
    topic = (
        "c18acc-pool-rv98-mv102"
        if pool_mode == "union_leading"
        else "c18acc-pool-rv98"
    )

    return {
        "schema": schema,
        "topic": topic,
        "timing_mode": "poll_5m",
        "date_start": date_start,
        "date_end": date_end,
        "is_end": is_end,
        "oos_h1": f"{oos_h1_start}..{min(oos_h1_end, date_end)}",
        "oos_h2": f"{h2_start}..{h2_end}" if h2_start <= h2_end else None,
        "oos_h2_mode": h2_mode,
        "challenger_id": challenger_id,
        "gate": {
            "baseline": "mono_tier2 · Leading (RV>100 ∧ MV>100)",
            "challenger": challenger_desc,
            "pool_mode": pool_mode,
            "rv_min": rv_min,
            "rv_compare": rv_compare,
            "mv_min_exclusive": mv_min_exclusive,
            "unchanged": "up_right · mono_up · disp∈[1,2) · fresh∪accel · S2 · bypass · poll_5m",
            **cov,
        },
        "variants": by_variant,
        "rv98_vs_baseline": comparison,
        "verdict": {
            "rv98_beats_baseline_h1": beat_h1,
            "rv98_beats_baseline_h2": beat_h2,
            "oos_h2_evaluated": h2_ready,
            "go_soft_rv": go,
            "summary": (
                f"POOL1-P5M FULL excess={base_full.get('mean_excess_pct')}% "
                f"n={base_full.get('n_periods')} swaps={base_full.get('swaps_total')} · "
                f"{challenger_id} FULL excess={soft_full.get('mean_excess_pct')}% "
                f"n={soft_full.get('n_periods')} swaps={soft_full.get('swaps_total')} · "
                f"ΔFULL={comparison.get('delta_excess_full_pp')}pp · "
                f"ΔH1={comparison.get('delta_excess_oos_h1_pp')}pp · "
                f"ΔH2={comparison.get('delta_excess_oos_h2_pp') if h2_ready else 'pending'} · "
                f"mean pool {cov.get('mean_pool_size_baseline')}→"
                f"{cov.get('mean_pool_size_challenger')} "
                f"(+{cov.get('mean_soft_only')}/day soft-only)"
            ),
        },
    }


def render_pool_rv98_sweep_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict", {})
    cmp_ = payload.get("rv98_vs_baseline", {})
    gate = payload.get("gate", {})
    go = v.get("go_soft_rv")
    challenger_id = str(payload.get("challenger_id") or "POOL1-RV98-P5M")
    pool_mode = gate.get("pool_mode", "replace")
    title = (
        "# C18acc POOL1 · Leading ∪ (RV>98 ∧ MV>102) · poll_5m"
        if pool_mode == "union_leading"
        else "# C18acc POOL1 · RV soft-edge (≥98) · poll_5m"
    )
    hypothesis = (
        "> 假設：在 **Leading 池不變** 下，額外納入 Improving 高動能帶 "
        f"**{gate.get('challenger')}**，是否提升 excess。"
        if pool_mode == "union_leading"
        else (
            "> 假設：圓形投影以 100 最快 · Leading 的 RV>100 可放寬至 "
            f"**{gate.get('challenger')}**，\n"
            "> 納入接近邊界的 Improving（右上邊緣）是否提升 excess。"
        )
    )
    pool_size = gate.get("mean_pool_size_challenger", gate.get("mean_pool_size_rv98"))
    lines = [
        title,
        "",
        hypothesis,
        "",
        f"- Span: **{payload.get('date_start')} → {payload.get('date_end')}**",
        f"- timing: **{payload.get('timing_mode')}**（5m poll · 非 1m exit loop）",
        f"- IS ≤ {payload.get('is_end')} · OOS H1: {payload.get('oos_h1')}",
        f"- Baseline: `{gate.get('baseline')}`",
        f"- Challenger: `{gate.get('challenger')}` · mode=`{pool_mode}`",
        f"- 池擴張: mean fresh∪accel **{gate.get('mean_pool_size_baseline')} → "
        f"{pool_size}** "
        f"(soft-only **{gate.get('mean_soft_only')}/day** · "
        f"RV≤100 band **{gate.get('mean_soft_only_rv_band')}/day**)",
        "",
        "## Verdict",
        "",
        f"- {'**GO_SOFT_RV**' if go else '**NO_GO_SOFT_RV**'} · {v.get('summary', '')}",
        "",
        "## Challenger vs POOL1 baseline",
        "",
        "| window | Δ excess (pp) | Δ legs | Δ swaps |",
        "|--------|---------------|--------|---------|",
        f"| FULL | {cmp_.get('delta_excess_full_pp', '—')} | {cmp_.get('delta_legs_full', '—')} | "
        f"{cmp_.get('delta_swaps_full', '—')} |",
        f"| OOS H1 | {cmp_.get('delta_excess_oos_h1_pp', '—')} | — | — |",
        f"| OOS H2 | {cmp_.get('delta_excess_oos_h2_pp', '—' if v.get('oos_h2_evaluated') else 'pending')} | — | — |",
        f"| Δ CAGR FULL | {cmp_.get('delta_cagr_full_pp', '—')}pp | | |",
        "",
        "## By variant",
        "",
    ]
    for vid in ("POOL1-P5M", challenger_id):
        block = payload.get("variants", {}).get(vid, {})
        lines.append(f"### {vid}")
        lines.append("")
        lines.append("| window | n | mean excess | swaps | CAGR | maxDD |")
        lines.append("|--------|---|-------------|-------|------|-------|")
        for win in ("IS", "OOS_H2", "OOS_H1", "FULL"):
            r = block.get(win)
            if not r:
                continue
            if r.get("error"):
                lines.append(f"| {win} | — | {r.get('error')} | — | — | — |")
                continue
            port = r.get("portfolio") or {}
            lines.append(
                f"| {win} | {r.get('n_periods', '—')} | {r.get('mean_excess_pct', '—')}% | "
                f"{r.get('swaps_total', '—')} | {port.get('cagr_pct', '—')}% | "
                f"{port.get('max_drawdown_pct', '—')}% |"
            )
        lines.append("")
    return "\n".join(lines)
