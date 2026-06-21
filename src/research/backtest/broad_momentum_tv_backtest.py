"""Broad-momentum TradingView strategy suite · TW backtest (FinMind DB).

Compares four TV-mapped strategies on IX0001 / local stock universe:

1. Antonacci 12M Absolute Momentum (vs risk-free)
2. 12M Return Strategy (vs zero)
3. Minervini SEPA market-health basket (Trend Template 7/7 bulk · 8/8 with RS)
4. NADY ADX-RSI trend filter (daily · Wilder ADX + SMA200)

Zweig / LuxAlgo index overlay removed · Regime diagnostic: market_breadth_impulse.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import yaml

from .dual_momentum_antonacci import DEFAULT_RF_ANNUAL, _compute_stats, _mom_12m_at
from .finpilot_local_backtest import load_price_panels, month_end_trading_dates
from market_breadth_ma import build_breadth_panel
from stage_analysis import (
    MINERVINI_CRITERIA_TOTAL,
    vectorized_minervini_criteria_count,
    vectorized_minervini_pass_pct,
)
from stock_db import PROJECT_ROOT
from report_paths import REPORTS_RESEARCH

DEFAULT_BROAD_MOMENTUM_TV_CONFIG = PROJECT_ROOT / "config" / "broad_momentum_tv.yaml"

StrategyId = Literal[
    "buy_hold",
    "abs_12m_rf",
    "abs_12m_zero",
    "minervini_basket",
    "adx_rsi_trend",
]

SAVED_STRATEGY_IDS: tuple[str, ...] = ("minervini-sepa-basket",)
REGISTRY_TO_INTERNAL: dict[str, StrategyId] = {
    "minervini-sepa-basket": "minervini_basket",
}
INTERNAL_TO_REGISTRY: dict[StrategyId, str] = {
    v: k for k, v in REGISTRY_TO_INTERNAL.items()
}

STRATEGY_LABELS: dict[StrategyId, str] = {
    "buy_hold": "IX0001 Buy & Hold",
    "abs_12m_rf": "Antonacci 12M Absolute (>RF)",
    "abs_12m_zero": "12M Return Strategy (>0)",
    "minervini_basket": "Minervini SEPA (Trend Template basket)",
    "adx_rsi_trend": "NADY ADX-RSI Trend (daily)",
}

LOOKBACK_12M = 252


@dataclass(frozen=True)
class BroadMomentumTvParams:
    minervini_pass: int = 7
    return_clip_pct: float = 35.0
    adx_period: int = 14
    adx_threshold: float = 20.0
    sma_trend: int = 200


def load_broad_momentum_tv_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or DEFAULT_BROAD_MOMENTUM_TV_CONFIG
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"invalid broad_momentum_tv yaml: {cfg_path}")
    return raw


def get_saved_strategy_spec(
    strategy_id: str, path: Path | None = None
) -> dict[str, Any]:
    cfg = load_broad_momentum_tv_config(path)
    block = (cfg.get("strategies") or {}).get(strategy_id)
    if not block:
        raise KeyError(f"saved strategy not found: {strategy_id}")
    return block


def params_from_config(path: Path | None = None) -> BroadMomentumTvParams:
    cfg = load_broad_momentum_tv_config(path)
    minervini = (cfg.get("strategies") or {}).get("minervini-sepa-basket") or {}
    mp = minervini.get("params") or {}
    return BroadMomentumTvParams(
        minervini_pass=int(mp.get("trend_template_min_criteria", 7)),
        return_clip_pct=float(mp.get("return_clip_pct", 35)),
    )


@dataclass
class StrategyBacktestResult:
    strategy_id: StrategyId
    label: str
    start_date: str
    end_date: str
    daily: pd.DataFrame
    stats: dict[str, float | int | str]
    signal_summary: dict[str, object] = field(default_factory=dict)


def _rf_12m(rf_annual: float) -> float:
    return (1.0 + rf_annual) ** 1.0 - 1.0


def load_benchmark_ohlc(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT date AS trade_date, open, high, low, close
        FROM daily_bars
        WHERE code = 'IX0001'
        ORDER BY date
        """
    ).fetchall()
    if not rows:
        raise RuntimeError("daily_bars 無 IX0001 資料")
    df = pd.DataFrame(rows, columns=["trade_date", "open", "high", "low", "close"])
    df = df.drop_duplicates(subset=["trade_date"], keep="last")
    return df.set_index("trade_date").astype(float)


