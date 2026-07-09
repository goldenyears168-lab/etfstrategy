"""Copytrade multi-leg ML allocation overlay · Phase 4 (Research layer).

P4-1: On multi-leg signal days only, allocate capital proportional to mom+fund
composite rank (PIT features at signal_date). Single-leg days unchanged.

P4-2: Optional RRG fresh-mono intersection — ML weighting applies only among legs
that are RRG fresh mono at signal_date; non-intersect legs get minimum weight.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from copytrade.signals import CopytradeSignal, group_signals_by_date, iter_copytrade_signals
from rank_stats import spearman_correlation
from research.backtest.copytrade_backtest import (
    ALLOCATION_EQUAL,
    _build_day_results,
    _matrix_col_key,
    _matrix_row_key,
    _paired_action_diffs,
    _paired_significance,
    _summarize_day_run,
    load_stock_beta_map,
    primary_alpha_improved,
    primary_alpha_ntd,
)
from research.backtest.archive.tw100_fundamental_features import (
    FUNDAMENTAL_FEATURE_NAMES,
    build_fundamental_feature_panels,
)
from research.backtest.archive.tw100_ml_backtest import load_close_panel
from research.backtest.archive.tw100_monthly_wfa import (
    MOM_FEATURE_NAMES,
    MOM_WINDOWS,
    resolve_feature_names,
)

DEFAULT_ETF_CODE = "00981A"
DEFAULT_STRATEGY_ID = "L1H9"
DEFAULT_WINDOW_START = "2015-01-01"
OOS_HOLDOUT_RATIO = 0.2


@dataclass(frozen=True)
class CopytradeLegMlOverlayConfig:
    etf_code: str = DEFAULT_ETF_CODE
    strategy_id: str = DEFAULT_STRATEGY_ID
    feature_set: str = "mom_fund"
    overlay_mode: str = "all_multi_leg"  # all_multi_leg | rrg_intersect
    entry_lag_days: int = 1
    hold_trading_days: int = 9
    entry_price_mode: str = "open"
    capital_ntd: float = 10_000.0
    cost_bps: float = 0.0
    window_start: str | None = DEFAULT_WINDOW_START
    window_end: str | None = None
    oos_holdout_ratio: float = OOS_HOLDOUT_RATIO
    feature_names: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.feature_names:
            object.__setattr__(
                self, "feature_names", tuple(resolve_feature_names(self.feature_set))
            )


def _resolve_strategy_params(cfg: CopytradeLegMlOverlayConfig) -> tuple[int, int]:
    col = _matrix_col_key(cfg.strategy_id)
    row = _matrix_row_key(cfg.strategy_id)
    hold = col if col is not None else cfg.hold_trading_days
    lag = cfg.entry_lag_days
    if row == "L1":
        lag = 0
    elif row == "L2":
        lag = 1
    elif row == "L3":
        lag = 2
    return lag, hold


def build_momentum_panels(
    close_panel: pd.DataFrame,
    *,
    windows: tuple[int, ...] = MOM_WINDOWS,
) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for w in windows:
        out[f"mom_{w}d"] = close_panel / close_panel.shift(w) - 1.0
    return out


def build_rrg_fresh_mono_lookup(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    signal_dates: list[str],
) -> set[tuple[str, str]]:
    """PIT fresh mono tier2 at each copytrade signal_date."""
    from market_benchmark import load_benchmark_close
    from research.backtest.finpilot_local_backtest import load_price_panels
    from rrg_mono_daily_brief import LOOKBACK, _fresh_mono
    from rrg_rotation import compute_rrg_panel

    if not stock_ids or not signal_dates:
        return set()

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench)
    full_dates = close.index.astype(str).tolist()

    out: set[tuple[str, str]] = set()
    for sd in sorted(set(signal_dates)):
        prior = [d for d in full_dates if d <= sd]
        if not prior:
            continue
        d = prior[-1]
        si = full_dates.index(d)
        if si < LOOKBACK:
            continue
        for sid in stock_ids:
            if sid not in close.columns:
                continue
            if _fresh_mono(rs_ratio, rs_mom, full_dates, si, sid):
                out.add((sd, sid))
    return out


def _rank_within(values: list[float | None]) -> list[float | None]:
    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(valid) < 2:
        return [None] * len(values)
    ranked = sorted(valid, key=lambda x: x[1], reverse=True)
    out: list[float | None] = [None] * len(values)
    for rank, (idx, _) in enumerate(ranked, start=1):
        out[idx] = float(rank)
    return out


def composite_leg_scores(
    legs: list[CopytradeSignal],
    signal_date: str,
    mom_panels: dict[str, pd.DataFrame],
    fund_panels: dict[str, pd.DataFrame],
    feature_names: tuple[str, ...],
) -> list[float | None]:
    raw: list[list[float | None]] = []
    for leg in legs:
        row: list[float | None] = []
        for name in feature_names:
            panel = mom_panels.get(name)
            if panel is None:
                panel = fund_panels.get(name)
            if panel is None or signal_date not in panel.index or leg.stock_id not in panel.columns:
                row.append(None)
                continue
            val = panel.at[signal_date, leg.stock_id]
            row.append(None if val is None or pd.isna(val) else float(val))
        raw.append(row)

    n = len(legs)
    avg_ranks: list[float | None] = []
    for i in range(n):
        ranks: list[float] = []
        for j, _fname in enumerate(feature_names):
            col_vals = [raw[k][j] for k in range(n)]
            day_ranks = _rank_within(col_vals)
            if day_ranks[i] is not None:
                ranks.append(day_ranks[i])
        avg_ranks.append(sum(ranks) / len(ranks) if ranks else None)
    return avg_ranks


def multipliers_from_scores(
    scores: list[float | None],
    *,
    active_indices: list[int] | None = None,
    min_relative: float = 0.25,
) -> list[float] | None:
    """Convert within-day scores to leg multipliers; None → equal-weight fallback."""
    n = len(scores)
    if n < 2:
        return None
    idxs = active_indices if active_indices is not None else list(range(n))
    if len(idxs) < 2:
        return None
    active_scores = [scores[i] for i in idxs]
    if any(s is None for s in active_scores):
        return None

    ranked = sorted((scores[i], i) for i in idxs)
    lo = min(s for s, _ in ranked if s is not None)
    mults = [min_relative] * n
    for s, i in ranked:
        if s is None:
            return None
        mults[i] = (s - lo) + 1.0
    for i in range(n):
        if i not in idxs:
            mults[i] = min_relative
    return mults


def _leg_rank_ic(
    overlay_days: list,
    *,
    mom_panels: dict[str, pd.DataFrame],
    fund_panels: dict[str, pd.DataFrame],
    feature_names: tuple[str, ...],
) -> dict[str, float | int | None]:
    score_ranks: list[float] = []
    leg_returns: list[float] = []
    for day in overlay_days:
        if day.n_legs < 2 or day.status != "complete":
            continue
        scores = composite_leg_scores(
            [
                CopytradeSignal(
                    signal_date=day.signal_date,
                    stock_id=lg.stock_id,
                    stock_name=lg.stock_name,
                    action=lg.action,
                    share_delta=lg.share_delta,
                    weight_delta=lg.weight_delta,
                )
                for lg in day.legs
            ],
            day.signal_date,
            mom_panels,
            fund_panels,
            feature_names,
        )
        for lg, sc in zip(day.legs, scores):
            if lg.status != "complete" or sc is None:
                continue
            score_ranks.append(float(sc))
            leg_returns.append(float(lg.return_pct))
    ic = spearman_correlation(score_ranks, leg_returns) if len(score_ranks) >= 6 else None
    return {
        "n_leg_pairs": len(score_ranks),
        "leg_rank_ic": round(ic, 4) if ic is not None else None,
    }


def _split_oos_dates(dates: list[str], ratio: float) -> tuple[list[str], list[str]]:
    if not dates or ratio <= 0:
        return dates, []
    n_oos = max(1, int(len(dates) * ratio))
    if n_oos >= len(dates):
        return [], dates
    return dates[:-n_oos], dates[-n_oos:]


def _filter_days_by_dates(days: list, dates: set[str]) -> list:
    return [d for d in days if d.signal_date in dates]


def _summarize_multi_leg_paired(
    baseline_days: list,
    overlay_days: list,
) -> dict[str, Any]:
    base_multi = {
        d.signal_date: d
        for d in baseline_days
        if d.status == "complete" and d.n_legs > 1
    }
    ovl_multi = {
        d.signal_date: d
        for d in overlay_days
        if d.status == "complete" and d.n_legs > 1
    }
    dates = sorted(set(base_multi) & set(ovl_multi))
    alpha_diffs = [ovl_multi[d].alpha_ntd - base_multi[d].alpha_ntd for d in dates]
    ret_diffs = [ovl_multi[d].return_pct - base_multi[d].return_pct for d in dates]
    sig_alpha = _paired_significance(alpha_diffs)
    sig_ret = _paired_significance(ret_diffs)
    return {
        "n_multi_leg_days": len(dates),
        "dates": dates,
        "alpha_diffs": alpha_diffs,
        "mean_alpha_diff_ntd": sig_alpha.get("mean_diff"),
        "p_value_alpha_ttest": sig_alpha.get("p_value_ttest"),
        "p_value_alpha_wilcoxon": sig_alpha.get("p_value_wilcoxon"),
        "mean_return_diff_pct": sig_ret.get("mean_diff"),
        "p_value_return_wilcoxon": sig_ret.get("p_value_wilcoxon"),
    }


def run_copytrade_leg_ml_overlay(
    conn: sqlite3.Connection,
    cfg: CopytradeLegMlOverlayConfig,
) -> dict[str, Any]:
    entry_lag, hold = _resolve_strategy_params(cfg)

    signals = iter_copytrade_signals(
        conn,
        cfg.etf_code,
        window_start=cfg.window_start,
        window_end=cfg.window_end,
    )
    grouped = group_signals_by_date(signals)
    if not grouped:
        return {"error": "no copytrade signals in window"}

    stock_ids = sorted({sig.stock_id for sigs in grouped.values() for sig in sigs})
    signal_dates = sorted(grouped)
    start = cfg.window_start or signal_dates[0]
    end = cfg.window_end or signal_dates[-1]

    close = load_close_panel(conn, stock_ids, start=start, end=end)
    mom_panels = build_momentum_panels(close)
    fund_panels = build_fundamental_feature_panels(
        conn, stock_ids, signal_dates, start=start, end=end
    )

    rrg_fresh: set[tuple[str, str]] = set()
    if cfg.overlay_mode == "rrg_intersect":
        rrg_fresh = build_rrg_fresh_mono_lookup(conn, stock_ids, signal_dates)

    beta_map, _ = load_stock_beta_map(conn)
    build_kw = dict(
        capital_ntd=cfg.capital_ntd,
        entry_lag_days=entry_lag,
        hold_trading_days=hold,
        cost_bps=cfg.cost_bps,
        entry_price_mode=cfg.entry_price_mode,
        beta_map=beta_map,
        allocation_mode=ALLOCATION_EQUAL,
    )

    baseline_days = _build_day_results(conn, grouped, **build_kw)

    def make_multiplier_fn(
        mom: dict[str, pd.DataFrame],
        fund: dict[str, pd.DataFrame],
        names: tuple[str, ...],
        mode: str,
        fresh: set[tuple[str, str]],
    ) -> Callable[[CopytradeSignal], float]:
        cache: dict[str, list[float] | None] = {}

        def _multipliers_for_date(signal_date: str, legs: list[CopytradeSignal]) -> list[float] | None:
            if signal_date in cache:
                return cache[signal_date]
            if len(legs) < 2:
                cache[signal_date] = None
                return None
            scores = composite_leg_scores(legs, signal_date, mom, fund, names)
            active: list[int] | None = None
            if mode == "rrg_intersect":
                active = [
                    i
                    for i, leg in enumerate(legs)
                    if (signal_date, leg.stock_id) in fresh
                ]
            mults = multipliers_from_scores(scores, active_indices=active)
            cache[signal_date] = mults
            return mults

        per_date_mults: dict[str, dict[str, float]] = {}

        for sd, legs in grouped.items():
            mults = _multipliers_for_date(sd, legs)
            if mults is None:
                per_date_mults[sd] = {leg.stock_id: 1.0 for leg in legs}
            else:
                per_date_mults[sd] = {
                    leg.stock_id: mult for leg, mult in zip(legs, mults)
                }

        def fn(sig: CopytradeSignal) -> float:
            return per_date_mults.get(sig.signal_date, {}).get(sig.stock_id, 1.0)

        return fn

    mult_fn = make_multiplier_fn(
        mom_panels, fund_panels, cfg.feature_names, cfg.overlay_mode, rrg_fresh
    )

    overlay_days = _build_day_results(
        conn,
        grouped,
        leg_multiplier_fn=mult_fn,
        **build_kw,
    )

    base_sum = _summarize_day_run(baseline_days, conn)
    ovl_sum = _summarize_day_run(overlay_days, conn)
    paired_all = _paired_action_diffs(baseline_days, overlay_days)
    paired_multi = _summarize_multi_leg_paired(baseline_days, overlay_days)
    leg_ic = _leg_rank_ic(
        baseline_days,
        mom_panels=mom_panels,
        fund_panels=fund_panels,
        feature_names=cfg.feature_names,
    )

    complete_dates = sorted(
        d.signal_date for d in baseline_days if d.status == "complete"
    )
    is_dates, oos_dates = _split_oos_dates(complete_dates, cfg.oos_holdout_ratio)
    is_set, oos_set = set(is_dates), set(oos_dates)

    is_base = _summarize_day_run(_filter_days_by_dates(baseline_days, is_set), conn)
    is_ovl = _summarize_day_run(_filter_days_by_dates(overlay_days, is_set), conn)
    oos_base = _summarize_day_run(_filter_days_by_dates(baseline_days, oos_set), conn)
    oos_ovl = _summarize_day_run(_filter_days_by_dates(overlay_days, oos_set), conn)

    n_reweighted = sum(
        1
        for sd, legs in grouped.items()
        if len(legs) > 1
        and (
            cfg.overlay_mode != "rrg_intersect"
            or sum(1 for leg in legs if (sd, leg.stock_id) in rrg_fresh) >= 2
        )
    )

    alpha_improved = primary_alpha_improved(ovl_sum, base_sum)
    hypothesis_id = "H-TW100-7" if cfg.overlay_mode == "rrg_intersect" else "H-TW100-6"
    hypothesis_pass = (
        alpha_improved
        and (paired_multi.get("mean_alpha_diff_ntd") or 0) > 0
        and (paired_multi.get("n_multi_leg_days") or 0) >= 5
    )

    return {
        "artifact_type": "copytrade_leg_ml_overlay",
        "generated_at": date.today().isoformat(),
        "etf_code": cfg.etf_code,
        "strategy_id": cfg.strategy_id,
        "feature_set": cfg.feature_set,
        "feature_names": list(cfg.feature_names),
        "overlay_mode": cfg.overlay_mode,
        "window": {"start": start, "end": end},
        "params": {
            "entry_lag_days": entry_lag,
            "hold_trading_days": hold,
            "capital_ntd": cfg.capital_ntd,
            "oos_holdout_ratio": cfg.oos_holdout_ratio,
        },
        "baseline": base_sum,
        "overlay": ovl_sum,
        "primary_alpha_improved": alpha_improved,
        "alpha_delta_ntd": (ovl_sum.get("total_alpha_ntd") or 0)
        - (base_sum.get("total_alpha_ntd") or 0),
        "paired_all_days": {
            "n_days": len(paired_all.get("dates") or []),
            "alpha_sig": _paired_significance(paired_all.get("alpha_ntd") or []),
        },
        "paired_multi_leg_only": paired_multi,
        "leg_rank_validation": leg_ic,
        "oos_split": {
            "is_dates": [is_dates[0], is_dates[-1]] if is_dates else None,
            "oos_dates": [oos_dates[0], oos_dates[-1]] if oos_dates else None,
            "is_baseline_alpha": is_base.get("total_alpha_ntd"),
            "is_overlay_alpha": is_ovl.get("total_alpha_ntd"),
            "oos_baseline_alpha": oos_base.get("total_alpha_ntd"),
            "oos_overlay_alpha": oos_ovl.get("total_alpha_ntd"),
            "oos_alpha_improved": primary_alpha_improved(oos_ovl, oos_base),
        },
        "coverage": {
            "n_signal_days": len(grouped),
            "n_stocks": len(stock_ids),
            "n_multi_leg_reweighted": n_reweighted,
            "n_rrg_fresh_pairs": len(rrg_fresh) if cfg.overlay_mode == "rrg_intersect" else None,
        },
        "hypothesis": {
            "id": hypothesis_id,
            "pass": hypothesis_pass,
            "criteria": "overlay total_alpha > baseline · multi-leg mean alpha diff > 0 · n_multi >= 5",
        },
    }


def write_overlay_artifact(payload: dict[str, Any], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def format_overlay_markdown(payload: dict[str, Any]) -> str:
    base = payload.get("baseline") or {}
    ovl = payload.get("overlay") or {}
    paired = payload.get("paired_multi_leg_only") or {}
    oos = payload.get("oos_split") or {}
    hyp = payload.get("hypothesis") or {}
    leg_ic = payload.get("leg_rank_validation") or {}

    pass_label = "PASS" if hyp.get("pass") else "FAIL"
    hyp_id = hyp.get("id", "H-TW100-6")
    p_w = paired.get("p_value_alpha_wilcoxon")

    lines = [
        f"# Copytrade leg ML overlay · Phase 4 · {payload.get('generated_at')}",
        "",
        f"- ETF: **{payload.get('etf_code')}** · strategy: **{payload.get('strategy_id')}**",
        f"- Feature set: `{payload.get('feature_set')}` · overlay: `{payload.get('overlay_mode')}`",
        f"- Window: {payload.get('window', {}).get('start')} → {payload.get('window', {}).get('end')}",
        "",
        "## Primary α (unconstrained cumulative)",
        "",
        f"| Variant | total_alpha_ntd | multi-leg days |",
        f"|---------|-----------------|----------------|",
        f"| Equal-weight baseline | {base.get('total_alpha_ntd'):+,.0f} | {base.get('n_multi_leg_days')} |",
        f"| ML-weighted overlay | {ovl.get('total_alpha_ntd'):+,.0f} | {ovl.get('n_multi_leg_days')} |",
        f"| Δα | {payload.get('alpha_delta_ntd'):+,.0f} | |",
        "",
        "## Multi-leg paired (overlay − baseline)",
        "",
        f"- Days: **{paired.get('n_multi_leg_days')}** · mean Δα: **{paired.get('mean_alpha_diff_ntd')}** NTD",
        f"- Wilcoxon p: **{p_w}**",
        "",
        "## Leg-level rank validation",
        "",
        f"- Spearman(score, H{payload.get('params', {}).get('hold_trading_days')} leg return): "
        f"**{leg_ic.get('leg_rank_ic')}** (n={leg_ic.get('n_leg_pairs')})",
        "",
        "## OOS holdout (last {int((payload.get('params') or {}).get('oos_holdout_ratio', 0.2) * 100)}%)",
        "",
        f"- IS baseline α: {oos.get('is_baseline_alpha')} · overlay α: {oos.get('is_overlay_alpha')}",
        f"- OOS baseline α: {oos.get('oos_baseline_alpha')} · overlay α: {oos.get('oos_overlay_alpha')}",
        f"- OOS improved: **{oos.get('oos_alpha_improved')}**",
        "",
        f"## {hyp_id}: **{pass_label}**",
        "",
        f"Criteria: {hyp.get('criteria')}",
        "",
        "> No global skip · multi-leg allocation only · PIT mom+fund features.",
    ]
    return "\n".join(lines)
