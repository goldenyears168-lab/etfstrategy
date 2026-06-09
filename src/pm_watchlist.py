#!/usr/bin/env python3
"""開盤前觀察名單：收盤寫入 pm_watchlist，早盤只讀 + 風控摘要。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from entry_signal import EntryContext, is_overextended_without_strong_trend
from market_labels import (
    CHIP_NEUTRAL,
    CHIP_SYNC_BUY,
    ENTRY_BREAKOUT,
    ENTRY_SKIP,
    ENTRY_TAG_VOLUME,
    EXCLUDE_CHIP_TAGS,
    HIGH_CHIP_RESONANCE_TAGS,
    PM_AVOID,
    PM_BREAKOUT,
    PM_BUCKET_ORDER,
    PM_BUCKETS,
    PM_OBSERVE,
    WATCHLIST_ON_PM,
    format_entry_display,
)
from research_universe import UniverseEntry
from score_engine import SCORE_VERSION, ScoredEntry
from stock_context import build_chip_resonance
from stock_db import load_latest_pm_watchlist, upsert_pm_watchlist

HIGH_CHIP_SCORE_MIN = 70.0
EXCLUDE_ENTRY = frozenset({ENTRY_SKIP})


@dataclass(frozen=True)
class PmWatchEntry:
    stock_id: str
    stock_name: str
    as_of_date: str
    investment_score: float
    watchlist: str
    entry_signal: str
    entry_tags: tuple[str, ...]
    chip_tag: str
    pm_bucket: str
    flow_score: float
    chip_score: float
    tech_score: float
    catalyst_score: float
    fundamental_score: float
    note: str

    @property
    def entry_display(self) -> str:
        return format_entry_display(self.entry_signal, self.entry_tags)


def entry_context_from_scored(s: ScoredEntry) -> EntryContext:
    return EntryContext(s.entry_signal, s.entry_tags)


def has_high_chip_resonance(*, chip_tag: str, chip_score: float) -> bool:
    """籌碼維度或共振標籤達標（乖離過大仍可進觀察，觀察名單仍可不列入）。"""
    if chip_tag in HIGH_CHIP_RESONANCE_TAGS:
        return True
    return chip_score >= HIGH_CHIP_SCORE_MIN


def pm_bucket_for(
    *,
    on_list: bool,
    entry_ctx: EntryContext,
    chip_tag: str,
    chip_score: float = 0.0,
) -> str:
    if entry_ctx.signal in EXCLUDE_ENTRY or chip_tag in EXCLUDE_CHIP_TAGS:
        return PM_AVOID
    if is_overextended_without_strong_trend(entry_ctx):
        if on_list and has_high_chip_resonance(chip_tag=chip_tag, chip_score=chip_score):
            return PM_OBSERVE
        return PM_AVOID
    if entry_ctx.signal == ENTRY_BREAKOUT and on_list:
        return PM_BREAKOUT
    if on_list:
        return PM_OBSERVE
    return PM_AVOID


def qualifies_pm_list(
    scored: ScoredEntry,
    *,
    chip_tag: str,
    entry_ctx: EntryContext,
) -> bool:
    if entry_ctx.signal in EXCLUDE_ENTRY:
        return False
    if chip_tag in EXCLUDE_CHIP_TAGS:
        return False
    d = scored.dimensions
    if is_overextended_without_strong_trend(entry_ctx):
        if has_high_chip_resonance(chip_tag=chip_tag, chip_score=d.chip):
            return True
        return False
    if scored.watchlist in WATCHLIST_ON_PM:
        return True
    if d.chip >= 70.0 and d.flow >= 60.0:
        return True
    if chip_tag == CHIP_SYNC_BUY and d.flow >= 55.0:
        return True
    if ENTRY_TAG_VOLUME in entry_ctx.tags and d.flow >= 60.0:
        return True
    return False


def build_pm_entries(
    scored: list[ScoredEntry],
    *,
    as_of_date: str,
    chip_by_id: dict[str, str],
) -> list[PmWatchEntry]:
    rows: list[PmWatchEntry] = []
    for s in scored:
        e = s.entry
        chip_tag = chip_by_id.get(e.stock_id, CHIP_NEUTRAL)
        entry_ctx = entry_context_from_scored(s)
        on_list = qualifies_pm_list(s, chip_tag=chip_tag, entry_ctx=entry_ctx)
        bucket = pm_bucket_for(
            on_list=on_list,
            entry_ctx=entry_ctx,
            chip_tag=chip_tag,
            chip_score=s.dimensions.chip,
        )
        if not on_list and bucket != PM_AVOID:
            bucket = PM_AVOID
        note_parts = [chip_tag, entry_ctx.display, s.watchlist]
        rows.append(
            PmWatchEntry(
                stock_id=e.stock_id,
                stock_name=e.stock_name,
                as_of_date=as_of_date,
                investment_score=s.dimensions.investment_score,
                watchlist=s.watchlist,
                entry_signal=s.entry_signal,
                entry_tags=s.entry_tags,
                chip_tag=chip_tag,
                pm_bucket=bucket,
                flow_score=s.dimensions.flow,
                chip_score=s.dimensions.chip,
                tech_score=s.dimensions.timing,
                catalyst_score=s.dimensions.catalyst,
                fundamental_score=s.dimensions.fundamental,
                note=" · ".join(note_parts),
            )
        )
    rows.sort(
        key=lambda r: (
            PM_BUCKET_ORDER.get(r.pm_bucket, 9),
            -r.investment_score,
            r.stock_id,
        )
    )
    return rows


def pm_rows_for_db(entries: list[PmWatchEntry], *, score_version: str) -> list[dict]:
    return [
        {
            "stock_id": e.stock_id,
            "as_of_date": e.as_of_date,
            "score_version": score_version,
            "stock_name": e.stock_name,
            "investment_score": e.investment_score,
            "watchlist": e.watchlist,
            "entry_signal": e.entry_signal,
            "entry_tags_json": json.dumps(list(e.entry_tags), ensure_ascii=False),
            "chip_tag": e.chip_tag,
            "pm_bucket": e.pm_bucket,
            "flow_score": e.flow_score,
            "chip_score": e.chip_score,
            "tech_score": e.tech_score,
            "catalyst_score": e.catalyst_score,
            "fundamental_score": e.fundamental_score,
            "note": e.note,
        }
        for e in entries
    ]


def chip_tags_for_universe(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    entries: list[UniverseEntry],
) -> dict[str, str]:
    name_by_id = {e.stock_id: e.stock_name for e in entries}
    stock_ids = [e.stock_id for e in entries]
    return {
        r.stock_id: r.tag
        for r in build_chip_resonance(conn, etf_codes, stock_ids, name_by_id)
    }


def print_pm_watchlist_report(entries: list[PmWatchEntry], *, as_of_date: str) -> None:
    print("")
    print("=== 開盤前觀察名單（收盤後產出 · 早盤只讀）===")
    print(f"  基準日 {as_of_date} · 評分版本 {SCORE_VERSION}")
    observe = [e for e in entries if e.pm_bucket == PM_OBSERVE]
    breakout = [e for e in entries if e.pm_bucket == PM_BREAKOUT]
    avoid = [e for e in entries if e.pm_bucket == PM_AVOID]
    print(
        f"  摘要  列入觀察 {len(observe)} 檔 · 價量突破 {len(breakout)} 檔 · "
        f"不宜追價 {len(avoid)} 檔"
    )
    print(
        f"  {'代號':>6} {'名稱':<8} {'隔日等級':<8} {'價位型態':<20} {'綜合評分':>6} "
        f"{'觀察名單':<8} {'籌碼標籤':<16} 說明"
    )
    for e in entries:
        print(
            f"  {e.stock_id:>6} {e.stock_name:<8} {e.pm_bucket:<8} "
            f"{e.entry_display:<20} {e.investment_score:>6.1f} "
            f"{e.watchlist:<8} {e.chip_tag:<16} {e.note}"
        )
    if observe or breakout:
        print("  --- 隔日優先關注（觀察＋突破）---")
        for e in observe + breakout:
            print(
                f"  {e.stock_id:>6} {e.stock_name:<8} {e.pm_bucket:<8} "
                f"{e.entry_display:<20} 分 {e.investment_score:.1f}"
            )


def print_morning_pm_conclusion(conn: sqlite3.Connection) -> None:
    rows = load_latest_pm_watchlist(conn)
    print("")
    print("=== 開盤前執行摘要（前日觀察名單＋隔夜風險）===")
    if not rows:
        print("  — 尚無觀察名單（請先跑收盤 Score Engine + --sync-db）")
        return
    as_of = rows[0]["as_of_date"]
    observe = [r for r in rows if r["pm_bucket"] == PM_OBSERVE]
    breakout = [r for r in rows if r["pm_bucket"] == PM_BREAKOUT]
    avoid = [r for r in rows if r["pm_bucket"] == PM_AVOID]
    print(
        f"  名單基準日 {as_of}  →  "
        f"列入觀察 {len(observe)} 檔；價量突破 {len(breakout)} 檔；不宜追價 {len(avoid)} 檔"
    )
    if breakout:
        ids = "、".join(
            f"{r['stock_id']}({r['entry_signal']})" for r in breakout[:6]
        )
        print(f"  價量突破  {ids}")
    if observe:
        ids = "、".join(r["stock_id"] for r in observe[:8])
        print(f"  列入觀察  {ids}")
    if avoid:
        ids = "、".join(f"{r['stock_id']}" for r in avoid[:6])
        print(f"  不宜追價  {ids}")


def sync_pm_watchlist_from_scored(
    conn: sqlite3.Connection,
    scored: list[ScoredEntry],
    etf_codes: tuple[str, ...],
    as_of_date: str,
) -> list[PmWatchEntry]:
    if not scored:
        return []
    entries = [s.entry for s in scored]
    chip_by_id = chip_tags_for_universe(conn, etf_codes, entries)
    pm = build_pm_entries(scored, as_of_date=as_of_date, chip_by_id=chip_by_id)
    upsert_pm_watchlist(conn, pm_rows_for_db(pm, score_version=SCORE_VERSION))
    return pm
