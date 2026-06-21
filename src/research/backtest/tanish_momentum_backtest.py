"""tanish35 Multi-Factor Momentum · TW local backtest (FinMind DB).

Faithful to https://github.com/tanish35/Momentum-Investing `NewMom` params:
  - Market regime: benchmark close > 200-day SMA
  - TSMOM: stock close > 200-day SMA
  - Cross-sectional score: 0.5×mean(Mom60,120,252) + 0.5×FIP252 + 0.5×Skew90
  - Top 5 · inverse 126d close StdDev weighting · rebalance when top-5 set changes

Breadth zone overlay (Regime layer · 200MA zone): optional gate to trade only in
`strong` (60–80%) and/or `overbought` (>80%) buckets.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
from scipy.stats import skew

from .dual_momentum_antonacci import DEFAULT_RF_ANNUAL, _compute_stats
from .finpilot_local_backtest import load_price_panels
from market_benchmark import load_benchmark_close
from market_breadth_ma import (
    BREADTH_ZONE_DISPLAY,
    BREADTH_ZONE_ZH,
    BREADTH_ZONES_ORDER,
    BreadthZone,
    build_breadth_panel,
    breadth_map_by_date,
)
from stock_db import DEFAULT_DB_PATH, PROJECT_ROOT, connect

StrategyVariant = Literal[
    "author",
    "strong",
    "overbought",
    "strong_overbought",
]

VARIANT_LABELS: dict[StrategyVariant, str] = {
    "author": "tanish35 NewMom (author · 200MA regime only)",
    "strong": "tanish35 NewMom · Breadth zone Strong only",
    "overbought": "tanish35 NewMom · Breadth zone Overbought only",
    "strong_overbought": "tanish35 NewMom · Breadth zone Strong + Overbought",
}

LOOKBACKS = (60, 120, 252)
TOP_N = 5
VOL_LOOKBACK = 126
SKEW_LOOKBACK = 90
FIP_LOOKBACK = 252
TS_MOM_LOOKBACK = 200
REGIME_MA = 200
MOM_WEIGHT = 0.5
FIP_WEIGHT = 0.5
SKEW_WEIGHT = 0.5
WARMUP_DAYS = max(REGIME_MA, max(LOOKBACKS), VOL_LOOKBACK, SKEW_LOOKBACK, FIP_LOOKBACK)


@dataclass(frozen=True)
class TanishMomentumParams:
    lookbacks: tuple[int, ...] = LOOKBACKS
    top_n: int = TOP_N
    vol_lookback: int = VOL_LOOKBACK
    skew_lookback: int = SKEW_LOOKBACK
    fip_lookback: int = FIP_LOOKBACK
    ts_mom_lookback: int = TS_MOM_LOOKBACK
    regime_ma_period: int = REGIME_MA
    momentum_weight: float = MOM_WEIGHT
    fip_weight: float = FIP_WEIGHT
    skewness_penalty: float = SKEW_WEIGHT
    rf_annual: float = DEFAULT_RF_ANNUAL
    return_clip_pct: float = 35.0


@dataclass
class TanishBacktestResult:
    variant: StrategyVariant
    label: str
    start_date: str
    end_date: str
    daily: pd.DataFrame
    stats: dict[str, float | int | str]
    signal_summary: dict[str, object] = field(default_factory=dict)


def _rolling_skew(log_ret: pd.DataFrame, window: int) -> pd.DataFrame:
    def _one(col: pd.Series) -> pd.Series:
        return col.rolling(window, min_periods=window).apply(
            lambda x: float(skew(x, bias=False)) if len(x) == window else np.nan,
            raw=True,
        )

    return log_ret.apply(_one)


def _fip_score(daily_ret: pd.DataFrame, window: int) -> pd.DataFrame:
    pos = (daily_ret > 0).astype(float)
    return pos.rolling(window, min_periods=window).mean()


def _absolute_momentum(close: pd.DataFrame, period: int) -> pd.DataFrame:
    """Backtrader Momentum: close - close.shift(period)."""
    return close - close.shift(period)


def _sanitize_close_panel(close: pd.DataFrame) -> pd.DataFrame:
    """Drop symbols with non-positive or discontinuous prices (bad FinMind ticks)."""
    ok_cols: list[str] = []
    for col in close.columns:
        s = close[col].dropna()
        if s.empty or (s <= 0).any():
            continue
        rets = s.pct_change(fill_method=None)
        if np.isinf(rets).any():
            continue
        ok_cols.append(col)
    return close[ok_cols]


def _precompute_indicators(
    close: pd.DataFrame, params: TanishMomentumParams
) -> dict[str, pd.DataFrame | pd.Series]:
    daily_ret = close.pct_change(fill_method=None)
    ratio = close / close.shift(1)
    ratio = ratio.where(ratio > 0)
    log_ret = np.log(ratio)
    moms = [_absolute_momentum(close, lb) for lb in params.lookbacks]
    mom_mean = sum(moms) / len(moms)
    return {
        "daily_ret": daily_ret,
        "sma200": close.rolling(params.ts_mom_lookback, min_periods=params.ts_mom_lookback).mean(),
        "mom_mean": mom_mean,
        "fip": _fip_score(daily_ret, params.fip_lookback),
        "skew": _rolling_skew(log_ret, params.skew_lookback),
        "vol": close.rolling(params.vol_lookback, min_periods=params.vol_lookback).std(),
    }


def _allowed_zones(variant: StrategyVariant) -> frozenset[BreadthZone] | None:
    if variant == "author":
        return None
    if variant == "strong":
        return frozenset({"strong"})
    if variant == "overbought":
        return frozenset({"overbought"})
    return frozenset({"strong", "overbought"})


def _combined_score_row(
    stocks: list[str],
    day: str,
    ind: dict[str, pd.DataFrame | pd.Series],
    params: TanishMomentumParams,
) -> list[tuple[str, float]]:
    close = ind["close"]  # type: ignore[assignment]
    sma200 = ind["sma200"]  # type: ignore[assignment]
    mom_mean = ind["mom_mean"]  # type: ignore[assignment]
    fip = ind["fip"]  # type: ignore[assignment]
    skew_df = ind["skew"]  # type: ignore[assignment]
    if day not in close.index:
        return []
    scored: list[tuple[str, float]] = []
    for s in stocks:
        try:
            px = float(close.at[day, s])
            ma = float(sma200.at[day, s])
            mm = float(mom_mean.at[day, s])
            fp = float(fip.at[day, s])
            sk = float(skew_df.at[day, s])
        except (KeyError, TypeError, ValueError):
            continue
        if any(np.isnan(v) for v in (px, ma, mm, fp, sk)):
            continue
        if px <= ma or mm <= 0:
            continue
        score = (
            params.momentum_weight * mm
            + params.fip_weight * fp
            + params.skewness_penalty * sk
        )
        scored.append((s, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def _inverse_vol_weights(
    picks: list[str], day: str, vol: pd.DataFrame
) -> dict[str, float]:
    if not picks:
        return {}
    inv: dict[str, float] = {}
    for s in picks:
        try:
            v = float(vol.at[day, s])
        except (KeyError, TypeError, ValueError):
            continue
        if np.isnan(v) or v <= 0:
            continue
        inv[s] = 1.0 / v
    total = sum(inv.values())
    if total <= 0:
        w = 1.0 / len(picks)
        return {s: w for s in picks}
    return {s: inv[s] / total for s in inv}


def simulate_tanish_momentum(
    close: pd.DataFrame,
    bench: pd.Series,
    zone_by_date: dict[str, str],
    *,
    variant: StrategyVariant,
    bt_dates: list[str],
    params: TanishMomentumParams | None = None,
) -> TanishBacktestResult:
    p = params or TanishMomentumParams()
    allowed = _allowed_zones(variant)
    ind = _precompute_indicators(close, p)
    ind["close"] = close
    bench_sma = bench.rolling(p.regime_ma_period, min_periods=p.regime_ma_period).mean()
    daily_ret = ind["daily_ret"].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    bound = p.return_clip_pct / 100.0
    daily_ret = daily_ret.clip(-bound, bound)
    bench_ret = bench.pct_change(fill_method=None).fillna(0.0)
    cash_daily = (1.0 + p.rf_annual) ** (1.0 / 252.0) - 1.0

    stocks = list(close.columns)
    last_picks: tuple[str, ...] = ()
    weights: dict[str, float] = {}
    strat_rets: list[float] = []
    dates_out: list[str] = []
    invested_days = 0
    rebalances = 0
    zone_days: dict[str, int] = {z: 0 for z in BREADTH_ZONES_ORDER}

    for day in bt_dates:
        zone = zone_by_date.get(day, "unknown")
        if zone in zone_days:
            zone_days[zone] += 1

        regime_ok = False
        if day in bench.index and day in bench_sma.index:
            bpx = float(bench.loc[day])
            bma = float(bench_sma.loc[day])
            regime_ok = not np.isnan(bpx) and not np.isnan(bma) and bpx > bma

        zone_ok = allowed is None or zone in allowed
        risk_on = regime_ok and zone_ok

        if not risk_on:
            port_ret = cash_daily
            weights = {}
            last_picks = ()
        else:
            scored = _combined_score_row(stocks, day, ind, p)
            picks = [s for s, _ in scored[: p.top_n]]
            pick_key = tuple(sorted(picks))
            if pick_key != last_picks:
                weights = _inverse_vol_weights(picks, day, ind["vol"])  # type: ignore[arg-type]
                last_picks = pick_key
                rebalances += 1
            if not weights:
                port_ret = cash_daily
            else:
                port_ret = 0.0
                for s, w in weights.items():
                    if day in daily_ret.index and s in daily_ret.columns:
                        port_ret += w * float(daily_ret.at[day, s])
                invested_days += 1

        strat_rets.append(port_ret)
        dates_out.append(day)

    daily = pd.DataFrame(
        {
            "strategy_return": strat_rets,
            "bench_return": [float(bench_ret.loc[d]) if d in bench_ret.index else 0.0 for d in dates_out],
        },
        index=pd.Index(dates_out, name="trade_date"),
    )
    stats = _compute_stats(daily["strategy_return"], daily["bench_return"])
    stats["variant"] = variant
    avg_exp = invested_days / max(len(bt_dates), 1)
    return TanishBacktestResult(
        variant=variant,
        label=VARIANT_LABELS[variant],
        start_date=dates_out[0],
        end_date=dates_out[-1],
        daily=daily,
        stats=stats,
        signal_summary={
            "invested_days": invested_days,
            "avg_exposure": round(avg_exp, 3),
            "rebalances": rebalances,
            "zone_days_in_window": zone_days,
            "allowed_zones": sorted(allowed) if allowed else ["all"],
            "source_repo": "https://github.com/tanish35/Momentum-Investing",
        },
    )


def run_tanish_breadth_comparison(
    conn: sqlite3.Connection | None = None,
    *,
    start_date: str = "2026-01-01",
    end_date: str | None = None,
    warmup_start: str = "2024-01-01",
    params: TanishMomentumParams | None = None,
) -> dict[str, object]:
    own = conn is None
    if own:
        conn = connect(DEFAULT_DB_PATH)
    close, _, _ = load_price_panels(conn)
    close = _sanitize_close_panel(close)
    bench = load_benchmark_close(conn)
    panel = build_breadth_panel(conn, date_start=warmup_start, date_end=end_date or "2099-12-31")
    zone_by_date = breadth_map_by_date(panel, use="200")

    all_dates = sorted(set(close.index) & set(bench.index))
    if end_date:
        all_dates = [d for d in all_dates if d <= end_date]
    bt_dates = [d for d in all_dates if d >= start_date]
    if len(bt_dates) < 5:
        raise ValueError(f"回測區間不足：{start_date}..{end_date}")

    # Indicator warm-up: keep full close panel; simulate only bt_dates
    variants: tuple[StrategyVariant, ...] = (
        "author",
        "strong",
        "overbought",
        "strong_overbought",
    )
    results = [
        simulate_tanish_momentum(
            close,
            bench,
            zone_by_date,
            variant=v,
            bt_dates=bt_dates,
            params=params,
        )
        for v in variants
    ]

    # Ex-post: author strategy return sliced by signal-day breadth zone
    author = next(r for r in results if r.variant == "author")
    by_zone: dict[str, dict[str, float | int]] = {}
    for zone in BREADTH_ZONES_ORDER:
        mask = pd.Series(
            [zone_by_date.get(d) == zone for d in author.daily.index],
            index=author.daily.index,
        )
        if not mask.any():
            continue
        sr = author.daily.loc[mask, "strategy_return"]
        br = author.daily.loc[mask, "bench_return"]
        by_zone[zone] = {
            "days": int(mask.sum()),
            "total_return_pct": round(float((1 + sr).prod() - 1) * 100, 2),
            "bench_return_pct": round(float((1 + br).prod() - 1) * 100, 2),
            "excess_return_pct": round(
                float((1 + sr).prod() - (1 + br).prod()) * 100, 2
            ),
        }

    out: dict[str, object] = {
        "start_date": start_date,
        "end_date": bt_dates[-1],
        "warmup_start": warmup_start,
        "results": results,
        "by_zone_expost_author": by_zone,
        "breadth_panel_2026": panel[panel["trade_date"] >= start_date].to_dict(orient="records")
        if not panel.empty
        else [],
    }
    if own:
        conn.close()
    return out


def render_tanish_backtest_markdown(payload: dict[str, object]) -> str:
    results: list[TanishBacktestResult] = payload["results"]  # type: ignore[assignment]
    start = payload["start_date"]
    end = payload["end_date"]
    by_zone: dict = payload.get("by_zone_expost_author", {})  # type: ignore[assignment]

    lines = [
        "# tanish35 Multi-Factor Momentum × Breadth zone",
        "",
        f"> 回測區間：**{start}** ~ **{end}** · 基準：**IX0001** · 資料：`data/stocks.db`",
        f"> 來源：[tanish35/Momentum-Investing](https://github.com/tanish35/Momentum-Investing) · `NewMom` 參數忠於作者",
        "",
        "## 策略規則（台股適配）",
        "",
        "| 元件 | 規則 |",
        "|---|---|",
        f"| Market regime | IX0001 close > {REGIME_MA}d SMA → 允許持倉 |",
        f"| TSMOM | 個股 close > {TS_MOM_LOOKBACK}d SMA |",
        f"| Momentum | mean(Mom{LOOKBACKS[0]}, Mom{LOOKBACKS[1]}, Mom{LOOKBACKS[2]}) > 0（Backtrader 絕對動量） |",
        f"| FIP | {FIP_LOOKBACK}d 正報酬日占比 |",
        f"| Skew | {SKEW_LOOKBACK}d log-return 偏度（加權 +{SKEW_WEIGHT}） |",
        f"| Score | {MOM_WEIGHT}×Mom + {FIP_WEIGHT}×FIP + {SKEW_WEIGHT}×Skew |",
        f"| 持倉 | Top {TOP_N} · inverse {VOL_LOOKBACK}d close StdDev 權重 |",
        "| 再平衡 | Top-N 組合變動時才調倉 |",
        "| Breadth overlay | `strong` / `overbought` 變體僅在對應 **Breadth zone**（200MA）交易 |",
        "",
        "## 績效摘要",
        "",
        "| 變體 | 總報酬% | 基準% | 超額% | Sharpe | MDD% | 持倉日 | 再平衡 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        s = r.stats
        sig = r.signal_summary
        lines.append(
            f"| {r.label} | {s['total_return_pct']} | {s['bench_return_pct']} | "
            f"{s['excess_return_pct']} | {s['sharpe']} | {s['max_drawdown_pct']} | "
            f"{sig.get('invested_days', '—')} | {sig.get('rebalances', '—')} |"
        )

    lines.extend(
        [
            "",
            "## Author 策略 · 依 Breadth zone 事後分層（ex-post）",
            "",
            "| Breadth zone | 天數 | 策略% | 基準% | 超額% |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for zone in BREADTH_ZONES_ORDER:
        row = by_zone.get(zone)
        if not row:
            continue
        zh = BREADTH_ZONE_ZH.get(zone, zone)
        disp = BREADTH_ZONE_DISPLAY.get(zone, zone)
        lines.append(
            f"| {disp} | {row['days']} | {row['total_return_pct']} | "
            f"{row['bench_return_pct']} | {row['excess_return_pct']} |"
        )

    lines.extend(
        [
            "",
            "## 備註",
            "",
            "- **PIT**：訊號日 T 僅用 `date ≤ T` 資料；Breadth zone 為 Regime 診斷軸，非 live gate。",
            "- 作者原 repo 用 SPY 200MA + 美股/ETF universe；此處改 IX0001 + FinMind 成分股面板。",
            f"- Warm-up 自 **{payload.get('warmup_start')}** 起算指標，回測計入區間自 **{start}**。",
        ]
    )
    return "\n".join(lines) + "\n"


def persist_tanish_artifacts(
    payload: dict[str, object], *, out_dir: Path | None = None
) -> Path:
    base = out_dir or (PROJECT_ROOT / "reports" / "research" / "breadth")
    base.mkdir(parents=True, exist_ok=True)
    results: list[TanishBacktestResult] = payload["results"]  # type: ignore[assignment]
    end = payload["end_date"]
    blob = {
        "start_date": payload["start_date"],
        "end_date": end,
        "source": "tanish35/Momentum-Investing",
        "variants": [
            {
                "variant": r.variant,
                "label": r.label,
                "stats": r.stats,
                "signal_summary": r.signal_summary,
            }
            for r in results
        ],
        "by_zone_expost_author": payload.get("by_zone_expost_author"),
    }
    path = base / f"tanish_momentum_breadth_{str(end).replace('-', '')}.json"
    path.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
