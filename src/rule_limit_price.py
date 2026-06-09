"""規則參考買入價（E0 · 只讀技術快照）。"""

from __future__ import annotations

import math
from dataclasses import dataclass

from investment_policy import InvestmentPolicy, compute_limit_discount_pct
from market_labels import (
    ENTRY_BREAKOUT,
    ENTRY_OVEREXTENDED,
    ENTRY_PULLBACK,
    ENTRY_SKIP,
    ENTRY_WAIT,
)
from stock_context import TechnicalSnapshot


@dataclass(frozen=True)
class RefPriceResult:
    ref_price: float | None
    benchmark_type: str | None
    benchmark_price: float | None
    stop_price: float | None
    target_price: float | None
    skip_reason: str | None = None
    discount_pct: float | None = None
    pricing_note: str | None = None
    structural_stop_price: float | None = None

    @property
    def risk_reward(self) -> float | None:
        if (
            self.ref_price is None
            or self.stop_price is None
            or self.target_price is None
            or self.ref_price <= self.stop_price
        ):
            return None
        risk = self.ref_price - self.stop_price
        reward = self.target_price - self.ref_price
        if risk <= 0 or reward <= 0:
            return None
        return round(reward / risk, 2)


def twd_tick_size(price: float) -> float:
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def round_twd_tick(price: float) -> float:
    """台股價格 tick 取整（買入向下取 tick）。"""
    if price <= 0:
        return 0.0
    tick = twd_tick_size(price)
    return math.floor(price / tick + 1e-9) * tick


def round_twd_tick_up(price: float) -> float:
    """目標價向上取 tick，避免極窄風險帶時 R:R 被向下取整壓過門檻。"""
    if price <= 0:
        return 0.0
    tick = twd_tick_size(price)
    return math.ceil(price / tick - 1e-9) * tick


def _execution_stop_below_ref(
    ref: float,
    entry_signal: str,
    ips: InvestmentPolicy,
) -> float:
    """成交後參考停損：掛在限價下方（R:R / risk_budget 用）。"""
    pct = (
        ips.breakout_risk_pct
        if entry_signal == ENTRY_BREAKOUT
        else ips.stop_buffer_pct
    )
    return round_twd_tick(ref * (1.0 - pct / 100.0))


def _finalize_buy_prices(
    ref: float,
    structural_stop: float | None,
    entry_signal: str,
    ips: InvestmentPolicy,
    pricing_note: str | None,
) -> tuple[float | None, float | None, float | None, float | None, str | None]:
    """限價不抬高；結構停損僅供參考，執行停損在限價下方。"""
    if ref <= 0:
        return None, None, None, None, pricing_note
    structural = (
        round_twd_tick(structural_stop) if structural_stop is not None else None
    )
    exec_stop = _execution_stop_below_ref(ref, entry_signal, ips)
    target = _execution_target(ref, exec_stop, ips=ips)
    return ref, exec_stop, target, structural, pricing_note


def compute_ref_price(
    *,
    entry_signal: str,
    pm_bucket: str,
    tech: TechnicalSnapshot | None,
    ips: InvestmentPolicy,
    investment_score: float | None = None,
    beta: float | None = None,
    tx_gap_pct: float | None = None,
    tsm_adr_pct: float | None = None,
) -> RefPriceResult:
    if entry_signal in ips.exclude_entry_signals or entry_signal in {
        ENTRY_SKIP,
        ENTRY_OVEREXTENDED,
    }:
        return RefPriceResult(None, None, None, None, None, skip_reason=entry_signal)

    if tech is None or tech.close is None or tech.close <= 0:
        return RefPriceResult(None, None, None, None, None, skip_reason="無 K 線")

    prev_close = float(tech.close)
    ma20 = float(tech.ma20) if tech.ma20 else None
    high_52w = float(tech.high_52w) if tech.high_52w else None

    ref: float | None = None
    bench_type: str | None = None
    bench_price: float | None = None
    stop: float | None = None
    target: float | None = None

    anchor: float
    if entry_signal == ENTRY_BREAKOUT:
        candidates: list[tuple[str, float]] = [("prev_close", prev_close)]
        if high_52w:
            cap = high_52w * (1.0 + ips.breakout_buffer_pct / 100.0)
            candidates.append(("high_52w", cap))
        bench_type, bench_price = min(candidates, key=lambda x: x[1])
        anchor = bench_price
        stop = prev_close * (1.0 - ips.breakout_risk_pct / 100.0)
    elif entry_signal == ENTRY_PULLBACK:
        if ma20:
            bench_type, bench_price, anchor = "ma20", ma20, ma20
            stop = ma20 * (1.0 - ips.stop_buffer_pct / 100.0)
        else:
            bench_type = "prev_close"
            bench_price = prev_close
            anchor = prev_close
            stop = bench_price * (1.0 - ips.stop_buffer_pct / 100.0)
    elif entry_signal == ENTRY_WAIT:
        bench_type = "prev_close"
        bench_price = prev_close
        anchor = prev_close
        stop = bench_price * (1.0 - ips.stop_buffer_pct / 100.0)
    else:
        return RefPriceResult(None, None, None, None, None, skip_reason=entry_signal)

    discount_pct: float | None = None
    pricing_note: str | None = None
    if investment_score is not None:
        discount_pct, pricing_note = compute_limit_discount_pct(
            investment_score=investment_score,
            entry_signal=entry_signal,
            ips=ips,
            dist_from_52w_high_pct=tech.dist_from_52w_high_pct,
            beta=beta,
            tx_gap_pct=tx_gap_pct,
            tsm_adr_pct=tsm_adr_pct,
            atr14_pct=tech.atr14_pct,
            avg_range_pct_14d=tech.avg_range_pct_14d,
            realized_vol_pct_14d=tech.realized_vol_pct_14d,
        )
        ref = round_twd_tick(anchor * (1.0 - discount_pct / 100.0))
    elif entry_signal == ENTRY_PULLBACK and ma20 is None:
        ref = round_twd_tick(prev_close * (1.0 - ips.pull_buffer_pct / 100.0))
    elif entry_signal == ENTRY_WAIT:
        ref = round_twd_tick(prev_close * (1.0 - ips.wait_buffer_pct / 100.0))
    else:
        ref = round_twd_tick(anchor)
    ref, exec_stop, target, structural, pricing_note = _finalize_buy_prices(
        ref, stop, entry_signal, ips, pricing_note
    )
    if ref is None:
        return RefPriceResult(None, bench_type, bench_price, None, None, skip_reason="價格無效")

    return RefPriceResult(
        ref,
        bench_type,
        bench_price,
        exec_stop,
        target,
        discount_pct=discount_pct,
        pricing_note=pricing_note,
        structural_stop_price=structural,
    )


