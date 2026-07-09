"""C18acc hybrid RRG sweep · W20 RS-Ratio + WMA5 RS-Momentum vs baselines.

Compares three RRG chart specs on daily diagnostics and C18acc champion backtest
(W20 POOL1 fixed · S2 · spread_lost bypass unchanged):

- W20: symmetric JdK WMA(20)
- W5-full: symmetric WMA(5)
- W20-M5: RS-Ratio WMA(20) · RS-Momentum WMA(5) on ratio series
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import numpy as np
import pandas as pd

from market_breadth_ma import build_breadth_panel
from market_benchmark import load_benchmark_close
from project_config import DEFAULT_ETF_CODES
from research.backtest.c18acc_drs_extension_overlay_sweep import (
    IS_END_DEFAULT,
    OOS_START_DEFAULT,
    _run_variant_window,
    build_daily_spread_rs_panel,
)
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, LENGTH
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    champion_score_swap_c_config,
    close_champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from research.backtest.slot_portfolio_metrics import portfolio_metrics_for_periods
from rrg_rotation import compute_rrg_panel, quadrant_flip_rate
from rrg_universe_intraday_panel import WMA_LONG_LENGTH, WMA_SHORT_LENGTH
from stock_db import load_etf_constituent_watchlist

RrgChartSpec = Literal["W20", "W5-full", "W20-M5"]
_WEAK = frozenset({"weakening", "lagging"})
_STRONG = frozenset({"leading", "improving"})


@dataclass(frozen=True)
class HybridRrgVariant:
    spec_id: RrgChartSpec
    label: str
    ratio_length: int
    mom_length: int | None  # None → same as ratio_length


HYBRID_RRG_VARIANTS: tuple[HybridRrgVariant, ...] = (
    HybridRrgVariant("W20", "WMA(20) symmetric · JdK baseline", WMA_LONG_LENGTH, None),
    HybridRrgVariant("W5-full", "WMA(5) symmetric · full fast chart", WMA_SHORT_LENGTH, None),
    HybridRrgVariant(
        "W20-M5",
        "Hybrid · RS-Ratio WMA(20) · RS-Momentum WMA(5)",
        WMA_LONG_LENGTH,
        WMA_SHORT_LENGTH,
    ),
)


def build_rrg_panels_for_spec(
    close: pd.DataFrame,
    bench: pd.Series,
    spec: HybridRrgVariant,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    return compute_rrg_panel(
        close,
        bench,
        length=spec.ratio_length,
        mom_length=spec.mom_length,
    )


def _universe_close(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    *,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> pd.DataFrame:
    watch = load_etf_constituent_watchlist(conn, etf_codes)
    ids = [w["stock_id"] for w in watch if w["stock_id"] in close.columns]
    return close[ids] if ids else close.iloc[:, :0]


def _weakening_streak_vs_w20(
    variant_quad: pd.DataFrame,
    w20_quad: pd.DataFrame,
    trade_dates: list[str],
) -> dict[str, Any]:
    """Days where variant is weakening/lagging while W20 still leading."""
    n_w20_leading = 0
    variant_weak = 0
    for d in trade_dates:
        if d not in variant_quad.index or d not in w20_quad.index:
            continue
        for sid in variant_quad.columns:
            if sid not in w20_quad.columns:
                continue
            wq = w20_quad.at[d, sid]
            vq = variant_quad.at[d, sid]
            if pd.isna(wq) or pd.isna(vq):
                continue
            if wq != "leading":
                continue
            n_w20_leading += 1
            if vq in _WEAK:
                variant_weak += 1
    return {
        "n_w20_leading_stock_days": n_w20_leading,
        "early_weakening_pct": round(variant_weak / n_w20_leading * 100, 2) if n_w20_leading else None,
    }


def _w20_leading_to_weakening_lead(
    w20_quad: pd.DataFrame,
    variant_quad: pd.DataFrame,
    trade_dates: list[str],
) -> dict[str, Any]:
    events = 0
    prior_warn = 0
    same_day_warn = 0
    for i in range(1, len(trade_dates)):
        d_prev, d = trade_dates[i - 1], trade_dates[i]
        if d not in w20_quad.index or d_prev not in w20_quad.index:
            continue
        for sid in w20_quad.columns:
            if sid not in variant_quad.columns:
                continue
            if w20_quad.at[d_prev, sid] != "leading" or w20_quad.at[d, sid] != "weakening":
                continue
            events += 1
            v_prev = variant_quad.at[d_prev, sid] if d_prev in variant_quad.index else None
            v_now = variant_quad.at[d, sid] if d in variant_quad.index else None
            if v_prev in _WEAK:
                prior_warn += 1
            if v_now in _WEAK:
                same_day_warn += 1
    return {
        "w20_leading_to_weakening_events": events,
        "variant_weak_prior_day_pct": round(prior_warn / events * 100, 2) if events else None,
        "variant_weak_same_day_pct": round(same_day_warn / events * 100, 2) if events else None,
    }


DEFAULT_FWD_HORIZONS: tuple[int, ...] = (1, 3, 5)


def _forward_excess_panel(
    close: pd.DataFrame, bench: pd.Series, horizon: int
) -> pd.DataFrame:
    """Per stock-day forward excess return over `horizon` trade days (vs bench)."""
    fwd_asset = close.shift(-horizon) / close - 1.0
    bench_fwd = (bench.shift(-horizon) / bench - 1.0).reindex(close.index)
    return fwd_asset.sub(bench_fwd, axis=0)


def _cohort_forward_stats(
    mask: pd.DataFrame,
    fwd_by_h: dict[int, pd.DataFrame],
) -> dict[str, Any]:
    """Mean forward excess + win-rate for stock-days where mask is True."""
    out: dict[str, Any] = {}
    for h, fwd in fwd_by_h.items():
        vals = fwd.where(mask).replace([np.inf, -np.inf], np.nan).stack().dropna()
        out[f"h{h}"] = {
            "n": int(vals.shape[0]),
            "mean_excess_pct": round(float(vals.mean()) * 100, 3) if not vals.empty else None,
            "win_rate_pct": round(float((vals > 0).mean()) * 100, 2) if not vals.empty else None,
        }
    return out


def hybrid_signal_quality(
    close: pd.DataFrame,
    bench: pd.Series,
    trade_dates: list[str],
    *,
    horizons: tuple[int, ...] = DEFAULT_FWD_HORIZONS,
    confirm_within: int = 3,
) -> dict[str, Any]:
    """Forward-return quality of hybrid W20-M5 signals vs W20 symmetric.

    Two decision-relevant cohorts (evaluation-only forward returns):

    - Exit warning: W20 leading day t · does hybrid weakening predict *negative*
      forward excess? If forward excess stays positive, do NOT hard-exit.
    - Entry early: W20 not-strong (lagging/improving) day t · does hybrid
      Improving (Y>100) flag positive forward excess earlier than W20?
    """
    hybrid_spec = next(v for v in HYBRID_RRG_VARIANTS if v.spec_id == "W20-M5")
    _, _, w20_quad = build_rrg_panels_for_spec(close, bench, HYBRID_RRG_VARIANTS[0])
    _, hy_mom, hy_quad = build_rrg_panels_for_spec(close, bench, hybrid_spec)

    idx = [d for d in trade_dates if d in close.index]
    w20 = w20_quad.reindex(idx)
    hyq = hy_quad.reindex(idx)
    hym = hy_mom.reindex(idx)
    fwd_by_h = {h: _forward_excess_panel(close, bench, h).reindex(idx) for h in horizons}

    w20_leading = w20 == "leading"
    hy_weak = hyq.isin(list(_WEAK))
    hy_strong_mom = hym > 100.0  # hybrid Y-axis says accelerating

    exit_warn_mask = w20_leading & hy_weak          # W20 leading but hybrid warns weak
    exit_hold_mask = w20_leading & (~hy_weak)        # W20 leading and hybrid still strong

    # Precision proxy: of exit-warn stock-days, how often does W20 weaken within N days?
    w20_weak_future = pd.DataFrame(False, index=w20.index, columns=w20.columns)
    w20_weak_bool = w20.isin(["weakening", "lagging"])
    for k in range(1, confirm_within + 1):
        w20_weak_future = w20_weak_future | w20_weak_bool.shift(-k, fill_value=False)
    warn_n = int(exit_warn_mask.to_numpy().sum())
    confirmed = int((exit_warn_mask & w20_weak_future).to_numpy().sum())

    # Entry cohorts: W20 lagging/improving, hybrid momentum turning up
    w20_not_strong = w20.isin(["lagging", "improving"])
    entry_early_mask = w20_not_strong & hy_strong_mom
    entry_base_mask = w20_not_strong
    # Lead time: hybrid Improving today, W20 reaches leading within N days
    w20_leading_future = pd.DataFrame(False, index=w20.index, columns=w20.columns)
    w20_leading_bool = w20 == "leading"
    for k in range(1, confirm_within + 1):
        w20_leading_future = w20_leading_future | w20_leading_bool.shift(-k, fill_value=False)
    entry_n = int(entry_early_mask.to_numpy().sum())
    entry_to_leading = int((entry_early_mask & w20_leading_future).to_numpy().sum())

    return {
        "confirm_within_days": confirm_within,
        "exit_warning": {
            "note": "W20 leading day t · hybrid weakening = candidate early-exit warning",
            "n_warn_stock_days": warn_n,
            "w20_confirms_weak_within_pct": round(confirmed / warn_n * 100, 2) if warn_n else None,
            "forward_excess_when_hybrid_weak": _cohort_forward_stats(exit_warn_mask, fwd_by_h),
            "forward_excess_when_hybrid_strong": _cohort_forward_stats(exit_hold_mask, fwd_by_h),
        },
        "entry_early": {
            "note": "W20 lagging/improving day t · hybrid Y>100 = candidate early entry (提早買上漲股)",
            "n_early_stock_days": entry_n,
            "w20_reaches_leading_within_pct": round(entry_to_leading / entry_n * 100, 2) if entry_n else None,
            "forward_excess_hybrid_accel": _cohort_forward_stats(entry_early_mask, fwd_by_h),
            "forward_excess_w20_notstrong_all": _cohort_forward_stats(entry_base_mask, fwd_by_h),
        },
    }


def daily_chart_metrics(
    close: pd.DataFrame,
    bench: pd.Series,
    trade_dates: list[str],
    *,
    spec: HybridRrgVariant,
    w20_quad: pd.DataFrame | None = None,
) -> dict[str, Any]:
    rs_ratio, rs_mom, quad = build_rrg_panels_for_spec(close, bench, spec)
    flip = quadrant_flip_rate(quad, sample_dates=trade_dates)
    early = (
        _weakening_streak_vs_w20(quad, w20_quad, trade_dates)
        if w20_quad is not None and spec.spec_id != "W20"
        else None
    )
    lead = (
        _w20_leading_to_weakening_lead(w20_quad, quad, trade_dates)
        if w20_quad is not None and spec.spec_id != "W20"
        else None
    )
    n_finite = 0
    n_weakening = 0
    for d in trade_dates:
        if d not in quad.index:
            continue
        for sid in quad.columns:
            q = quad.at[d, sid]
            if pd.isna(q) or q is None:
                continue
            n_finite += 1
            if q == "weakening":
                n_weakening += 1
    return {
        "spec_id": spec.spec_id,
        "label": spec.label,
        "ratio_length": spec.ratio_length,
        "mom_length": spec.mom_length or spec.ratio_length,
        "quadrant_flip_rate_pct": flip.get("mean_flip_rate_pct"),
        "pct_weakening_stock_days": round(n_weakening / n_finite * 100, 2) if n_finite else None,
        "early_weakening_vs_w20_leading": early,
        "w20_weakening_lead_lag": lead,
    }


def _champion_config_for_spec(spec: HybridRrgVariant) -> ScoreSwapCConfig:
    base = champion_score_swap_c_config()
    fields = base.to_dict()
    fields["variant_id"] = f"C18acc-{spec.spec_id}"
    fields["label"] = f"{base.label} · RRG {spec.label}"
    return ScoreSwapCConfig(**fields)


def _verdict(
    daily_rows: list[dict[str, Any]],
    backtest_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    full = {r["spec_id"]: r for r in backtest_rows if r.get("window") == "FULL"}
    oos = {r["spec_id"]: r for r in backtest_rows if r.get("window") == "OOS"}
    w20 = full.get("W20") or {}
    hybrid = full.get("W20-M5") or {}
    w5 = full.get("W5-full") or {}
    w20_oos = oos.get("W20") or {}
    hybrid_oos = oos.get("W20-M5") or {}

    w20_ex = float(w20.get("mean_excess_pct") or 0)
    hy_ex = float(hybrid.get("mean_excess_pct") or 0)
    w5_ex = float(w5.get("mean_excess_pct") or 0)
    oos_delta = (
        round(float(hybrid_oos.get("mean_excess_pct") or 0) - float(w20_oos.get("mean_excess_pct") or 0), 4)
        if hybrid_oos and w20_oos
        else None
    )

    daily_by = {r["spec_id"]: r for r in daily_rows}
    hy_daily = daily_by.get("W20-M5") or {}
    w5_daily = daily_by.get("W5-full") or {}
    w20_daily = daily_by.get("W20") or {}

    reasons: list[str] = []
    recommend: Literal["W20-M5", "W20", "W5-full", "research_only"] = "W20"

    flip_hy = float(hy_daily.get("quadrant_flip_rate_pct") or 0)
    flip_w20 = float(w20_daily.get("quadrant_flip_rate_pct") or 0)
    flip_w5 = float(w5_daily.get("quadrant_flip_rate_pct") or 0)
    prior_hy = ((hy_daily.get("w20_weakening_lead_lag") or {}).get("variant_weak_prior_day_pct") or 0)
    prior_w5 = ((w5_daily.get("w20_weakening_lead_lag") or {}).get("variant_weak_prior_day_pct") or 0)

    if hy_ex >= w20_ex - 0.11 and oos_delta is not None and oos_delta >= -0.3:
        recommend = "W20-M5"
        reasons.append(
            f"Hybrid FULL excess {hy_ex:.2f}% vs W20 {w20_ex:.2f}% · OOS Δ {oos_delta:+.2f}pp"
        )
    elif hy_ex > w20_ex:
        recommend = "research_only"
        reasons.append(f"Hybrid FULL excess +{hy_ex - w20_ex:.2f}pp but OOS Δ {oos_delta}pp fails guard")
    else:
        reasons.append(f"Hybrid FULL excess {hy_ex:.2f}% under W20 {w20_ex:.2f}%")

    reasons.append(
        f"Flip rate: W20 {flip_w20:.1f}% · hybrid {flip_hy:.1f}% (+{flip_hy - flip_w20:.1f}pp) · "
        f"W5-full {flip_w5:.1f}%"
    )
    reasons.append(
        f"Prior-day warn on W20 L→W: hybrid {prior_hy}% · W5-full {prior_w5}%"
    )
    if w5_ex < w20_ex - 1.0:
        reasons.append(
            f"W5-full underperforms W20 by {w20_ex - w5_ex:.2f}pp (consistent with c18acc-w5-shortline)"
        )

    hy_swaps = int(hybrid.get("swaps_total") or 0)
    w20_swaps = int(w20.get("swaps_total") or 0)
    if w20_swaps:
        reasons.append(
            f"Turnover: hybrid swaps {hy_swaps} vs W20 {w20_swaps} "
            f"({hy_swaps / w20_swaps:.2f}×) · force_exits {hybrid.get('quad_force_exits')} vs {w20.get('quad_force_exits')}"
        )

    return {
        "recommend": recommend,
        "reasons": reasons,
        "summary": (
            f"Hybrid W20-M5: FULL excess {hy_ex:.2f}% vs W20 {w20_ex:.2f}% · "
            f"W5-full {w5_ex:.2f}% · recommend **{recommend}**"
        ),
        "full_excess_pp": {
            "W20": w20_ex,
            "W20-M5": hy_ex,
            "W5-full": w5_ex,
            "hybrid_minus_w20": round(hy_ex - w20_ex, 4),
        },
        "oos_delta_hybrid_minus_w20_pp": oos_delta,
    }


def _pause_ab_configs() -> list[ScoreSwapCConfig]:
    """B0 baseline vs B0 + hybrid-Y pause on quad force-exit (close timing)."""
    base = close_champion_score_swap_c_config()
    b0_fields = base.to_dict()
    b0_fields["variant_id"] = "B0"
    b0_fields["label"] = "baseline · champion close · S2 weakening 2d"
    b0 = ScoreSwapCConfig(**b0_fields)

    pause_fields = base.to_dict()
    pause_fields["variant_id"] = "B0+pauseHYmom"
    pause_fields["label"] = "B0 + pause quad force-exit while hybrid Y>100"
    pause_fields["quad_force_exit_pause_hybrid_mom_positive"] = True
    b0_pause = ScoreSwapCConfig(**pause_fields)
    return [b0, b0_pause]


def _ab_row(
    conn: sqlite3.Connection,
    *,
    cfg: ScoreSwapCConfig,
    window: str,
    date_start: str,
    date_end: str,
    close: pd.DataFrame,
    bench: pd.Series,
    full_dates: list[str],
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    hybrid_mom_panel: pd.DataFrame,
    spread_rs_panel: pd.DataFrame,
    fresh_by_date: dict,
    mono_by_date: dict,
    zone_by_date: dict,
    c0_cfg: Any,
    kbar_cache: dict,
) -> dict[str, Any]:
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        mono_by_date=mono_by_date,
        zone_by_date=zone_by_date,
        config=cfg,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        hybrid_mom_panel=hybrid_mom_panel,
        spread_rs_panel=spread_rs_panel,
        entry_c_config=c0_cfg,
        kbar_cache=kbar_cache,
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
    quad_exits = sum(1 for p in periods if p.get("exit_reason") == "quad_force_exit")
    return {
        "window": window,
        "variant_id": cfg.variant_id,
        "label": cfg.label,
        "n_periods": summary.get("n_periods"),
        "mean_excess_pct": summary.get("mean_excess_pct"),
        "mean_return_pct": leg_sum.get("mean_return_pct"),
        "win_rate_vs_bench_pct": leg_sum.get("win_rate_vs_bench_pct"),
        "swaps_total": summary.get("swaps_total"),
        "force_exits": summary.get("force_exits"),
        "quad_force_exits": quad_exits,
        "quad_force_exit_paused": summary.get("quad_force_exit_paused"),
        "mean_hold_days": summary.get("mean_hold_days"),
        "total_return_pct": port.get("total_return_pct"),
        "cagr_pct": port.get("cagr_pct"),
        "sharpe_ratio": port.get("sharpe_ratio"),
        "max_drawdown_pct": port.get("max_drawdown_pct"),
    }


def run_hybrid_y_pause_ab(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str | None = None,
    is_end: str = IS_END_DEFAULT,
    oos_start: str = OOS_START_DEFAULT,
) -> dict[str, Any]:
    """A/B: does pausing S2 quad force-exit while hybrid Y>100 capture the +0.4% drift?"""
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    if end > full_dates[-1]:
        end = full_dates[-1]

    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    hybrid_spec = next(v for v in HYBRID_RRG_VARIANTS if v.spec_id == "W20-M5")
    _, hybrid_mom_panel, _ = build_rrg_panels_for_spec(close, bench, hybrid_spec)
    spread_rs_panel = build_daily_spread_rs_panel(close, bench)

    trade_dates_bt = [d for d in full_dates if date_start <= d <= end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates_bt, rrg_length=LENGTH)
    mono_by_date = build_mono_tier2_calendar(
        conn, trade_dates_bt, close=close, bench=bench, rrg_length=LENGTH
    )
    zone_panel = build_breadth_panel(conn, date_start=date_start, date_end=end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in zone_panel.itertuples()}
    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    kbar_cache: dict = {}

    windows = [
        ("IS", date_start, is_end),
        ("OOS", oos_start, end),
        ("FULL", date_start, end),
    ]
    rows: list[dict[str, Any]] = []
    for cfg in _pause_ab_configs():
        for win_label, w_start, w_end in windows:
            if w_start > w_end:
                continue
            rows.append(
                _ab_row(
                    conn,
                    cfg=cfg,
                    window=win_label,
                    date_start=w_start,
                    date_end=w_end,
                    close=close,
                    bench=bench,
                    full_dates=full_dates,
                    rs_ratio=rs_ratio,
                    rs_mom=rs_mom,
                    hybrid_mom_panel=hybrid_mom_panel,
                    spread_rs_panel=spread_rs_panel,
                    fresh_by_date=fresh_by_date,
                    mono_by_date=mono_by_date,
                    zone_by_date=zone_by_date,
                    c0_cfg=c0_cfg,
                    kbar_cache=kbar_cache,
                )
            )

    by_key = {(r["variant_id"], r["window"]): r for r in rows}
    deltas: dict[str, Any] = {}
    for win in ("IS", "OOS", "FULL"):
        b0 = by_key.get(("B0", win))
        pz = by_key.get(("B0+pauseHYmom", win))
        if b0 and pz:
            deltas[win] = {
                "excess_pp": round(
                    float(pz.get("mean_excess_pct") or 0) - float(b0.get("mean_excess_pct") or 0), 4
                ),
                "total_return_pp": round(
                    float(pz.get("total_return_pct") or 0) - float(b0.get("total_return_pct") or 0), 4
                ),
                "quad_force_exits_delta": (pz.get("quad_force_exits") or 0)
                - (b0.get("quad_force_exits") or 0),
                "paused": pz.get("quad_force_exit_paused"),
            }

    full_delta = deltas.get("FULL") or {}
    oos_delta = deltas.get("OOS") or {}
    ex_pp = float(full_delta.get("excess_pp") or 0)
    oos_pp = float(oos_delta.get("excess_pp") or 0)
    if ex_pp >= 0.05 and oos_pp >= -0.1:
        verdict = "GO"
        summary = f"Hybrid-Y pause exit · FULL excess {ex_pp:+.2f}pp · OOS {oos_pp:+.2f}pp → GO"
    elif ex_pp > 0:
        verdict = "MARGINAL"
        summary = f"Hybrid-Y pause exit · FULL excess {ex_pp:+.2f}pp · OOS {oos_pp:+.2f}pp → marginal"
    else:
        verdict = "NO_GO"
        summary = f"Hybrid-Y pause exit · FULL excess {ex_pp:+.2f}pp · OOS {oos_pp:+.2f}pp → no-go"

    return {
        "schema": "c18acc_hybrid_y_pause_ab-v1",
        "topic": "c18acc-hybrid-rrg-mom",
        "mode": "pause_ab",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": end,
        "is_end": is_end,
        "oos_start": oos_start,
        "rows": rows,
        "deltas": deltas,
        "verdict": {"verdict": verdict, "summary": summary},
    }


def render_hybrid_y_pause_ab_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    lines = [
        "# C18acc · hybrid-Y pause exit A/B",
        "",
        f"> {v.get('summary', '')}",
        "",
        f"- {payload.get('date_start')} → {payload.get('date_end')} · "
        f"IS≤{payload.get('is_end')} · OOS≥{payload.get('oos_start')}",
        "- B0 = champion close · S2 weakening 2d · loss5%",
        "- B0+pauseHYmom = pause quad force-exit while hybrid Y (W20 ratio·WMA5) > 100",
        "",
        "| window | variant | n | mean excess% | swaps | quad_force | paused | total ret% | CAGR% | MDD% |",
        "|--------|---------|--:|-------------:|------:|-----------:|-------:|-----------:|------:|-----:|",
    ]
    for row in payload.get("rows") or []:
        lines.append(
            f"| {row.get('window')} | {row.get('variant_id')} | {row.get('n_periods')} | "
            f"{row.get('mean_excess_pct')} | {row.get('swaps_total')} | "
            f"{row.get('quad_force_exits')} | {row.get('quad_force_exit_paused')} | "
            f"{row.get('total_return_pct')} | {row.get('cagr_pct')} | {row.get('max_drawdown_pct')} |"
        )

    lines.extend(["", "## Δ (pause − B0)", "", "| window | excess pp | total ret pp | Δ quad_force |", "|--------|----------:|-------------:|-------------:|"])
    for win in ("IS", "OOS", "FULL"):
        d = (payload.get("deltas") or {}).get(win) or {}
        lines.append(
            f"| {win} | {d.get('excess_pp')} | {d.get('total_return_pp')} | {d.get('quad_force_exits_delta')} |"
        )
    lines.extend(["", "---", "模組：`scripts/run_c18acc_hybrid_rrg_mom_sweep.py --pause-ab`"])
    return "\n".join(lines) + "\n"


def run_c18acc_hybrid_rrg_mom_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str | None = None,
    is_end: str = IS_END_DEFAULT,
    oos_start: str = OOS_START_DEFAULT,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    quality_only: bool = False,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    uni = _universe_close(conn, close, etf_codes=etf_codes)
    full_dates = uni.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    if end > full_dates[-1]:
        end = full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]

    _, _, w20_quad = build_rrg_panels_for_spec(uni, bench, HYBRID_RRG_VARIANTS[0])
    daily_rows = [
        daily_chart_metrics(
            uni,
            bench,
            trade_dates,
            spec=spec,
            w20_quad=w20_quad if spec.spec_id != "W20" else None,
        )
        for spec in HYBRID_RRG_VARIANTS
    ]

    signal_quality = hybrid_signal_quality(uni, bench, trade_dates)

    if quality_only:
        return {
            "schema": "c18acc_hybrid_rrg_mom_sweep-v1",
            "topic": "c18acc-hybrid-rrg-mom",
            "mode": "quality_only",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "date_start": date_start,
            "date_end": end,
            "universe_stocks": len(uni.columns),
            "daily": daily_rows,
            "signal_quality": signal_quality,
        }

    panel_cache: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    for spec in HYBRID_RRG_VARIANTS:
        rs_ratio, rs_mom, _ = build_rrg_panels_for_spec(close, bench, spec)
        panel_cache[spec.spec_id] = (rs_ratio, rs_mom)

    spread_rs_panel = build_daily_spread_rs_panel(close, bench)
    trade_dates_bt = [d for d in full_dates if date_start <= d <= end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates_bt, rrg_length=LENGTH)
    mono_by_date = build_mono_tier2_calendar(
        conn, trade_dates_bt, close=close, bench=bench, rrg_length=LENGTH
    )
    zone_panel = build_breadth_panel(conn, date_start=date_start, date_end=end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in zone_panel.itertuples()}
    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    kbar_cache: dict = {}

    windows = [
        ("IS", date_start, is_end),
        ("OOS", oos_start, end),
        ("FULL", date_start, end),
    ]

    backtest_rows: list[dict[str, Any]] = []
    for spec in HYBRID_RRG_VARIANTS:
        cfg = _champion_config_for_spec(spec)
        rs_ratio, rs_mom = panel_cache[spec.spec_id]
        for win_label, w_start, w_end in windows:
            if w_start > w_end:
                continue
            row = _run_variant_window(
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
                fresh_by_date=fresh_by_date,
                mono_by_date=mono_by_date,
                zone_by_date=zone_by_date,
                c0_cfg=c0_cfg,
                kbar_cache=kbar_cache,
            )
            row["window"] = win_label
            row["spec_id"] = spec.spec_id
            row["rrg_chart"] = spec.label
            row["ratio_length"] = spec.ratio_length
            row["mom_length"] = spec.mom_length or spec.ratio_length
            backtest_rows.append(row)

    verdict = _verdict(daily_rows, backtest_rows)
    return {
        "schema": "c18acc_hybrid_rrg_mom_sweep-v1",
        "topic": "c18acc-hybrid-rrg-mom",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": end,
        "is_end": is_end,
        "oos_start": oos_start,
        "universe_stocks": len(uni.columns),
        "pool_fixed": "W20 fresh_union_accel · candidate_lookback=4",
        "strategy_fixed": "champion · S2 weakening 2d · spread_lost bypass",
        "variants": [spec.__dict__ for spec in HYBRID_RRG_VARIANTS],
        "daily": daily_rows,
        "signal_quality": signal_quality,
        "backtest": backtest_rows,
        "verdict": verdict,
        "prior_research": {
            "c18acc-w5-shortline": "Pure W5 pool rejected · W20 POOL1 + spread bypass adopted",
            "note": "This sweep isolates asymmetric Y-axis (W20 ratio + W5 momentum).",
        },
    }


def _render_signal_quality(payload: dict[str, Any]) -> list[str]:
    sq = payload.get("signal_quality") or {}
    if not sq:
        return []
    ew = sq.get("exit_warning") or {}
    en = sq.get("entry_early") or {}
    cw = sq.get("confirm_within_days")

    def _fwd_row(tag: str, stats: dict[str, Any]) -> str:
        cells = [tag]
        for h in (1, 3, 5):
            hs = (stats or {}).get(f"h{h}") or {}
            cells.append(f"{hs.get('mean_excess_pct')}/{hs.get('win_rate_pct')}%")
        return "| " + " | ".join(cells) + " |"

    lines = [
        "",
        "## Signal quality · forward excess (evaluation-only)",
        "",
        f"> Forward excess vs bench · mean%/win% · confirm window {cw}d",
        "",
        "### Exit warning · W20 leading day t",
        f"- Hybrid-weak stock-days: **{ew.get('n_warn_stock_days')}** · "
        f"W20 confirms weak within {cw}d: **{ew.get('w20_confirms_weak_within_pct')}%**",
        "",
        "| cohort | h1 | h3 | h5 |",
        "|--------|----|----|----|",
        _fwd_row("W20 leading · hybrid WEAK (would-exit)", ew.get("forward_excess_when_hybrid_weak")),
        _fwd_row("W20 leading · hybrid STRONG (hold)", ew.get("forward_excess_when_hybrid_strong")),
        "",
        "### Entry early · W20 lagging/improving day t （提早買上漲股）",
        f"- Hybrid-accel stock-days: **{en.get('n_early_stock_days')}** · "
        f"W20 reaches leading within {cw}d: **{en.get('w20_reaches_leading_within_pct')}%**",
        "",
        "| cohort | h1 | h3 | h5 |",
        "|--------|----|----|----|",
        _fwd_row("W20 not-strong · hybrid Y>100 (early buy)", en.get("forward_excess_hybrid_accel")),
        _fwd_row("W20 not-strong · all (baseline)", en.get("forward_excess_w20_notstrong_all")),
    ]
    return lines


def render_c18acc_hybrid_rrg_mom_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    if payload.get("mode") == "quality_only":
        lines = [
            "# C18acc hybrid RRG · signal quality（quality-only）",
            "",
            f"- Universe: **{payload.get('universe_stocks')}** stocks · "
            f"{payload.get('date_start')} → {payload.get('date_end')}",
            "",
            "## Daily chart metrics",
            "",
            "| spec | flip% | weakening% | early-weak vs W20 leading% | prior-warn L→W% |",
            "|------|------:|-----------:|----------------------------:|----------------:|",
        ]
        for row in payload.get("daily") or []:
            early = (row.get("early_weakening_vs_w20_leading") or {}).get("early_weakening_pct")
            lead = (row.get("w20_weakening_lead_lag") or {}).get("variant_weak_prior_day_pct")
            lines.append(
                f"| {row.get('spec_id')} | {row.get('quadrant_flip_rate_pct')} | "
                f"{row.get('pct_weakening_stock_days')} | {early if early is not None else '—'} | "
                f"{lead if lead is not None else '—'} |"
            )
        lines.extend(_render_signal_quality(payload))
        lines.extend(["", "---", "模組：`scripts/run_c18acc_hybrid_rrg_mom_sweep.py --quality-only`"])
        return "\n".join(lines) + "\n"

    lines = [
        "# C18acc hybrid RRG · W20 RS-Ratio + WMA5 RS-Momentum",
        "",
        f"> {v.get('summary', '')}",
        "",
        f"- Universe: **{payload.get('universe_stocks')}** stocks · "
        f"{payload.get('date_start')} → {payload.get('date_end')}",
        f"- Pool: {payload.get('pool_fixed')}",
        f"- Strategy: {payload.get('strategy_fixed')}",
        "",
        "## Daily chart metrics",
        "",
        "| spec | flip% | weakening% | early-weak vs W20 leading% | prior-warn L→W% |",
        "|------|------:|-----------:|----------------------------:|----------------:|",
    ]
    for row in payload.get("daily") or []:
        early = (row.get("early_weakening_vs_w20_leading") or {}).get("early_weakening_pct")
        lead = (row.get("w20_weakening_lead_lag") or {}).get("variant_weak_prior_day_pct")
        lines.append(
            f"| {row.get('spec_id')} | {row.get('quadrant_flip_rate_pct')} | "
            f"{row.get('pct_weakening_stock_days')} | {early if early is not None else '—'} | "
            f"{lead if lead is not None else '—'} |"
        )

    lines.extend(
        [
            "",
            "## C18acc backtest (champion · W20 pool fixed)",
            "",
            "| window | spec | n | mean excess% | swaps | quad_force | total ret% | CAGR% |",
            "|--------|------|--:|-------------:|------:|-----------:|-----------:|------:|",
        ]
    )
    for row in payload.get("backtest") or []:
        port = row.get("portfolio") or {}
        lines.append(
            f"| {row.get('window')} | {row.get('spec_id')} | {row.get('n_periods')} | "
            f"{row.get('mean_excess_pct')} | {row.get('swaps_total')} | "
            f"{row.get('quad_force_exits')} | {port.get('total_return_pct')} | "
            f"{port.get('cagr_pct')} |"
        )

    lines.extend(_render_signal_quality(payload))

    lines.extend(["", "## Verdict", "", f"- **Recommend**: {v.get('recommend')}"])
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")

    fe = v.get("full_excess_pp") or {}
    lines.extend(
        [
            "",
            "### FULL excess (pp vs W20)",
            f"- Hybrid − W20: **{fe.get('hybrid_minus_w20')}**",
            f"- OOS hybrid − W20: **{v.get('oos_delta_hybrid_minus_w20_pp')}**",
            "",
            "---",
            "模組：`scripts/run_c18acc_hybrid_rrg_mom_sweep.py`",
        ]
    )
    return "\n".join(lines) + "\n"