def _trend_template_pass_count(close: pd.DataFrame, *, min_pass: int) -> pd.Series:
    """Daily % of universe passing Minervini template (RS omitted in bulk scan)."""
    return vectorized_minervini_pass_pct(close, min_pass=min_pass)


def _sanitize_stock_returns(rets: pd.DataFrame, *, clip_pct: float) -> pd.DataFrame:
    """Remove inf/nan from pct-change panel (bad ticks / corporate actions)."""
    clean = rets.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    bound = clip_pct / 100.0
    return clean.clip(-bound, bound)


def _compute_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm = pd.Series(plus_dm, index=close.index)
    minus_dm = pd.Series(minus_dm, index=close.index)

    alpha = 1.0 / period
    atr = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100.0 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr.replace(0, np.nan)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100.0
    return dx.ewm(alpha=alpha, adjust=False).mean()


def _monthly_exposure_abs(
    bench_close: pd.Series,
    dates: list[str],
    month_ends: list[str],
    *,
    vs_rf: bool,
    rf_annual: float,
) -> dict[str, float]:
    rf12 = _rf_12m(rf_annual)
    sched: dict[str, float] = {}
    for me in month_ends:
        if me not in dates:
            continue
        m = _mom_12m_at(bench_close, me)
        if m is None:
            sched[me] = 0.0
        elif vs_rf:
            sched[me] = 1.0 if m > rf12 else 0.0
        else:
            sched[me] = 1.0 if m > 0.0 else 0.0
    return sched


def _apply_monthly_exposure(
    dates: list[str],
    bench_ret: pd.Series,
    month_ends: list[str],
    sched: dict[str, float],
    *,
    cash_daily: float = 0.0,
) -> tuple[list[float], list[str]]:
    me_sorted = sorted(d for d in month_ends if d in sched)
    cur_exp = 0.0
    for me in reversed(me_sorted):
        if me <= dates[0]:
            cur_exp = sched[me]
            break
    me_ptr = 0
    while me_ptr < len(me_sorted) and me_sorted[me_ptr] <= dates[0]:
        me_ptr += 1
    rets: list[float] = []
    out_dates: list[str] = []
    for d in dates:
        while me_ptr < len(me_sorted) and me_sorted[me_ptr] <= d:
            cur_exp = sched[me_sorted[me_ptr]]
            me_ptr += 1
        r_b = float(bench_ret.loc[d])
        rets.append(cur_exp * r_b + (1.0 - cur_exp) * cash_daily)
        out_dates.append(d)
    return rets, out_dates


def _minervini_basket_returns(
    close: pd.DataFrame,
    dates: list[str],
    month_ends: list[str],
    bench_ret: pd.Series,
    params: BroadMomentumTvParams,
) -> tuple[list[float], list[str], dict[str, object]]:
    crit = vectorized_minervini_criteria_count(close)
    passed = crit >= min(params.minervini_pass, MINERVINI_CRITERIA_TOTAL - 1)
    stock_rets = _sanitize_stock_returns(
        close.pct_change(fill_method=None), clip_pct=params.return_clip_pct
    )

    me_in_range = [me for me in month_ends if me in dates]
    me_ptr = 0
    cur_weights: pd.Series | None = None
    rets: list[float] = []
    out_dates: list[str] = []
    pick_counts: list[int] = []

    for d in dates:
        while me_ptr < len(me_in_range) and me_in_range[me_ptr] <= d:
            me = me_in_range[me_ptr]
            picks = passed.loc[me]
            picks = picks[picks].index.tolist()
            if picks:
                w = 1.0 / len(picks)
                cur_weights = pd.Series(w, index=picks)
            else:
                cur_weights = None
            me_ptr += 1

        if cur_weights is not None:
            common = [s for s in cur_weights.index if s in stock_rets.columns]
            if common:
                r = float((stock_rets.loc[d, common] * cur_weights.loc[common]).sum())
                pick_counts.append(len(common))
            else:
                r = float(bench_ret.loc[d])
                pick_counts.append(0)
        else:
            r = 0.0
            pick_counts.append(0)
        rets.append(r)
        out_dates.append(d)

    summary = {
        "avg_picks": round(float(np.mean(pick_counts)), 1) if pick_counts else 0,
        "max_picks": int(max(pick_counts)) if pick_counts else 0,
        "zero_pick_days": int(sum(1 for c in pick_counts if c == 0)),
    }
    return rets, out_dates, summary