def _execution_target(
    ref: float,
    stop: float | None,
    *,
    ips: InvestmentPolicy,
) -> float | None:
    if stop is None or ref <= stop or ips.min_risk_reward <= 0:
        return None
    return round_twd_tick_up(ref + (ref - stop) * ips.min_risk_reward)


def compute_execution_prices(
    *,
    entry_signal: str,
    pm_bucket: str,
    tech: TechnicalSnapshot | None,
    ips: InvestmentPolicy,
    snapshot_price: float,
    investment_score: float | None = None,
    beta: float | None = None,
    tx_gap_pct: float | None = None,
    tsm_adr_pct: float | None = None,
) -> RefPriceResult:
    """Phase 2：anchor 用 snapshot；stop 固定於 as_of_date 技術位（§5.3）。"""
    if entry_signal in ips.exclude_entry_signals or entry_signal in {
        ENTRY_SKIP,
        ENTRY_OVEREXTENDED,
    }:
        return RefPriceResult(None, None, None, None, None, skip_reason=entry_signal)

    if tech is None or tech.close is None or tech.close <= 0:
        return RefPriceResult(None, None, None, None, None, skip_reason="無 K 線")

    if snapshot_price <= 0:
        return RefPriceResult(None, None, None, None, None, skip_reason="snapshot 無效")

    db_prev_close = float(tech.close)
    ma20 = float(tech.ma20) if tech.ma20 else None
    high_52w = float(tech.high_52w) if tech.high_52w else None

    bench_type: str | None = None
    bench_price: float | None = None
    anchor: float
    stop: float | None = None

    if entry_signal == ENTRY_BREAKOUT:
        candidates: list[tuple[str, float]] = [("snapshot", snapshot_price)]
        if high_52w:
            cap = high_52w * (1.0 + ips.breakout_buffer_pct / 100.0)
            candidates.append(("high_52w", cap))
        bench_type, bench_price = min(candidates, key=lambda x: x[1])
        anchor = bench_price
        stop = db_prev_close * (1.0 - ips.breakout_risk_pct / 100.0)
    elif entry_signal == ENTRY_PULLBACK:
        if ma20:
            bench_type, bench_price, anchor = "ma20", ma20, ma20
            stop = ma20 * (1.0 - ips.stop_buffer_pct / 100.0)
        else:
            bench_type, bench_price, anchor = "snapshot", snapshot_price, snapshot_price
            stop = db_prev_close * (1.0 - ips.stop_buffer_pct / 100.0)
    elif entry_signal == ENTRY_WAIT:
        bench_type, bench_price, anchor = "snapshot", snapshot_price, snapshot_price
        stop = db_prev_close * (1.0 - ips.stop_buffer_pct / 100.0)
    else:
        return RefPriceResult(None, None, None, None, None, skip_reason=entry_signal)

    discount_pct: float | None = None
    pricing_note: str | None = None
    if investment_score is not None:
        discount_pct, pricing_note = compute_limit_discount_pct(
            investment_score=investment_score,
            entry_signal=entry_signal,
            ips=ips,
            dist_from_52w_high_pct=tech.dist_from_52w_high_pct,
            beta=beta,
            tx_gap_pct=tx_gap_pct,
            tsm_adr_pct=tsm_adr_pct,
            atr14_pct=tech.atr14_pct,
            avg_range_pct_14d=tech.avg_range_pct_14d,
            realized_vol_pct_14d=tech.realized_vol_pct_14d,
        )
        ref = round_twd_tick(anchor * (1.0 - discount_pct / 100.0))
    elif entry_signal == ENTRY_PULLBACK and ma20 is None:
        ref = round_twd_tick(snapshot_price * (1.0 - ips.pull_buffer_pct / 100.0))
    elif entry_signal == ENTRY_WAIT:
        ref = round_twd_tick(snapshot_price * (1.0 - ips.wait_buffer_pct / 100.0))
    else:
        ref = round_twd_tick(anchor)

    ref, exec_stop, target, structural, pricing_note = _finalize_buy_prices(
        ref, stop, entry_signal, ips, pricing_note
    )
    if ref is None:
        return RefPriceResult(None, bench_type, bench_price, None, None, skip_reason="價格無效")

    return RefPriceResult(
        ref,
        bench_type,
        bench_price,
        exec_stop,
        target,
        discount_pct=discount_pct,
        pricing_note=pricing_note,
        structural_stop_price=structural,
    )
