#!/usr/bin/env python3
"""部位配置：Position / Risk / Portfolio Weight（規則 · 讀 Score + pm_watchlist）。"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass

from entry_signal import EntryContext, is_overextended_without_strong_trend
from market_labels import (
    ENTRY_SKIP,
    ENTRY_TAG_VOLUME,
    PM_ALLOC_BUCKETS,
    TIER_BASE_WEIGHT,
    WL_EXCLUDED,
    WL_PRIMARY,
    format_entry_display,
)
from investment_policy import InvestmentPolicy, load_investment_policy
from project_config import DEFAULT_CAPITAL_NTD
from score_engine import SCORE_VERSION, ScoredEntry
from stock_context import compute_technical
from stock_db import (
    load_latest_portfolio_weights,
    load_latest_tech_risk,
    load_stock_beta_map,
    upsert_portfolio_weights,
)


ALLOC_BUCKETS = PM_ALLOC_BUCKETS


@dataclass(frozen=True)
class PortfolioRow:
    stock_id: str
    stock_name: str
    as_of_date: str
    watchlist: str
    position_score: float
    risk_score: float
    portfolio_weight_pct: float
    suggested_ntd: float
    entry_signal: str
    entry_tags: tuple[str, ...]
    pm_bucket: str | None
    note: str


def _env_capital() -> float:
    raw = os.environ.get("PORTFOLIO_CAPITAL_NTD", "").strip()
    if not raw:
        return DEFAULT_CAPITAL_NTD
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_CAPITAL_NTD


def risk_score_for_stock(
    stock_id: str,
    *,
    entry_ctx: EntryContext,
    tech_risk_flag: str | None,
    beta_row: sqlite3.Row | None,
    tech,
) -> float:
    risk = 25.0
    if beta_row is not None and beta_row["beta"] is not None:
        beta = float(beta_row["beta"])
        if beta >= 1.6:
            risk += 25.0
        elif beta >= 1.3:
            risk += 15.0
    if tech is not None:
        if tech.dist_ma60_pct is not None and tech.dist_ma60_pct >= 18.0:
            risk += 20.0
        elif tech.dist_ma20_pct is not None and tech.dist_ma20_pct >= 18.0:
            risk += 12.0
    if is_overextended_without_strong_trend(entry_ctx):
        risk += 35.0
    if entry_ctx.signal == ENTRY_SKIP:
        risk += 40.0
    if tech_risk_flag == "TSM_ADR_LT_-2PCT":
        risk += 15.0
    if tech_risk_flag == "HIGH_BETA":
        risk += 10.0
    return round(min(100.0, risk), 1)


def position_score_for(
    scored: ScoredEntry,
    *,
    pm_bucket: str | None,
) -> float:
    base = scored.dimensions.investment_score
    if pm_bucket in ALLOC_BUCKETS:
        base = min(100.0, base + 3.0)
    if ENTRY_TAG_VOLUME in scored.entry_tags:
        base = min(100.0, base + 2.0)
    if scored.watchlist == WL_PRIMARY:
        base = min(100.0, base + 2.0)
    return round(base, 1)


def raw_weight_pct(
    scored: ScoredEntry,
    *,
    position_score: float,
    risk_score: float,
    pm_bucket: str | None,
    entry_ctx: EntryContext,
) -> float:
    if pm_bucket not in ALLOC_BUCKETS:
        return 0.0
    if is_overextended_without_strong_trend(entry_ctx):
        return 0.0
    if entry_ctx.signal == ENTRY_SKIP:
        return 0.0
    tier = TIER_BASE_WEIGHT.get(scored.watchlist, 0.0)
    if tier <= 0.0:
        return 0.0
    adj = (position_score / 100.0) * (1.0 - risk_score / 150.0)
    return max(0.0, tier * 100.0 * adj)


def _ips_pool_eligible(
    scored: ScoredEntry,
    *,
    raw: float,
    pm_bucket: str | None,
    ips: InvestmentPolicy,
) -> bool:
    if raw <= 0.0:
        return False
    if pm_bucket in ips.exclude_pm_buckets:
        return False
    if scored.entry_signal in ips.exclude_entry_signals:
        return False
    if scored.chip_tag in ips.exclude_chip_tags:
        return False
    return True


def _equal_position_weight_pct(ips: InvestmentPolicy) -> float:
    if ips.equal_position_weight_pct > 0:
        return ips.equal_position_weight_pct
    return 100.0 / max(1, ips.max_daily_positions)


def _rows_from_drafts(
    drafts: list[tuple[ScoredEntry, float, float, float, str | None, EntryContext, str]],
    *,
    as_of_date: str,
    weight_by_id: dict[str, float],
    capital: float,
    top_n: int | None = None,
) -> list[PortfolioRow]:
    rows: list[PortfolioRow] = []
    for s, pos, rsk, _raw, pm_bucket, _entry_ctx, note in drafts:
        sid = s.entry.stock_id
        pct = weight_by_id.get(sid, 0.0)
        ntd = capital * pct / 100.0 if pct > 0 else 0.0
        row_note = note
        if pct > 0 and top_n is not None:
            row_note = f"Top{top_n}等權 · {note}"
        rows.append(
            PortfolioRow(
                stock_id=sid,
                stock_name=s.entry.stock_name,
                as_of_date=as_of_date,
                watchlist=s.watchlist,
                position_score=pos,
                risk_score=rsk,
                portfolio_weight_pct=round(pct, 1),
                suggested_ntd=round(ntd, 0),
                entry_signal=s.entry_signal,
                entry_tags=s.entry_tags,
                pm_bucket=pm_bucket,
                note=row_note,
            )
        )
    rows.sort(key=lambda r: (-r.portfolio_weight_pct, -r.position_score, r.stock_id))
    return rows


def build_portfolio_rows(
    scored: list[ScoredEntry],
    *,
    as_of_date: str,
    conn: sqlite3.Connection,
    pm_bucket_by_id: dict[str, str],
    capital_ntd: float | None = None,
    ips: InvestmentPolicy | None = None,
) -> list[PortfolioRow]:
    policy = ips or load_investment_policy()
    capital = capital_ntd if capital_ntd is not None else _env_capital()
    beta_map, _ = load_stock_beta_map(conn)
    tech_risk = load_latest_tech_risk(conn)
    tsm_penalty = False
    if tech_risk is not None:
        tsm = tech_risk["tsm_daily_return_pct"]
        if tsm is not None and float(tsm) < -2.0:
            tsm_penalty = True

    drafts: list[tuple[ScoredEntry, float, float, float, str | None, EntryContext, str]] = []
    for s in scored:
        tech = compute_technical(conn, s.entry.stock_id)
        entry_ctx = EntryContext(s.entry_signal, s.entry_tags)
        pm_bucket = pm_bucket_by_id.get(s.entry.stock_id)
        pos = position_score_for(s, pm_bucket=pm_bucket)
        rsk = risk_score_for_stock(
            s.entry.stock_id,
            entry_ctx=entry_ctx,
            tech_risk_flag=s.tech_risk_flag,
            beta_row=beta_map.get(s.entry.stock_id),
            tech=tech,
        )
        raw = raw_weight_pct(
            s,
            position_score=pos,
            risk_score=rsk,
            pm_bucket=pm_bucket,
            entry_ctx=entry_ctx,
        )
        note_parts = [s.watchlist, entry_ctx.display]
        if pm_bucket:
            note_parts.append(pm_bucket)
        drafts.append((s, pos, rsk, raw, pm_bucket, entry_ctx, " · ".join(note_parts)))

    pool = [
        d
        for d in drafts
        if _ips_pool_eligible(d[0], raw=d[3], pm_bucket=d[4], ips=policy)
    ]
    scale = 0.70 if tsm_penalty and pool else 1.0

    if policy.daily_weight_mode == "equal":
        pool.sort(
            key=lambda d: (
                -d[0].dimensions.investment_score,
                d[0].entry.stock_id,
            )
        )
        top_n = min(policy.max_daily_positions, len(pool))
        eq_pct = _equal_position_weight_pct(policy) * scale
        top_ids = {d[0].entry.stock_id for d in pool[:top_n]}
        weight_by_id = {sid: eq_pct for sid in top_ids}
        return _rows_from_drafts(
            drafts,
            as_of_date=as_of_date,
            weight_by_id=weight_by_id,
            capital=capital,
            top_n=top_n if top_n > 0 else None,
        )

    total_raw = sum(d[3] for d in pool)
    weight_by_id: dict[str, float] = {}
    for s, _pos, _rsk, raw, _pm, _ctx, _note in drafts:
        sid = s.entry.stock_id
        in_pool = any(d[0].entry.stock_id == sid for d in pool)
        if not in_pool or total_raw <= 0:
            weight_by_id[sid] = 0.0
        else:
            weight_by_id[sid] = raw / total_raw * 100.0 * scale
    return _rows_from_drafts(
        drafts,
        as_of_date=as_of_date,
        weight_by_id=weight_by_id,
        capital=capital,
    )


def portfolio_rows_for_db(rows: list[PortfolioRow], *, score_version: str) -> list[dict]:
    return [
        {
            "stock_id": r.stock_id,
            "as_of_date": r.as_of_date,
            "score_version": score_version,
            "stock_name": r.stock_name,
            "watchlist": r.watchlist,
            "position_score": r.position_score,
            "risk_score": r.risk_score,
            "portfolio_weight_pct": r.portfolio_weight_pct,
            "suggested_ntd": r.suggested_ntd,
            "capital_ntd": _env_capital(),
            "entry_signal": r.entry_signal,
            "entry_tags_json": json.dumps(list(r.entry_tags), ensure_ascii=False),
            "pm_bucket": r.pm_bucket,
            "note": r.note,
        }
        for r in rows
    ]


def print_portfolio_report(
    rows: list[PortfolioRow],
    *,
    as_of_date: str,
    capital_ntd: float | None = None,
) -> None:
    capital = capital_ntd if capital_ntd is not None else _env_capital()
    ips = load_investment_policy()
    eq_pct = _equal_position_weight_pct(ips)
    mode_line = (
        f"Top{ips.max_daily_positions} 等權各 {eq_pct:.0f}%"
        if ips.daily_weight_mode == "equal"
        else "評分比例配置"
    )
    print("")
    print("=== 開盤前建議部位（部位分 · 風險分 · 建議權重）===")
    print(
        f"  基準日 {as_of_date} · 評分版本 {SCORE_VERSION} · 資金 {capital:,.0f} NTD · {mode_line}"
    )
    alloc = [r for r in rows if r.portfolio_weight_pct > 0]
    print(f"  建議配置 {len(alloc)} 檔 · 合計權重 {sum(r.portfolio_weight_pct for r in alloc):.1f}%")
    print(
        f"  {'代號':>6} {'名稱':<8} {'部位分':>5} {'風險分':>5} {'建議權重%':>8} "
        f"{'建議金額':>10} {'觀察名單':<8} {'價位型態':<20} 備註"
    )
    for r in rows:
        if r.portfolio_weight_pct <= 0 and r.watchlist == WL_EXCLUDED:
            continue
        entry_d = format_entry_display(r.entry_signal, r.entry_tags)
        print(
            f"  {r.stock_id:>6} {r.stock_name:<8} {r.position_score:>4.0f} "
            f"{r.risk_score:>4.0f} {r.portfolio_weight_pct:>5.1f}% "
            f"{r.suggested_ntd:>10,.0f} {r.watchlist:<8} {entry_d:<20} {r.note}"
        )


def print_morning_portfolio_summary(conn: sqlite3.Connection) -> None:
    rows = load_latest_portfolio_weights(conn)
    if not rows:
        return
    capital = float(rows[0]["capital_ntd"] or DEFAULT_CAPITAL_NTD)
    alloc = [r for r in rows if float(r["portfolio_weight_pct"] or 0) > 0]
    ips = load_investment_policy()
    eq_pct = _equal_position_weight_pct(ips)
    mode_line = (
        f"Top{ips.max_daily_positions}等權各{eq_pct:.0f}%"
        if ips.daily_weight_mode == "equal"
        else "比例配置"
    )
    print("")
    print(
        f"=== 開盤前建議部位（基準日 {rows[0]['as_of_date']} · 資金 {capital:,.0f} · {mode_line}）==="
    )
    if not alloc:
        print("  建議配置 0 檔（風控或不宜追價）")
        return
    parts = [
        f"{r['stock_id']} {float(r['portfolio_weight_pct']):.0f}%"
        for r in alloc[: ips.max_daily_positions]
    ]
    print(f"  建議配置 {len(alloc)} 檔  {' · '.join(parts)}")


def sync_portfolio_from_scored(
    conn: sqlite3.Connection,
    scored: list[ScoredEntry],
    *,
    as_of_date: str,
    pm_bucket_by_id: dict[str, str],
) -> list[PortfolioRow]:
    rows = build_portfolio_rows(
        scored,
        as_of_date=as_of_date,
        conn=conn,
        pm_bucket_by_id=pm_bucket_by_id,
    )
    upsert_portfolio_weights(
        conn, portfolio_rows_for_db(rows, score_version=SCORE_VERSION)
    )
    return rows