def _adx_rsi_exposure(bench: pd.DataFrame, params: BroadMomentumTvParams) -> pd.Series:
    adx = _compute_adx(
        bench["high"], bench["low"], bench["close"], params.adx_period
    )
    sma = bench["close"].rolling(params.sma_trend, min_periods=params.sma_trend).mean()
    long_ok = (adx > params.adx_threshold) & (bench["close"] > sma)
    exp = long_ok.astype(float)
    return exp.fillna(0.0)


def run_all_broad_momentum_backtests(
    conn: sqlite3.Connection,
    *,
    start_date: str = "2024-01-01",
    end_date: str | None = None,
    rf_annual: float = DEFAULT_RF_ANNUAL,
    config_path: Path | None = None,
) -> tuple[pd.DataFrame, list[StrategyBacktestResult], pd.DataFrame | None]:
    params = params_from_config(config_path)
    stock_close, _, _ = load_price_panels(conn)
    bench_df = load_benchmark_ohlc(conn)
    bench_close = bench_df["close"]
    all_dates = sorted(set(stock_close.index) & set(bench_close.index))
    if end_date:
        all_dates = [d for d in all_dates if d <= end_date]
    if not all_dates:
        raise RuntimeError("無重疊交易日")

    bench_ret_full = bench_close.pct_change(fill_method=None).fillna(0.0)
    month_ends = month_end_trading_dates(all_dates)
    cash_daily = (1.0 + rf_annual) ** (1.0 / 252.0) - 1.0

    bt_dates = [d for d in all_dates if d >= start_date]
    if not bt_dates:
        raise ValueError(f"start_date {start_date} 無交易日")
    end_date = bt_dates[-1]

    adx_exp = _adx_rsi_exposure(bench_df.reindex(all_dates).ffill(), params)

    sched_rf = _monthly_exposure_abs(
        bench_close, all_dates, month_ends, vs_rf=True, rf_annual=rf_annual
    )
    sched_zero = _monthly_exposure_abs(
        bench_close, all_dates, month_ends, vs_rf=False, rf_annual=rf_annual
    )

    min_rets, min_dates, min_summary = _minervini_basket_returns(
        stock_close, all_dates, month_ends, bench_ret_full, params
    )

    strategies: list[tuple[StrategyId, list[float], list[str], dict[str, object]]] = []

    bh_rets = [float(bench_ret_full.loc[d]) for d in bt_dates]
    strategies.append(("buy_hold", bh_rets, bt_dates, {}))

    abs_rf_rets, abs_rf_dates = _apply_monthly_exposure(
        bt_dates, bench_ret_full, month_ends, sched_rf, cash_daily=cash_daily
    )
    strategies.append(
        (
            "abs_12m_rf",
            abs_rf_rets,
            abs_rf_dates,
            {
                "risk_on_months": sum(
                    1 for d in month_ends if d in sched_rf and d >= start_date and sched_rf[d] >= 1.0
                ),
            },
        )
    )

    abs_zero_rets, abs_zero_dates = _apply_monthly_exposure(
        bt_dates, bench_ret_full, month_ends, sched_zero, cash_daily=cash_daily
    )
    strategies.append(("abs_12m_zero", abs_zero_rets, abs_zero_dates, {}))

    min_bt = [r for r, d in zip(min_rets, min_dates, strict=True) if d >= start_date]
    strategies.append(("minervini_basket", min_bt, bt_dates, min_summary))

    adx_bt = [
        float(adx_exp.loc[d]) * float(bench_ret_full.loc[d])
        + (1.0 - float(adx_exp.loc[d])) * cash_daily
        for d in bt_dates
    ]
    strategies.append(
        (
            "adx_rsi_trend",
            adx_bt,
            bt_dates,
            {"avg_exposure": round(float(adx_exp.loc[bt_dates].mean()), 3)},
        )
    )

    bench_bt = pd.Series([float(bench_ret_full.loc[d]) for d in bt_dates], index=bt_dates)
    results: list[StrategyBacktestResult] = []
    for sid, rets, dates, sig in strategies:
        daily = pd.DataFrame(
            {
                "strategy_return": rets,
                "bench_return": [float(bench_ret_full.loc[d]) for d in dates],
            },
            index=pd.Index(dates, name="trade_date"),
        )
        stats = _compute_stats(daily["strategy_return"], daily["bench_return"])
        stats["strategy_id"] = sid
        results.append(
            StrategyBacktestResult(
                strategy_id=sid,
                label=STRATEGY_LABELS[sid],
                start_date=dates[0],
                end_date=dates[-1],
                daily=daily,
                stats=stats,
                signal_summary=sig,
            )
        )

    summary_rows = [{"strategy": r.label, **r.stats} for r in results]
    summary = pd.DataFrame(summary_rows)

    regime_slice: pd.DataFrame | None = None
    try:
        regime_panel = build_breadth_panel(
            conn,
            date_start=start_date,
            date_end=end_date,
        )
        if not regime_panel.empty and "trade_date" in regime_panel.columns:
            rp = regime_panel.set_index("trade_date")
            rows: list[dict[str, object]] = []
            for r in results:
                if r.strategy_id == "buy_hold":
                    continue
                mask = (rp["pct_above_50"] >= 55.0) & (rp["pct_above_200"] >= 45.0)
                if mask.sum() == 0:
                    continue
                strat = r.daily["strategy_return"].reindex(rp.index).fillna(0.0)
                bench = r.daily["bench_return"].reindex(rp.index).fillna(0.0)
                rows.append(
                    {
                        "strategy": r.label,
                        "broad_days": int(mask.sum()),
                        "broad_total_return_pct": round(
                            float(((1 + strat[mask]).prod() - 1) * 100), 2
                        ),
                        "broad_bench_return_pct": round(
                            float(((1 + bench[mask]).prod() - 1) * 100), 2
                        ),
                        "broad_excess_pct": round(
                            float(
                                ((1 + strat[mask]).prod() - (1 + bench[mask]).prod()) * 100
                            ),
                            2,
                        ),
                    }
                )
            if rows:
                regime_slice = pd.DataFrame(rows)
    except Exception:
        regime_slice = None

    return summary, results, regime_slice


