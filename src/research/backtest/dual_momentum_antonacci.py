"""Gary Antonacci dual momentum · TW ETF adaptation (FinMind).

Absolute momentum: 12M return vs risk-free (T-bill proxy).
Relative momentum: pick stronger of domestic (0050) vs intl (00646).
Safe leg: investment-grade bond ETF (00720B).

Recommended overlay (§2.3): absolute circuit breaker on 0050 — not full GEM as GPS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import numpy as np
import pandas as pd

from finmind_client import fetch_finmind
from .finpilot_local_backtest import month_end_trading_dates

AssetCode = Literal["0050", "00646", "00720B", "CASH"]
StrategyId = Literal[
    "buy_hold_0050",
    "gem_dual_momentum",
    "abs_circuit_breaker",
    "abs_only_bond_switch",
]

LOOKBACK_DAYS = 252
DEFAULT_RF_ANNUAL = 0.015
DEFAULT_RISK_OFF_EXPOSURE = 0.2

TW_GEM_ASSETS: dict[str, str] = {
    "0050": "元大台灣50（境內股票）",
    "00646": "元大S&P500（境外股票）",
    "00720B": "元大投等債20+（債券避險）",
    "CASH": "現金（無風險代理）",
}


@dataclass(frozen=True)
class RebalanceRow:
    signal_date: str
    strategy: StrategyId
    asset: AssetCode
    exposure: float
    mom_0050_12m_pct: float | None
    mom_00646_12m_pct: float | None
    mom_bond_12m_pct: float | None
    abs_ok: bool | None
    note: str


@dataclass
class DualMomentumResult:
    strategy: StrategyId
    label: str
    start_date: str
    end_date: str
    daily: pd.DataFrame
    rebalances: list[RebalanceRow]
    stats: dict[str, float | int | str]
    latest_signal: dict[str, object] = field(default_factory=dict)


def load_etf_close_panel(
    codes: list[str],
    *,
    start: date,
    end: date,
    adjusted: bool = True,
) -> pd.DataFrame:
    dataset = "TaiwanStockPriceAdj" if adjusted else "TaiwanStockPrice"
    frames: dict[str, pd.Series] = {}
    for code in codes:
        rows = fetch_finmind(dataset, code, start, end)
        if not rows:
            raise RuntimeError(f"FinMind {dataset} 無資料: {code}")
        s = pd.Series(
            {str(r["date"]): float(r["close"]) for r in rows},
            dtype=float,
            name=code,
        )
        frames[code] = s
    df = pd.DataFrame(frames).sort_index()
    df.index.name = "trade_date"
    return df.astype(float)


def _mom_12m_at(close: pd.Series, as_of: str, *, lookback: int = LOOKBACK_DAYS) -> float | None:
    hist = close.loc[:as_of].dropna()
    if len(hist) <= lookback:
        return None
    now = float(hist.iloc[-1])
    prev = float(hist.iloc[-1 - lookback])
    if prev <= 0:
        return None
    return (now / prev) - 1.0


def _rf_12m(rf_annual: float) -> float:
    return (1.0 + rf_annual) ** 1.0 - 1.0


def _pick_gem_asset(
    mom_0050: float | None,
    mom_00646: float | None,
    mom_bond: float | None,
    *,
    rf_annual: float,
    vigilant: bool = False,
) -> tuple[AssetCode, str]:
    """Antonacci GEM: abs filter → relative winner → else bonds."""
    rf12 = _rf_12m(rf_annual)
    if mom_0050 is None:
        return "CASH", "warmup"

    abs_equity_ok = mom_0050 > rf12
    if vigilant and mom_bond is not None:
        abs_equity_ok = abs_equity_ok and mom_bond > rf12

    if not abs_equity_ok:
        if mom_bond is not None and (mom_bond > rf12 or mom_0050 <= 0):
            return "00720B", "absolute off → bonds"
        return "CASH", "absolute off → cash"

    if mom_00646 is not None and mom_00646 > mom_0050:
        return "00646", "relative → intl"
    return "0050", "relative → TW"


def _allocate(
    strategy: StrategyId,
    *,
    mom_0050: float | None,
    mom_00646: float | None,
    mom_bond: float | None,
    rf_annual: float,
    risk_off_exposure: float,
) -> tuple[AssetCode, float, str, bool | None]:
    if strategy == "buy_hold_0050":
        return "0050", 1.0, "buy & hold", None

    abs_ok: bool | None = None
    if mom_0050 is not None:
        abs_ok = mom_0050 >= 0.0

    if strategy == "abs_circuit_breaker":
        if mom_0050 is None:
            return "CASH", 0.0, "warmup", None
        if mom_0050 >= 0:
            return "0050", 1.0, "12M≥0 full risk-on", True
        return "0050", risk_off_exposure, f"12M<0 → {risk_off_exposure:.0%} exposure", False

    if strategy == "abs_only_bond_switch":
        if mom_0050 is None:
            return "CASH", 0.0, "warmup", None
        rf12 = _rf_12m(rf_annual)
        if mom_0050 > rf12:
            return "0050", 1.0, "abs > RF", True
        if mom_bond is not None:
            return "00720B", 1.0, "abs ≤ RF → bonds", False
        return "CASH", 1.0, "abs ≤ RF → cash", False

    asset, note = _pick_gem_asset(
        mom_0050, mom_00646, mom_bond, rf_annual=rf_annual, vigilant=False
    )
    return asset, 1.0, note, abs_ok


def _daily_returns(close: pd.DataFrame) -> pd.DataFrame:
    return close.pct_change(fill_method=None).fillna(0.0)


def _compute_stats(daily_ret: pd.Series, bench_ret: pd.Series) -> dict[str, float | int | str]:
    eq = (1.0 + daily_ret).cumprod()
    bench_eq = (1.0 + bench_ret).cumprod()
    dd = eq / eq.cummax() - 1.0
    n = len(daily_ret)
    years = max(n / 252.0, 1e-9)
    total = float(eq.iloc[-1] - 1.0) * 100.0
    bench_total = float(bench_eq.iloc[-1] - 1.0) * 100.0
    cagr = (float(eq.iloc[-1]) ** (1.0 / years) - 1.0) * 100.0
    vol = float(daily_ret.std() * np.sqrt(252) * 100.0) if n > 1 else 0.0
    sharpe = float(daily_ret.mean() / daily_ret.std() * np.sqrt(252)) if daily_ret.std() > 0 else 0.0
    mdd = float(dd.min() * 100.0)
    calmar = cagr / abs(mdd) if mdd < 0 else float("nan")
    win_days = int((daily_ret > 0).sum())
    beat_bench = int((daily_ret > bench_ret).sum())
    return {
        "trading_days": n,
        "total_return_pct": round(total, 2),
        "bench_return_pct": round(bench_total, 2),
        "excess_return_pct": round(total - bench_total, 2),
        "cagr_pct": round(cagr, 2),
        "ann_vol_pct": round(vol, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(mdd, 2),
        "calmar": round(calmar, 2) if calmar == calmar else 0.0,
        "win_days": win_days,
        "beat_bench_days": beat_bench,
    }


def run_strategy_backtest(
    close: pd.DataFrame,
    *,
    strategy: StrategyId,
    start_date: str | None = None,
    end_date: str | None = None,
    rf_annual: float = DEFAULT_RF_ANNUAL,
    risk_off_exposure: float = DEFAULT_RISK_OFF_EXPOSURE,
    cash_daily_return: float = 0.0,
) -> DualMomentumResult:
    labels = {
        "buy_hold_0050": "0050 Buy & Hold",
        "gem_dual_momentum": "GEM 雙動能（0050/00646/00720B）",
        "abs_circuit_breaker": f"絕對動能熔斷（12M<0 → {risk_off_exposure:.0%}）",
        "abs_only_bond_switch": "純絕對動能（12M vs RF → 債券）",
    }
    rets = _daily_returns(close)
    dates = list(close.index)
    month_ends = month_end_trading_dates(dates)

    if start_date:
        dates = [d for d in dates if d >= start_date]
    if end_date:
        dates = [d for d in dates if d <= end_date]
    if not dates:
        raise ValueError("回測區間無交易日")
    if _mom_12m_at(close["0050"], dates[0]) is None:
        raise ValueError("資料不足以計算 12M 動能（需 start 前 252 交易日）")

    sched: dict[str, tuple[AssetCode, float, str, bool | None]] = {}
    rebalances: list[RebalanceRow] = []
    for me in month_ends:
        if me < dates[0] or me > dates[-1]:
            continue
        m0 = _mom_12m_at(close["0050"], me)
        m1 = _mom_12m_at(close["00646"], me) if "00646" in close.columns else None
        mb = _mom_12m_at(close["00720B"], me) if "00720B" in close.columns else None
        asset, exposure, note, abs_ok = _allocate(
            strategy,
            mom_0050=m0,
            mom_00646=m1,
            mom_bond=mb,
            rf_annual=rf_annual,
            risk_off_exposure=risk_off_exposure,
        )
        sched[me] = (asset, exposure, note, abs_ok)
        rebalances.append(
            RebalanceRow(
                signal_date=me,
                strategy=strategy,
                asset=asset,
                exposure=exposure,
                mom_0050_12m_pct=round(m0 * 100, 2) if m0 is not None else None,
                mom_00646_12m_pct=round(m1 * 100, 2) if m1 is not None else None,
                mom_bond_12m_pct=round(mb * 100, 2) if mb is not None else None,
                abs_ok=abs_ok,
                note=note,
            )
        )

    me_sorted = sorted(sched.keys())
    strat_ret: list[float] = []
    out_dates: list[str] = []
    cur_asset: AssetCode = "CASH"
    cur_exposure = 0.0

    # Warm-start: use last month-end signal on or before backtest start.
    for me in reversed(me_sorted):
        if me <= dates[0]:
            cur_asset, cur_exposure, _, _ = sched[me]
            break
    else:
        m0 = _mom_12m_at(close["0050"], dates[0])
        m1 = _mom_12m_at(close["00646"], dates[0]) if "00646" in close.columns else None
        mb = _mom_12m_at(close["00720B"], dates[0]) if "00720B" in close.columns else None
        cur_asset, cur_exposure, _, _ = _allocate(
            strategy,
            mom_0050=m0,
            mom_00646=m1,
            mom_bond=mb,
            rf_annual=rf_annual,
            risk_off_exposure=risk_off_exposure,
        )

    me_ptr = 0
    while me_ptr < len(me_sorted) and me_sorted[me_ptr] <= dates[0]:
        me_ptr += 1

    for d in dates:
        while me_ptr < len(me_sorted) and me_sorted[me_ptr] <= d:
            cur_asset, cur_exposure, _, _ = sched[me_sorted[me_ptr]]
            me_ptr += 1
        risky_code = cur_asset if cur_asset != "CASH" else "0050"
        r_risky = float(rets.loc[d, risky_code]) if risky_code in rets.columns else 0.0
        r_safe = cash_daily_return
        if cur_asset == "CASH":
            r = r_safe
        else:
            r = cur_exposure * r_risky + (1.0 - cur_exposure) * r_safe
        strat_ret.append(r)
        out_dates.append(d)

    daily = pd.DataFrame(
        {
            "strategy_return": strat_ret,
            "bench_return": [float(rets.loc[d, "0050"]) for d in out_dates],
        },
        index=pd.Index(out_dates, name="trade_date"),
    )
    stats = _compute_stats(daily["strategy_return"], daily["bench_return"])
    stats["start_date"] = out_dates[0]
    stats["end_date"] = out_dates[-1]
    stats["rebalance_count"] = len(rebalances)
    stats["strategy_id"] = strategy

    last_me = me_sorted[-1] if me_sorted else out_dates[-1]
    m0 = _mom_12m_at(close["0050"], last_me)
    latest = {
        "as_of": last_me,
        "0050_12m_pct": round(m0 * 100, 2) if m0 is not None else None,
        "abs_momentum_on": m0 is not None and m0 >= 0,
        "recommended_posture": "risk-on" if (m0 is not None and m0 >= 0) else "de-risk",
    }
    if rebalances:
        last = rebalances[-1]
        latest.update(
            {
                "allocation": last.asset,
                "exposure": last.exposure,
                "note": last.note,
            }
        )

    return DualMomentumResult(
        strategy=strategy,
        label=labels[strategy],
        start_date=out_dates[0],
        end_date=out_dates[-1],
        daily=daily,
        rebalances=rebalances,
        stats=stats,
        latest_signal=latest,
    )


def _default_backtest_start(close: pd.DataFrame) -> str:
    for d in close.index:
        if _mom_12m_at(close["0050"], d) is not None:
            return str(d)
    raise RuntimeError("無法找到 12M 動能 warmup 起點")


def run_all_scenarios(
    *,
    start: date,
    end: date,
    backtest_start: str | None = None,
    rf_annual: float = DEFAULT_RF_ANNUAL,
    risk_off_exposure: float = DEFAULT_RISK_OFF_EXPOSURE,
) -> tuple[pd.DataFrame, list[DualMomentumResult]]:
    close = load_etf_close_panel(["0050", "00646", "00720B"], start=start, end=end)
    close = close.dropna(how="any")
    if close.empty:
        raise RuntimeError("ETF close panel empty after alignment")

    bt_start = backtest_start or _default_backtest_start(close)
    cash_daily = (1.0 + rf_annual) ** (1.0 / 252.0) - 1.0
    strategies: list[StrategyId] = [
        "buy_hold_0050",
        "gem_dual_momentum",
        "abs_only_bond_switch",
        "abs_circuit_breaker",
    ]
    results: list[DualMomentumResult] = []
    for sid in strategies:
        results.append(
            run_strategy_backtest(
                close,
                strategy=sid,
                start_date=bt_start,
                rf_annual=rf_annual,
                risk_off_exposure=risk_off_exposure,
                cash_daily_return=cash_daily,
            )
        )

    summary_rows = []
    for r in results:
        summary_rows.append({"strategy": r.label, **r.stats})
    summary = pd.DataFrame(summary_rows)
    return summary, results
