"""Pre-trade 硬檢查（E0 · 只讀 DB + IPS + tech_risk）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from investment_policy import InvestmentPolicy, uses_risk_budget_sizing
from investment_themes import stock_theme
from project_config import NON_TECH_THEMES, SCORE_VERSION
from stock_db import (
    load_latest_pm_watchlist,
    load_latest_portfolio_weights,
    load_latest_tech_risk,
    load_tsm_adr_spread_before,
)

STATUS_DRAFT = "draft"
STATUS_BLOCKED = "blocked"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


@dataclass
class SyncHealth:
    ok: bool
    as_of_date: str | None
    message: str


@dataclass
class IntentDraft:
    trade_date: str
    as_of_date: str
    stock_id: str
    stock_name: str
    side: str
    ref_price: float
    limit_price: float
    qty: int
    suggested_ntd: float
    pm_bucket: str
    entry_signal: str
    entry_tags_json: str
    benchmark_type: str
    benchmark_price: float
    stop_price: float | None
    target_price: float | None
    score_version: str
    investment_score: float
    chip_tag: str
    discount_pct: float | None = None
    pricing_note: str = ""
    status: str = STATUS_DRAFT
    block_reason: str = ""
    order_type_planned: str = "pending_open"
    open_price: float | None = None
    order_type_effective: str | None = None
    price_snapshot: float | None = None
    open_gap_pct: float | None = None
    size_scale: float = 1.0
    price_snapshot_json: str = ""
    structural_stop_price: float | None = None


@dataclass
class PreTradeContext:
    sync: SyncHealth
    global_block: bool
    global_message: str
    tsm_adr_pct: float | None
    intents: list[IntentDraft] = field(default_factory=list)


def assess_sync_health(
    conn: sqlite3.Connection,
    *,
    trade_date: str,
    ips: InvestmentPolicy,
    score_version: str = SCORE_VERSION,
) -> SyncHealth:
    pm = load_latest_pm_watchlist(conn, score_version=score_version)
    pw = load_latest_portfolio_weights(conn, score_version=score_version)
    if not pm:
        return SyncHealth(False, None, "尚無 pm_watchlist（請先跑收盤 Score）")
    as_of = pm[0]["as_of_date"]
    if not pw:
        return SyncHealth(False, as_of, "尚無 portfolio_weights")
    pw_dates = {r["as_of_date"] for r in pw}
    if as_of not in pw_dates:
        return SyncHealth(False, as_of, "pm_watchlist 與 portfolio_weights 基準日不一致")
    if ips.require_evening_sync_ok and as_of >= trade_date:
        return SyncHealth(
            False,
            as_of,
            f"基準日 {as_of} 不早於交易日 {trade_date}（收盤鏈可能未更新）",
        )
    return SyncHealth(True, as_of, f"基準日 {as_of}")


def is_tech_theme(stock_id: str) -> bool:
    theme = stock_theme(stock_id)
    return theme not in NON_TECH_THEMES


def apply_pre_trade_checks(
    intents: list[IntentDraft],
    *,
    ips: InvestmentPolicy,
    sync: SyncHealth,
    tsm_adr_pct: float | None,
) -> PreTradeContext:
    ctx = PreTradeContext(
        sync=sync,
        global_block=not sync.ok,
        global_message=sync.message if not sync.ok else "",
        tsm_adr_pct=tsm_adr_pct,
        intents=intents,
    )
    if ctx.global_block:
        for it in intents:
            it.status = STATUS_BLOCKED
            it.block_reason = sync.message
        return ctx

    capital = ips.capital_ntd
    theme_ntd: dict[str, float] = {}

    for it in sorted(intents, key=lambda x: (-x.investment_score, x.stock_id)):
        if it.status == STATUS_BLOCKED and it.block_reason:
            continue
        if it.pm_bucket in ips.exclude_pm_buckets:
            it.status = STATUS_BLOCKED
            it.block_reason = f"pm_bucket={it.pm_bucket}"
            continue
        if it.entry_signal in ips.exclude_entry_signals:
            it.status = STATUS_BLOCKED
            it.block_reason = f"entry_signal={it.entry_signal}"
            continue
        if it.chip_tag in ips.exclude_chip_tags:
            it.status = STATUS_BLOCKED
            it.block_reason = f"chip_tag={it.chip_tag}"
            continue
        if capital > 0 and it.suggested_ntd / capital * 100 > ips.max_single_weight_pct:
            it.status = STATUS_BLOCKED
            it.block_reason = (
                f"單檔 {it.suggested_ntd / capital * 100:.1f}% > {ips.max_single_weight_pct}%"
            )
            continue
        if it.qty < 1:
            it.status = STATUS_BLOCKED
            if uses_risk_budget_sizing(ips):
                risk_ntd = ips.capital_ntd * ips.risk_budget_pct_per_trade / 100.0
                per_risk = (
                    f"，每股風險 {it.ref_price - it.stop_price:,.0f}"
                    if it.stop_price is not None
                    else ""
                )
                it.block_reason = (
                    f"risk_budget 張數不足（風險預算 {risk_ntd:,.0f} 元{per_risk}）"
                )
            else:
                it.block_reason = "股數 < 1"
            continue

        # R:R check uses stop/target computed at draft time
        rr = None
        if it.stop_price and it.target_price and it.ref_price > it.stop_price:
            risk = it.ref_price - it.stop_price
            reward = it.target_price - it.ref_price
            if risk > 0 and reward > 0:
                rr = reward / risk
        if rr is not None and rr < ips.min_risk_reward:
            it.status = STATUS_BLOCKED
            it.block_reason = f"R:R {rr:.2f} < {ips.min_risk_reward}"
            continue

        theme = stock_theme(it.stock_id)
        projected = theme_ntd.get(theme, 0.0) + it.suggested_ntd
        cap_theme = capital * ips.max_theme_weight_pct / 100.0
        if projected > cap_theme:
            it.status = STATUS_BLOCKED
            it.block_reason = (
                f"主題 {theme} 合計 {projected:,.0f} > {cap_theme:,.0f}"
            )
            continue
        theme_ntd[theme] = projected
        it.status = STATUS_DRAFT
        it.block_reason = ""

    ctx.intents = intents
    return ctx


def load_tsm_adr_pct(
    conn: sqlite3.Connection,
    trade_date: str | None = None,
) -> float | None:
    """開盤前 TSM 日報酬；snapshot 落後時以 daily_bars 補齊。"""
    from datetime import date as date_cls

    ref = trade_date or date_cls.today().isoformat()
    bar_date, bar_spread = load_tsm_adr_spread_before(conn, ref)
    row = load_latest_tech_risk(conn, trade_date=ref)
    if bar_spread is not None:
        if row is None:
            return bar_spread
        us_date = row["us_trade_date"] if "us_trade_date" in row.keys() else None
        if us_date is None or (bar_date and str(us_date) < bar_date):
            return bar_spread
    if row is None:
        return None
    val = row["tsm_daily_return_pct"]
    return float(val) if val is not None else None