def render_backtest_markdown(
    summary: pd.DataFrame,
    results: list[StrategyBacktestResult],
    regime_slice: pd.DataFrame | None,
    *,
    start_date: str,
    end_date: str,
) -> str:
    lines = [
        "# Broad-Momentum TV 策略回測比較",
        "",
        f"> 區間：**{start_date}** ~ **{end_date}** · 基準：**IX0001** · 資料：`data/stocks.db`",
        "",
        "## 策略定義（台股適配）",
        "",
        "| # | TV 策略 | 規則摘要 |",
        "|---:|---|---|",
        "| 1 | Antonacci 12M Absolute | 月末：IX0001 12M > RF(1.5%) → 滿倉，否則現金 |",
        "| 2 | 12M Return Strategy | 月末：12M > 0 → 滿倉，否則現金 |",
        "| 3 | Minervini SEPA | 月末等權：Trend Template 7/7 成分股 basket |",
        "| 4 | NADY ADX-RSI | 日頻：ADX>20 且 IX0001>200MA → 滿倉 |",
        "",
        "## 績效摘要",
        "",
        "| 策略 | 總報酬% | 基準% | 超額% | CAGR% | Sharpe | MDD% | 勝日/基準 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in summary.iterrows():
        sid = row.get("strategy_id", "")
        beat = row.get("beat_bench_days", 0)
        td = row.get("trading_days", 1)
        lines.append(
            f"| {row['strategy']} | {row['total_return_pct']:+.2f} | "
            f"{row['bench_return_pct']:+.2f} | {row['excess_return_pct']:+.2f} | "
            f"{row['cagr_pct']:+.2f} | {row['sharpe']:.2f} | "
            f"{row['max_drawdown_pct']:.2f} | {beat}/{td} |"
        )

    lines.extend(["", "## 訊號統計", ""])
    for r in results:
        if r.signal_summary:
            parts = ", ".join(f"{k}={v}" for k, v in r.signal_summary.items())
            lines.append(f"- **{r.label}**: {parts}")

    if regime_slice is not None and not regime_slice.empty:
        lines.extend(
            [
                "",
                "## 廣度強勢子區間（50MA≥55% · 200MA≥45%）",
                "",
                "| 策略 | 普漲日數 | 策略報酬% | 基準% | 超額% |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for _, row in regime_slice.iterrows():
            lines.append(
                f"| {row['strategy']} | {row['broad_days']} | "
                f"{row['broad_total_return_pct']:+.2f} | "
                f"{row['broad_bench_return_pct']:+.2f} | "
                f"{row['broad_excess_pct']:+.2f} |"
            )

    lines.extend(
        [
        "",
        "## 解讀備註",
        "",
        "- 資料 universe：`stock_daily_bars` 約 **133 檔**（ETF 成分股同步範圍），非全台股。",
        "- Minervini 為**個股 basket**（非指數 overlay），最能代表 SEPA 選股邏輯。",
        "- ADX 為**指數曝險調節**；Antonacci / 12M Return 為**月頻 binary**。",
        "- Zweig / Deemer 廣度推力僅 **Regime 診斷**（`config/regime.yaml` · `run_breadth_impulse_validation.py`）。",
        "- 12M 策略需 252 交易日 warmup；2024 以前資料僅供訊號計算。",
        "- 個股日報酬已 clip ±35% 並剔除 inf（除權息異常 tick）。",
        "",
    ]
    )
    return "\n".join(lines)


def persist_saved_strategy_artifacts(
    results: list[StrategyBacktestResult],
    *,
    config_path: Path | None = None,
    reports_root: Path | None = None,
) -> dict[str, Path]:
    """Write backtest_summary.json under reports/{strategy_id}/ for saved strategies."""
    root = reports_root or (REPORTS_RESEARCH)
    cfg = load_broad_momentum_tv_config(config_path)
    by_internal = {r.strategy_id: r for r in results}
    written: dict[str, Path] = {}

    for registry_id in SAVED_STRATEGY_IDS:
        internal_id = REGISTRY_TO_INTERNAL[registry_id]
        result = by_internal.get(internal_id)
        if result is None:
            continue
        spec = get_saved_strategy_spec(registry_id, config_path)
        out_dir = root / registry_id
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "strategy_id": registry_id,
            "internal_id": internal_id,
            "title": spec.get("title", registry_id),
            "config_version": cfg.get("version"),
            "benchmark_code": cfg.get("benchmark_code", "IX0001"),
            "params": spec.get("params"),
            "backtest_period": {
                "start": result.start_date,
                "end": result.end_date,
            },
            "stats": result.stats,
            "signal_summary": result.signal_summary,
            "breadth_zone_200": spec.get("breadth_zone_200"),
            "adopted": spec.get("adopted"),
        }
        out_path = out_dir / "backtest_summary.json"
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written[registry_id] = out_path
    return written
