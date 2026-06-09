#!/usr/bin/env python3
"""
P4-v2 Score Engine：Smart Money(資金+籌碼) / 事件 / 預期 / 基本 / 風險 加權；
技術僅 ENTRY 閘門，不進 Investment Score。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from entry_signal import (
    EntryContext,
    classify_entry_context,
    classify_entry_context_batch,
    extension_pct,
    is_overextended_without_strong_trend,
    overextended_min_pct,
)
from event_ranking import (
    is_index_rebalance_headline,
    is_index_rebalance_event,
    load_catalyst_events_from_db,
    score_event,
)
from market_labels import (
    CHIP_NEUTRAL,
    CHIP_TAG_SCORE,
    ENTRY_BREAKOUT,
    ENTRY_OVEREXTENDED,
    ENTRY_PULLBACK,
    ENTRY_SKIP,
    ENTRY_TAG_VOLUME,
    ENTRY_WAIT,
    VOL_DOWN,
    VOL_SURGE,
    VOL_UP,
    WL_CANDIDATE,
    WL_EXCLUDED,
    WL_GENERAL,
    WL_PRIMARY,
    format_entry_display,
)
from investment_themes import stock_theme
from market_analytics import analytics_entry_tags, build_stock_analytics
from project_config import (
    CATALYST_BASELINE_EVENT,
    CATALYST_BASELINE_MONEY,
    CATALYST_LOW_CONF_CAP,
    CATALYST_UNCONFIRMED_CAP,
    DEFAULT_ETF_CODES,
    FLOW_NO_SIGNAL,
    NEUTRAL_SUBSCORE,
    NON_TECH_THEMES,
    ROLE_WEIGHT,
    SCORE_VERSION,
    SMART_MONEY_CHIP_BLEND,
    SMART_MONEY_FLOW_BLEND,
    WEIGHT_CATALYST,
    WEIGHT_EXPECTATION,
    WEIGHT_FUNDAMENTAL,
    WEIGHT_RISK,
    WEIGHT_SMART_MONEY,
    parse_etf_codes,
)
from research_universe import (
    DEFAULT_EVENTS_PATH,
    UniverseEntry,
    build_research_universe,
)
from signal_engine import StockSignal, build_aligned_signals
from expectation_engine import SubscoreResult, load_subscores_for_stocks
from stock_context import (
    TechnicalSnapshot,
    build_chip_resonance,
    compute_technical,
    load_latest_institutional,
)
from stock_db import (
    DEFAULT_DB_PATH,
    connect,
    load_latest_tech_risk,
    load_stock_beta_map,
    upsert_investment_scores,
)

@dataclass(frozen=True)
class DimensionScores:
    """flow/chip 為分量；smart_money 為合成子分（進加權）。"""

    flow: float
    chip: float
    catalyst: float
    expectation: float
    fundamental: float
    risk: float
    timing: float

    @property
    def smart_money(self) -> float:
        raw = (
            SMART_MONEY_FLOW_BLEND * self.flow
            + SMART_MONEY_CHIP_BLEND * self.chip
        )
        return round(min(100.0, raw), 1)

    @property
    def investment_score(self) -> float:
        raw = (
            WEIGHT_SMART_MONEY * self.smart_money
            + WEIGHT_CATALYST * self.catalyst
            + WEIGHT_EXPECTATION * self.expectation
            + WEIGHT_FUNDAMENTAL * self.fundamental
            + WEIGHT_RISK * self.risk
        )
        return round(raw, 1)


@dataclass(frozen=True)
class ScoredEntry:
    entry: UniverseEntry
    dimensions: DimensionScores
    watchlist: str
    position_intent: str | None
    tech_risk_flag: str | None
    entry_signal: str
    entry_tags: tuple[str, ...]
    chip_tag: str
    metadata: dict


def _scale_minmax(pool: list[float], value: float) -> float:
    if not pool:
        return 50.0
    lo, hi = min(pool), max(pool)
    if hi <= lo:
        return 50.0
    return 100.0 * (value - lo) / (hi - lo)


def _is_tech_theme(theme: str) -> bool:
    return theme not in NON_TECH_THEMES


def watchlist_tier(
    investment_score: float,
    smart_money: float,
    *,
    entry_ctx: EntryContext,
) -> str:
    if entry_ctx.signal == ENTRY_SKIP:
        return WL_EXCLUDED
    qualifies_a = investment_score >= 75.0 and smart_money >= 72.0
    if qualifies_a:
        if is_overextended_without_strong_trend(entry_ctx):
            return WL_GENERAL
        return WL_PRIMARY
    if investment_score >= 65.0:
        return WL_GENERAL
    if investment_score >= 55.0:
        return WL_CANDIDATE
    return WL_EXCLUDED


def _rescored_with_entry_ctx(
    scored: ScoredEntry,
    entry_ctx: EntryContext,
    tech: TechnicalSnapshot | None,
    *,
    overextended_min: float | None = None,
) -> ScoredEntry:
    timing = timing_score(tech, entry_ctx=entry_ctx)
    d = scored.dimensions
    dims = DimensionScores(
        flow=d.flow,
        chip=d.chip,
        catalyst=d.catalyst,
        expectation=d.expectation,
        fundamental=d.fundamental,
        risk=d.risk,
        timing=timing,
    )
    wl = watchlist_tier(dims.investment_score, dims.smart_money, entry_ctx=entry_ctx)
    meta = dict(scored.metadata)
    meta["entry_signal"] = entry_ctx.signal
    meta["entry_tags"] = list(entry_ctx.tags)
    meta["timing_score"] = timing
    if overextended_min is not None:
        meta["overextended_min_pct"] = round(overextended_min, 2)
    return ScoredEntry(
        entry=scored.entry,
        dimensions=dims,
        watchlist=wl,
        position_intent=scored.position_intent,
        tech_risk_flag=scored.tech_risk_flag,
        entry_signal=entry_ctx.signal,
        entry_tags=entry_ctx.tags,
        chip_tag=scored.chip_tag,
        metadata=meta,
    )


def catalyst_subscore(entry: UniverseEntry) -> float:
    if entry.headline and is_index_rebalance_headline(entry.headline):
        if entry.pool_reason == "money":
            return CATALYST_BASELINE_MONEY
        return CATALYST_BASELINE_EVENT
    if entry.event_score is not None:
        return round(min(100.0, entry.event_score * 100.0), 1)
    if entry.pool_reason == "money":
        return CATALYST_BASELINE_MONEY
    return CATALYST_BASELINE_EVENT


def _industry_catalyst_score(
    conn: sqlite3.Connection,
    stock_id: str,
) -> float | None:
    best = 0.0
    for ev in load_catalyst_events_from_db(conn, stock_ids={stock_id}):
        if is_index_rebalance_event(ev):
            continue
        best = max(best, score_event(ev))
    return round(min(100.0, best * 100.0), 1) if best > 0 else None


def _max_catalyst_confidence_industry(
    conn: sqlite3.Connection,
    stock_id: str,
) -> int | None:
    best: int | None = None
    for ev in load_catalyst_events_from_db(conn, stock_ids={stock_id}):
        if is_index_rebalance_event(ev):
            continue
        best = ev.confidence if best is None else max(best, ev.confidence)
    return best


def catalyst_subscore_capped(
    entry: UniverseEntry,
    conn: sqlite3.Connection,
) -> float:
    industry = _industry_catalyst_score(conn, entry.stock_id)
    base = industry if industry is not None else catalyst_subscore(entry)
    mx = _max_catalyst_confidence_industry(conn, entry.stock_id)
    if mx is None:
        return base
    if mx < 50:
        return min(base, CATALYST_UNCONFIRMED_CAP)
    if mx < 70:
        return min(base, CATALYST_LOW_CONF_CAP)
    return base


def flow_subscore(
    entry: UniverseEntry,
    signal_by_id: dict[str, StockSignal],
    *,
    conv_pool: list[float],
    cons_pool: list[float],
) -> float:
    sig = signal_by_id.get(entry.stock_id)
    if sig is None:
        return FLOW_NO_SIGNAL
    nc = _scale_minmax(conv_pool, sig.conviction_score)
    ns = _scale_minmax(cons_pool, sig.consensus_score)
    rot = 100.0 if (sig.rotation_in or sig.rotation_out) else 0.0
    role = ROLE_WEIGHT.get(sig.portfolio_role, 0.5) * 100.0
    raw = 0.40 * nc + 0.40 * ns + 0.10 * rot + 0.10 * role
    score = min(100.0, round(raw, 1))
    if sig.net_side != "add":
        score = min(score, 35.0)
    return score


def chip_subscore_for_stock(
    conn: sqlite3.Connection,
    stock_id: str,
    etf_codes: tuple[str, ...],
    name_by_id: dict[str, str],
) -> tuple[float, str]:
    rows = build_chip_resonance(conn, etf_codes, [stock_id], name_by_id)
    tag = rows[0].tag if rows else CHIP_NEUTRAL
    base = CHIP_TAG_SCORE.get(tag, NEUTRAL_SUBSCORE)
    inst = load_latest_institutional(conn, stock_id)
    if inst and inst.foreign_net is not None and inst.foreign_net > 0:
        if inst.investment_trust_net is not None and inst.investment_trust_net > 0:
            base = min(100.0, base + 3.0)
        else:
            base = min(100.0, base + 1.0)
    return round(base, 1), tag


def timing_score(
    tech: TechnicalSnapshot | None,
    *,
    entry_ctx: EntryContext,
) -> float:
    """進場適配度（僅報告/metadata，不進 Investment Score 加權）。"""
    if tech is None:
        return 45.0
    if entry_ctx.signal == ENTRY_OVEREXTENDED:
        if ENTRY_TAG_VOLUME in entry_ctx.tags:
            return 62.0
        return 32.0
    if entry_ctx.signal == ENTRY_BREAKOUT:
        return 88.0
    if entry_ctx.signal == ENTRY_PULLBACK:
        return 78.0
    if entry_ctx.signal == ENTRY_SKIP:
        return 30.0
    if tech.vol_label in (VOL_SURGE, VOL_UP):
        return 62.0
    if tech.vol_label == VOL_DOWN:
        return 48.0
    return 55.0


def risk_subscore(
    stock_id: str,
    *,
    beta_row: sqlite3.Row | None,
    tech_risk: sqlite3.Row | None,
) -> tuple[float, str | None]:
    score = 70.0
    flag: str | None = None
    if beta_row is not None and beta_row["beta"] is not None:
        beta = float(beta_row["beta"])
        if beta >= 1.6:
            score -= 25.0
        elif beta >= 1.3:
            score -= 15.0
        elif beta < 0.8:
            score += 5.0
    theme = stock_theme(stock_id)
    if tech_risk is not None and _is_tech_theme(theme):
        tsm_ret = tech_risk["tsm_daily_return_pct"]
        if tsm_ret is not None and float(tsm_ret) < -2.0:
            score -= 20.0
            flag = "TSM_ADR_LT_-2PCT"
    if beta_row is not None and beta_row["beta"] is not None:
        if float(beta_row["beta"]) >= 1.6 and flag is None:
            flag = "HIGH_BETA"
    return round(max(0.0, min(100.0, score)), 1), flag


def score_universe_entry(
    entry: UniverseEntry,
    *,
    signal_by_id: dict[str, StockSignal],
    conv_pool: list[float],
    cons_pool: list[float],
    beta_map: dict[str, sqlite3.Row],
    tech_risk: sqlite3.Row | None,
    conn: sqlite3.Connection,
    trade_date: str | None,
    etf_codes: tuple[str, ...],
    name_by_id: dict[str, str],
    expectation: SubscoreResult | None = None,
    fundamental: SubscoreResult | None = None,
) -> ScoredEntry:
    del trade_date
    sig = signal_by_id.get(entry.stock_id)
    tech = compute_technical(conn, entry.stock_id)
    flow = flow_subscore(
        entry,
        signal_by_id,
        conv_pool=conv_pool,
        cons_pool=cons_pool,
    )
    chip, chip_tag = chip_subscore_for_stock(
        conn, entry.stock_id, etf_codes, name_by_id
    )
    entry_ctx = classify_entry_context(
        tech,
        net_side=sig.net_side if sig else None,
        flow_score=flow,
        chip_score=chip,
    )
    analytics = build_stock_analytics(
        conn,
        entry.stock_id,
        tech=tech,
        entry_signal=entry_ctx.signal,
    )
    extra_tags = analytics_entry_tags(analytics)
    if extra_tags:
        entry_ctx = EntryContext(
            entry_ctx.signal,
            entry_ctx.tags + tuple(extra_tags),
        )
    timing = timing_score(tech, entry_ctx=entry_ctx)
    cat = catalyst_subscore_capped(entry, conn)
    fund_res = fundamental or SubscoreResult(NEUTRAL_SUBSCORE, "DATA_MISSING", {})
    fund = fund_res.score
    exp_res = expectation or SubscoreResult(NEUTRAL_SUBSCORE, "DATA_MISSING", {})
    exp = exp_res.score
    risk, tech_flag = risk_subscore(
        entry.stock_id,
        beta_row=beta_map.get(entry.stock_id),
        tech_risk=tech_risk,
    )
    dims = DimensionScores(
        flow=flow,
        chip=chip,
        catalyst=cat,
        expectation=exp,
        fundamental=fund,
        risk=risk,
        timing=timing,
    )
    wl = watchlist_tier(
        dims.investment_score,
        dims.smart_money,
        entry_ctx=entry_ctx,
    )
    meta = {
        "expectation": exp_res.status,
        "fundamental": fund_res.status,
        "expectation_detail": exp_res.detail,
        "fundamental_detail": fund_res.detail,
        "entry_signal": entry_ctx.signal,
        "entry_tags": list(entry_ctx.tags),
        "timing_score": timing,
        "chip_tag": chip_tag,
        "flow_score": flow,
        "chip_score": chip,
        "score_version": SCORE_VERSION,
        "weights": {
            "smart_money": WEIGHT_SMART_MONEY,
            "catalyst": WEIGHT_CATALYST,
            "expectation": WEIGHT_EXPECTATION,
            "fundamental": WEIGHT_FUNDAMENTAL,
            "risk": WEIGHT_RISK,
            "smart_money_blend": {
                "flow": SMART_MONEY_FLOW_BLEND,
                "chip": SMART_MONEY_CHIP_BLEND,
            },
        },
        "timing_not_in_total": True,
        "analytics": analytics.to_dict(),
    }
    if tech_flag:
        meta["risk_gate"] = tech_flag
    return ScoredEntry(
        entry=entry,
        dimensions=dims,
        watchlist=wl,
        position_intent=sig.position_intent if sig else None,
        tech_risk_flag=tech_flag,
        entry_signal=entry_ctx.signal,
        entry_tags=entry_ctx.tags,
        chip_tag=chip_tag,
        metadata=meta,
    )


def build_signal_map(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> dict[str, StockSignal]:
    aligned = build_aligned_signals(conn, etf_codes)
    if aligned is None:
        return {}
    return {s.stock_id: s for s in aligned.signals}


def run_score_engine(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    events_path: Path | None = None,
    top_n: int = 10,
    max_pool: int = 20,
) -> tuple[list[ScoredEntry], str | None] | None:
    universe = build_research_universe(
        conn,
        etf_codes,
        top_n=top_n,
        max_pool=max_pool,
        events_path=events_path,
    )
    if universe is None or not universe.entries:
        return None

    signal_by_id = build_signal_map(conn, etf_codes)
    signals_in_pool = [
        signal_by_id[e.stock_id]
        for e in universe.entries
        if e.stock_id in signal_by_id
    ]
    conv_pool = [s.conviction_score for s in signals_in_pool]
    cons_pool = [s.consensus_score for s in signals_in_pool]

    beta_map, _ = load_stock_beta_map(conn)
    tech_risk = load_latest_tech_risk(conn)
    trade_date = universe.curr_date
    stock_ids = [e.stock_id for e in universe.entries]
    name_by_id = {e.stock_id: e.stock_name for e in universe.entries}
    exp_map, fund_map = load_subscores_for_stocks(conn, stock_ids)

    scored = [
        score_universe_entry(
            e,
            signal_by_id=signal_by_id,
            conv_pool=conv_pool,
            cons_pool=cons_pool,
            beta_map=beta_map,
            tech_risk=tech_risk,
            conn=conn,
            trade_date=trade_date,
            etf_codes=etf_codes,
            name_by_id=name_by_id,
            expectation=exp_map.get(e.stock_id),
            fundamental=fund_map.get(e.stock_id),
        )
        for e in universe.entries
    ]

    tech_by_id: dict[str, TechnicalSnapshot | None] = {
        e.stock_id: compute_technical(conn, e.stock_id) for e in universe.entries
    }
    batch_items: list[
        tuple[str, TechnicalSnapshot | None, str | None, float | None, float | None]
    ] = []
    scored_by_id = {s.entry.stock_id: s for s in scored}
    for e in universe.entries:
        prev = scored_by_id[e.stock_id]
        sig = signal_by_id.get(e.stock_id)
        batch_items.append(
            (
                e.stock_id,
                tech_by_id[e.stock_id],
                sig.net_side if sig else None,
                prev.dimensions.flow,
                prev.dimensions.chip,
            )
        )
    extensions = [
        ext
        for tech in tech_by_id.values()
        if (ext := extension_pct(tech)) is not None
    ]
    ext_min = overextended_min_pct(extensions)
    ctx_map = classify_entry_context_batch(batch_items)
    scored = [
        _rescored_with_entry_ctx(
            scored_by_id[e.stock_id],
            ctx_map[e.stock_id],
            tech_by_id[e.stock_id],
            overextended_min=ext_min,
        )
        for e in universe.entries
    ]
    scored.sort(key=lambda x: x.dimensions.investment_score, reverse=True)
    return scored, universe.curr_date


def scored_rows_for_db(
    scored: list[ScoredEntry],
    as_of_date: str,
) -> list[dict]:
    rows: list[dict] = []
    for s in scored:
        e = s.entry
        d = s.dimensions
        rows.append(
            {
                "stock_id": e.stock_id,
                "as_of_date": as_of_date,
                "score_version": SCORE_VERSION,
                "stock_name": e.stock_name,
                "smart_money": d.smart_money,
                "catalyst": d.catalyst,
                "expectation": d.expectation,
                "fundamental": d.fundamental,
                "risk": d.risk,
                "investment_score": d.investment_score,
                "watchlist": s.watchlist,
                "pool_reason": e.pool_reason,
                "money_rank": e.money_rank,
                "event_rank": e.event_rank,
                "position_intent": s.position_intent,
                "tech_risk_flag": s.tech_risk_flag,
                "metadata_json": json.dumps(s.metadata, ensure_ascii=False),
            }
        )
    return rows


def print_score_report(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    events_path: Path | None = None,
    top_n: int = 10,
    max_pool: int = 20,
    quiet: bool = False,
    human: bool = False,
    sync_pm: bool = False,
) -> tuple[list[ScoredEntry], str] | None:
    result = run_score_engine(
        conn,
        etf_codes,
        events_path=events_path,
        top_n=top_n,
        max_pool=max_pool,
    )
    if result is None:
        if not human and not quiet:
            print("")
            print(
                "=== 綜合研究評分（p4-v2 · 資金籌碼50／催化10／預期15／基本15／風險10 · 技術僅作價位參考）==="
            )
            print("  略過：無 Research Universe（需對齊 cohort）")
        return None

    scored, as_of_date = result
    if as_of_date is None:
        if not human and not quiet:
            print("")
            print(
                "=== 綜合研究評分（p4-v2 · 資金籌碼50／催化10／預期15／基本15／風險10 · 技術僅作價位參考）==="
            )
            print("  略過：無 curr_date")
        return None

    if human:
        if sync_pm:
            from pm_watchlist import sync_pm_watchlist_from_scored
            from portfolio_engine import sync_portfolio_from_scored

            pm = sync_pm_watchlist_from_scored(conn, scored, etf_codes, as_of_date)
            pm_bucket_by_id = {p.stock_id: p.pm_bucket for p in pm}
            sync_portfolio_from_scored(
                conn, scored, as_of_date=as_of_date, pm_bucket_by_id=pm_bucket_by_id
            )
        return scored, as_of_date

    print("")
    print(
        "=== 綜合研究評分（p4-v2 · 資金籌碼50／催化10／預期15／基本15／風險10 · 技術僅作價位參考）==="
    )
    print(f"  基準日 {as_of_date} · 評分版本 {SCORE_VERSION} · {len(scored)} 檔")
    if tech := load_latest_tech_risk(conn):
        tsm = tech["tsm_daily_return_pct"]
        tsm_s = f"{tsm:+.2f}%" if tsm is not None else "—"
        if not quiet:
            print(f"  tech_risk session={tech['session_date']} TSM ADR {tsm_s}")

    print("")
    print(
        f"{'代號':>6} {'名稱':<8} {'綜合評分':>6} {'觀察名單':<8} {'價位型態':<20} "
        f"{'資金籌碼':>6} {'ETF流向':>6} {'法人籌碼':>6} {'預期差':>6} {'基本面':>6} "
        f"{'風險面':>6} {'價位分':>6} {'籌碼標籤':<16}"
    )
    for s in scored:
        e = s.entry
        d = s.dimensions
        entry_d = format_entry_display(s.entry_signal, s.entry_tags)
        print(
            f"  {e.stock_id:>6} {e.stock_name:<8} {d.investment_score:>6.1f} "
            f"{s.watchlist:<8} {entry_d:<20} "
            f"{d.smart_money:>6.0f} {d.flow:>6.0f} {d.chip:>6.0f} "
            f"{d.expectation:>6.0f} {d.fundamental:>6.0f} {d.risk:>6.0f} "
            f"{d.timing:>6.0f} {s.chip_tag:<16}"
        )
    primary_count = sum(1 for s in scored if s.watchlist == WL_PRIMARY)
    print(
        f"  首要觀察 {primary_count} 檔（綜合評分≥75 且資金籌碼≥72；"
        f"乖離過大且非量價齊揚者最高僅列一般觀察；價位分不計入綜合評分）"
    )

    if sync_pm:
        from pm_watchlist import print_pm_watchlist_report, sync_pm_watchlist_from_scored
        from portfolio_engine import print_portfolio_report, sync_portfolio_from_scored

        pm = sync_pm_watchlist_from_scored(conn, scored, etf_codes, as_of_date)
        print_pm_watchlist_report(pm, as_of_date=as_of_date)
        pm_bucket_by_id = {p.stock_id: p.pm_bucket for p in pm}
        pf = sync_portfolio_from_scored(
            conn, scored, as_of_date=as_of_date, pm_bucket_by_id=pm_bucket_by_id
        )
        print_portfolio_report(pf, as_of_date=as_of_date)

    return scored, as_of_date


def main() -> int:
    parser = argparse.ArgumentParser(description="P4-v2 綜合研究評分 + 開盤前觀察名單")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--max-pool", type=int, default=20)
    parser.add_argument("--events-file", type=Path, default=DEFAULT_EVENTS_PATH)
    parser.add_argument(
        "--sync-db",
        action="store_true",
        help="寫入 investment_scores + pm_watchlist + portfolio_weights",
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--human",
        action="store_true",
        help="收盤 digest 模式：只寫 DB，不印寬表",
    )
    args = parser.parse_args()

    codes = parse_etf_codes(args.etf_codes)
    conn = connect(args.db)
    try:
        out = print_score_report(
            conn,
            codes,
            events_path=args.events_file,
            top_n=args.top_n,
            max_pool=args.max_pool,
            quiet=args.quiet,
            human=args.human,
            sync_pm=args.sync_db,
        )
        if out and args.sync_db:
            scored, as_of = out
            n = upsert_investment_scores(
                conn, scored_rows_for_db(scored, as_of)
            )
            if not args.human:
                print(f"  DB：investment_scores upsert {n} 列")
                try:
                    pw = conn.execute(
                        "SELECT COUNT(*) AS c FROM portfolio_weights WHERE as_of_date=?",
                        (as_of,),
                    ).fetchone()["c"]
                    print(f"  DB：portfolio_weights {pw} 列（as_of={as_of}）")
                except sqlite3.OperationalError:
                    pass
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
