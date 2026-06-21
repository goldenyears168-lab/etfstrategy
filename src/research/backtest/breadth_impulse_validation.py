"""Breadth impulse validation · prove Zweig/Deemer incremental value vs Breadth zone alone."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from market_breadth_impulse import (
    BreadthImpulseParams,
    build_impulse_panel_from_close,
    daily_adv_decl,
    luxalgo_exposure,
    ma_zone_exposure,
    zweig_state_exposure,
)
from market_breadth_ma import build_breadth_panel, compute_ma_breadth_frame
from research.backtest.broad_momentum_tv_backtest import load_benchmark_ohlc
from research.backtest.dual_momentum_antonacci import DEFAULT_RF_ANNUAL, _compute_stats
from research.backtest.finpilot_local_backtest import load_price_panels
from report_paths import REPORTS_RESEARCH
from stock_db import PROJECT_ROOT

DEFAULT_START = "2020-01-01"

VARIANT_LABELS: dict[str, str] = {
    "buy_hold": "IX0001 Buy & Hold",
    "ma_zone_only": "Breadth zone tiers only (200MA state)",
    "zweig_state_only": "Zweig EMA tiers only (no thrust/BAM)",
    "luxalgo_full": "LuxAlgo full (Zweig thrust + Deemer BAM window)",
}


@dataclass(frozen=True)
class ValidationResult:
    params: BreadthImpulseParams
    summary: pd.DataFrame
    event_study: pd.DataFrame
    zone_conditional: pd.DataFrame
    incremental: dict[str, float | int | str]
    best_score: float


def _overlay_returns(
    exposure: pd.Series,
    bench_ret: pd.Series,
    dates: list[str],
    *,
    cash_daily: float,
) -> pd.Series:
    out: list[float] = []
    for d in dates:
        exp = float(exposure.get(d, 0.0))
        r = float(bench_ret.loc[d])
        out.append(exp * r + (1.0 - exp) * cash_daily)
    return pd.Series(out, index=dates, name="strategy_return")


def _forward_return(bench_close: pd.Series, dates: list[str], horizon: int) -> pd.Series:
    rets: list[float | None] = []
    for i, d in enumerate(dates):
        if i + horizon >= len(dates):
            rets.append(None)
            continue
        d_end = dates[i + horizon]
        c0 = float(bench_close.loc[d])
        c1 = float(bench_close.loc[d_end])
        rets.append((c1 / c0 - 1.0) * 100.0 if c0 > 0 else None)
    return pd.Series(rets, index=dates)


def _welch_ttest(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """Return (t_stat, p_value_two_sided) · scipy-free approximation."""
    if len(a) < 2 or len(b) < 2:
        return float("nan"), float("nan")
    ma, mb = float(np.mean(a)), float(np.mean(b))
    va, vb = float(np.var(a, ddof=1)), float(np.var(b, ddof=1))
    na, nb = len(a), len(b)
    se = np.sqrt(va / na + vb / nb)
    if se <= 0:
        return 0.0, 1.0
    t = (ma - mb) / se
    # Normal approx for large samples
    from math import erf, sqrt

    p = 2.0 * (1.0 - 0.5 * (1.0 + erf(abs(t) / sqrt(2.0))))
    return round(t, 3), round(p, 4)


def run_breadth_impulse_validation(
    conn: sqlite3.Connection,
    *,
    start_date: str = DEFAULT_START,
    end_date: str | None = None,
    params: BreadthImpulseParams | None = None,
    rf_annual: float = DEFAULT_RF_ANNUAL,
) -> ValidationResult:
    p = params or BreadthImpulseParams()
    stock_close, _, _ = load_price_panels(conn)
    bench_df = load_benchmark_ohlc(conn)
    bench_close = bench_df["close"]
    bench_ret = bench_close.pct_change(fill_method=None).fillna(0.0)

    all_dates = sorted(set(stock_close.index) & set(bench_close.index))
    if end_date:
        all_dates = [d for d in all_dates if d <= end_date]
    bt_dates = [d for d in all_dates if d >= start_date]
    if not bt_dates:
        raise ValueError(f"no trading days from {start_date}")
    end_date = bt_dates[-1]
    cash_daily = (1.0 + rf_annual) ** (1.0 / 252.0) - 1.0

    impulse = build_impulse_panel_from_close(stock_close, params=p)
    impulse = impulse.reindex(all_dates).ffill()

    ma_frame = compute_ma_breadth_frame(stock_close)
    ma_frame = ma_frame.set_index("trade_date").reindex(all_dates)
    pct200 = pd.to_numeric(ma_frame["pct_above_200"], errors="coerce")
    zone_exp = ma_zone_exposure(pct200)
    zweig_exp = zweig_state_exposure(impulse, p)
    full_exp = luxalgo_exposure(impulse, p)

    variants: dict[str, pd.Series] = {
        "buy_hold": pd.Series(1.0, index=all_dates),
        "ma_zone_only": zone_exp,
        "zweig_state_only": zweig_exp,
        "luxalgo_full": full_exp,
    }

    summary_rows: list[dict[str, Any]] = []
    daily_by_variant: dict[str, pd.Series] = {}
    for vid, exp in variants.items():
        strat = _overlay_returns(exp, bench_ret, bt_dates, cash_daily=cash_daily)
        bench = pd.Series([float(bench_ret.loc[d]) for d in bt_dates], index=bt_dates)
        stats = _compute_stats(strat, bench)
        stats["variant"] = VARIANT_LABELS[vid]
        stats["variant_id"] = vid
        stats["avg_exposure"] = round(float(exp.loc[bt_dates].mean()), 3)
        summary_rows.append(stats)
        daily_by_variant[vid] = strat

    summary = pd.DataFrame(summary_rows)

    # Event study · forward returns when thrust_active vs matched non-thrust days
    fwd20 = _forward_return(bench_close, all_dates, 20)
    fwd63 = _forward_return(bench_close, all_dates, 63)
    study_dates = [d for d in bt_dates if d in impulse.index]
    thrust_mask = impulse.loc[study_dates, "thrust_active"].astype(bool)
    event_starts = impulse.loc[study_dates, "zweig_thrust_today"] | impulse.loc[
        study_dates, "deemer_bam_today"
    ]

    rows_es: list[dict[str, Any]] = []
    for label, mask in [
        ("thrust_window_active", thrust_mask),
        ("thrust_or_bam_fire_day", event_starts.astype(bool)),
        ("non_thrust_same_period", ~thrust_mask),
    ]:
        idx = [d for d in study_dates if bool(mask.get(d, False))]
        r20 = fwd20.loc[idx].dropna()
        r63 = fwd63.loc[idx].dropna()
        if len(r20) == 0:
            continue
        rows_es.append(
            {
                "cohort": label,
                "days": len(idx),
                "fwd20_mean_pct": round(float(r20.mean()), 2),
                "fwd20_median_pct": round(float(r20.median()), 2),
                "fwd63_mean_pct": round(float(r63.mean()), 2),
                "fwd63_median_pct": round(float(r63.median()), 2),
                "fwd20_hit_rate_pct": round(float((r20 > 0).mean() * 100), 1),
            }
        )

    thrust_starts = [d for d in study_dates if bool(event_starts.get(d, False))]
    non_event = [d for d in study_dates if d not in thrust_starts and not bool(thrust_mask.get(d, False))]
    r20_ev = fwd20.loc[thrust_starts].dropna().to_numpy(dtype=float)
    r20_ne = fwd20.loc[non_event].dropna().to_numpy(dtype=float)
    t_stat, p_val = _welch_ttest(r20_ev, r20_ne)

    if rows_es:
        rows_es.append(
            {
                "cohort": "event_vs_non_event_ttest_fwd20",
                "days": len(thrust_starts),
                "fwd20_mean_pct": round(float(np.mean(r20_ev)), 2) if len(r20_ev) else None,
                "fwd20_median_pct": round(float(np.median(r20_ev)), 2) if len(r20_ev) else None,
                "fwd63_mean_pct": None,
                "fwd63_median_pct": None,
                "fwd20_hit_rate_pct": round(float((r20_ev > 0).mean() * 100), 1) if len(r20_ev) else None,
                "ttest_t": t_stat,
                "ttest_p": p_val,
            }
        )
    event_study = pd.DataFrame(rows_es)

    # Zone-conditional: does impulse add info within same Breadth zone?
    zone_rows: list[dict[str, Any]] = []
    for zone in ("oversold", "weak", "neutral", "strong", "overbought"):
        zdates = [
            d
            for d in study_dates
            if pd.notna(ma_frame.loc[d, "pct_above_200"])
            and classify_breadth_zone(float(ma_frame.loc[d, "pct_above_200"])) == zone  # noqa: E501
        ]
        if not zdates:
            continue
        thrust_days = [d for d in zdates if bool(thrust_mask.get(d, False))]
        calm_days = [d for d in zdates if not bool(thrust_mask.get(d, False))]
        r_th = fwd63.loc[thrust_days].dropna()
        r_ca = fwd63.loc[calm_days].dropna()
        zone_rows.append(
            {
                "breadth_zone_200": zone,
                "days": len(zdates),
                "thrust_window_days": len(thrust_days),
                "fwd63_thrust_mean_pct": round(float(r_th.mean()), 2) if len(r_th) else None,
                "fwd63_calm_mean_pct": round(float(r_ca.mean()), 2) if len(r_ca) else None,
                "fwd63_spread_pp": round(float(r_th.mean() - r_ca.mean()), 2)
                if len(r_th) and len(r_ca)
                else None,
            }
        )
    zone_conditional = pd.DataFrame(zone_rows)

    lux = summary[summary["variant_id"] == "luxalgo_full"].iloc[0]
    zone = summary[summary["variant_id"] == "ma_zone_only"].iloc[0]
    zweig = summary[summary["variant_id"] == "zweig_state_only"].iloc[0]
    incremental = {
        "luxalgo_vs_zone_excess_pp": round(
            float(lux["excess_return_pct"]) - float(zone["excess_return_pct"]), 2
        ),
        "luxalgo_vs_zweig_state_excess_pp": round(
            float(lux["excess_return_pct"]) - float(zweig["excess_return_pct"]), 2
        ),
        "luxalgo_vs_zone_sharpe_delta": round(float(lux["sharpe"]) - float(zone["sharpe"]), 2),
        "luxalgo_vs_zone_mdd_improve_pp": round(
            float(lux["max_drawdown_pct"]) - float(zone["max_drawdown_pct"]), 2
        ),
        "event_study_fwd20_spread_pp": round(float(np.mean(r20_ev) - np.mean(r20_ne)), 2)
        if len(r20_ev) and len(r20_ne)
        else None,
        "event_study_p_value": p_val,
        "thrust_fire_days": int(event_starts.sum()),
        "thrust_window_day_pct": round(float(thrust_mask.mean() * 100), 1),
    }

    score = (
        float(incremental["luxalgo_vs_zone_sharpe_delta"]) * 10.0
        + float(incremental["luxalgo_vs_zone_excess_pp"]) / 50.0
        + (0.0 if pd.isna(p_val) else max(0.0, 0.05 - float(p_val)) * 100.0)
    )

    return ValidationResult(
        params=p,
        summary=summary,
        event_study=event_study,
        zone_conditional=zone_conditional,
        incremental=incremental,
        best_score=round(score, 4),
    )


def classify_breadth_zone(pct: float) -> str:
    from market_breadth_ma import classify_breadth_zone as _c

    return _c(pct)


def sweep_breadth_impulse_params(
    conn: sqlite3.Connection,
    *,
    start_date: str = DEFAULT_START,
    end_date: str | None = None,
) -> tuple[BreadthImpulseParams, ValidationResult, pd.DataFrame]:
    grid: list[BreadthImpulseParams] = []
    for zweig_low in (0.35, 0.40, 0.45):
        for zweig_high in (0.58, 0.615, 0.65):
            if zweig_high <= zweig_low + 0.10:
                continue
            for deemer in (1.85, 1.97, 2.10):
                for hold in (42, 63, 84):
                    grid.append(
                        BreadthImpulseParams(
                            zweig_low=zweig_low,
                            zweig_high=zweig_high,
                            deemer_10d_ratio=deemer,
                            thrust_hold_days=hold,
                        )
                    )

    rows: list[dict[str, Any]] = []
    best: ValidationResult | None = None
    best_params: BreadthImpulseParams | None = None
    for params in grid:
        try:
            res = run_breadth_impulse_validation(
                conn, start_date=start_date, end_date=end_date, params=params
            )
        except Exception:
            continue
        inc = res.incremental
        rows.append(
            {
                "zweig_low": params.zweig_low,
                "zweig_high": params.zweig_high,
                "deemer": params.deemer_10d_ratio,
                "hold_days": params.thrust_hold_days,
                "score": res.best_score,
                "luxalgo_excess_pct": res.summary.loc[
                    res.summary["variant_id"] == "luxalgo_full", "excess_return_pct"
                ].iloc[0],
                "luxalgo_sharpe": res.summary.loc[
                    res.summary["variant_id"] == "luxalgo_full", "sharpe"
                ].iloc[0],
                "vs_zone_excess_pp": inc["luxalgo_vs_zone_excess_pp"],
                "vs_zone_sharpe_delta": inc["luxalgo_vs_zone_sharpe_delta"],
                "event_p": inc["event_study_p_value"],
            }
        )
        if best is None or res.best_score > best.best_score:
            best = res
            best_params = params

    if not rows:
        raise RuntimeError("param sweep produced no results")
    sweep_df = pd.DataFrame(rows).sort_values("score", ascending=False)
    if best is None or best_params is None:
        raise RuntimeError("param sweep produced no valid best result")
    return best_params, best, sweep_df


def render_validation_markdown(
    result: ValidationResult,
    sweep_top: pd.DataFrame | None,
    *,
    start_date: str,
    end_date: str,
    params_note: str = "",
) -> str:
    p = result.params
    inc = result.incremental
    lines = [
        "# Breadth impulse validation · Zweig + Deemer vs Breadth zone",
        "",
        f"> 區間 **{start_date}** ~ **{end_date}** · 基準 IX0001 · universe ETF 成分股 adv/decl",
        f"> 參數：zweig {p.zweig_low:.0%}→{p.zweig_high:.0%} EMA{p.zweig_ema_span} · "
        f"Deemer≥{p.deemer_10d_ratio} · hold {p.thrust_hold_days}d{params_note}",
        "",
        "## 假說",
        "",
        "- **Breadth zone** = 200MA 參與「水位」（狀態）",
        "- **Zweig thrust / Deemer BAM** = adv/decl 廣度「推力事件」（事件）",
        "- 若事件層有增量，應見於 (1) overlay A/B 績效差 (2) 事件日 forward return 顯著較高",
        "",
        "## Overlay A/B（指數曝險調節）",
        "",
        "| 變體 | 總報酬% | 超額% | Sharpe | MDD% | 平均曝險 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for _, row in result.summary.iterrows():
        lines.append(
            f"| {row['variant']} | {row['total_return_pct']:+.2f} | "
            f"{row['excess_return_pct']:+.2f} | {row['sharpe']:.2f} | "
            f"{row['max_drawdown_pct']:.2f} | {row['avg_exposure']:.2f} |"
        )

    lines.extend(
        [
            "",
            "## 增量統計（LuxAlgo full − Breadth zone only）",
            "",
            f"- 超額報酬差：**{inc['luxalgo_vs_zone_excess_pp']:+.2f} pp**",
            f"- Sharpe 差：**{inc['luxalgo_vs_zone_sharpe_delta']:+.2f}**",
            f"- MDD 改善（愈少愈好）：**{inc['luxalgo_vs_zone_mdd_improve_pp']:+.2f} pp**",
            f"- 事件日 vs 非事件日 forward 20D 均值差：**{inc['event_study_fwd20_spread_pp']} pp** "
            f"（Welch p={inc['event_study_p_value']}）",
            f"- 推力/窗口日占比：**{inc['thrust_window_day_pct']}%** · "
            f"fire days：**{inc['thrust_fire_days']}**",
            "",
            "## Event study · forward returns",
            "",
            "| 樣本 | 天數 | Fwd20 均值% | Fwd20 中位% | Fwd63 均值% | 勝率% |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for _, row in result.event_study.iterrows():
        if str(row.get("cohort", "")).startswith("event_vs"):
            continue
        lines.append(
            f"| {row['cohort']} | {row['days']} | {row.get('fwd20_mean_pct', '—')} | "
            f"{row.get('fwd20_median_pct', '—')} | {row.get('fwd63_mean_pct', '—')} | "
            f"{row.get('fwd20_hit_rate_pct', '—')} |"
        )

    if not result.zone_conditional.empty:
        lines.extend(
            [
                "",
                "## 同 Breadth zone 內 · thrust window vs calm（Fwd63）",
                "",
                "| Zone | 天數 | 窗口日 | 窗口 Fwd63% | 非窗口 Fwd63% | 差 pp |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for _, row in result.zone_conditional.iterrows():
            lines.append(
                f"| {row['breadth_zone_200']} | {row['days']} | {row['thrust_window_days']} | "
                f"{row.get('fwd63_thrust_mean_pct', '—')} | {row.get('fwd63_calm_mean_pct', '—')} | "
                f"{row.get('fwd63_spread_pp', '—')} |"
            )

    if sweep_top is not None and not sweep_top.empty:
        lines.extend(["", "## Param sweep · top 5 by score", ""])
        cols = [
            "zweig_low",
            "zweig_high",
            "deemer",
            "hold_days",
            "score",
            "luxalgo_sharpe",
            "vs_zone_sharpe_delta",
            "event_p",
        ]
        lines.append("| " + " | ".join(cols) + " |")
        lines.append("|" + "|".join(["---:"] * len(cols)) + "|")
        for _, row in sweep_top.head(5).iterrows():
            lines.append(
                "| "
                + " | ".join(str(row[c]) for c in cols)
                + " |"
            )

    lines.extend(
        [
            "",
            "## Regime 導入結論",
            "",
            "- 併入 **Breadth zone 軸子區塊** `impulse`（`thrust_active` · `deemer_flag` · `thrust_days_remaining`）",
            "- **非** live gate · **非** Strategy overlay · 診斷用 only",
            "",
        ]
    )
    return "\n".join(lines)


def persist_validation_artifacts(
    result: ValidationResult,
    sweep_df: pd.DataFrame | None,
    *,
    report_path: Path,
) -> Path:
    out = report_path.with_suffix(".json")
    payload = {
        "params": result.params.__dict__,
        "incremental": result.incremental,
        "summary": result.summary.to_dict(orient="records"),
        "event_study": result.event_study.to_dict(orient="records"),
        "zone_conditional": result.zone_conditional.to_dict(orient="records"),
        "best_score": result.best_score,
    }
    if sweep_df is not None:
        payload["sweep_top10"] = sweep_df.head(10).to_dict(orient="records")
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
