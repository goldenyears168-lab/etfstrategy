#!/usr/bin/env python3
"""
雙引擎 Research Universe：Money Flow Top10 ∪ Event Top10（目標 15–20 檔）。
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from event_ranking import (
    DEFAULT_EVENTS_PATH,
    RankedEvent,
    events_path_hint,
    load_manual_events,
    rank_events,
)
from signal_engine import StockSignal, build_aligned_signals
from stock_db import DEFAULT_DB_PATH, connect, load_etf_constituent_watchlist

DEFAULT_ETF_CODES = (
    "00981A",
    "00403A",
    "009816",
    "00980A",
    "00982A",
    "00992A",
)
DEFAULT_TOP_N = 10
DEFAULT_MAX_POOL = 20


@dataclass(frozen=True)
class UniverseEntry:
    stock_id: str
    stock_name: str
    pool_reason: str  # money | event | both
    money_rank: int | None
    event_rank: int | None
    smart_money_score: float | None
    event_score: float | None
    headline: str | None = None


@dataclass(frozen=True)
class ResearchUniverseResult:
    prev_date: str | None
    curr_date: str | None
    etf_codes: tuple[str, ...]
    money_top: list[tuple[int, StockSignal, float]]
    event_top: list[RankedEvent]
    entries: list[UniverseEntry]

    @property
    def stock_ids(self) -> list[str]:
        return [e.stock_id for e in self.entries]


def smart_money_score(sig: StockSignal) -> float:
    """加碼側 Smart Money 子分（對齊 L2 共識 + L4 conviction + 權重/流量）。"""
    if sig.net_side != "add":
        return float("-inf")
    flow_term = 0.0
    if sig.flow_ntd_total is not None and sig.flow_ntd_total > 0:
        flow_term = min(2.0, (sig.flow_ntd_total / 1e9) ** 0.35)
    return (
        sig.consensus_score * 0.50
        + max(0.0, sig.conviction_score) * 0.35
        + sig.weight_delta_pp_max * 0.10
        + flow_term * 0.05
    )


def rank_money_flow(
    signals: list[StockSignal],
    *,
    top_n: int = DEFAULT_TOP_N,
) -> list[tuple[int, StockSignal, float]]:
    adds = [s for s in signals if s.net_side == "add"]
    scored = [(s, smart_money_score(s)) for s in adds]
    scored = [(s, sc) for s, sc in scored if sc > float("-inf")]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [(i + 1, s, sc) for i, (s, sc) in enumerate(scored[:top_n])]


def build_research_universe(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    top_n: int = DEFAULT_TOP_N,
    max_pool: int = DEFAULT_MAX_POOL,
    event_window_days: int = 7,
    events_path: Path | None = None,
) -> ResearchUniverseResult | None:
    aligned = build_aligned_signals(conn, etf_codes)
    if aligned is None:
        return None

    money_top = rank_money_flow(aligned.signals, top_n=top_n)
    watchlist = load_etf_constituent_watchlist(conn, etf_codes)
    pool_ids = {w["stock_id"] for w in watchlist} if watchlist else None
    events = load_manual_events(events_path)
    event_top = rank_events(
        events,
        top_n=top_n,
        window_days=event_window_days,
        pool_stock_ids=pool_ids,
    )

    name_by_id = {w["stock_id"]: w.get("stock_name", "") for w in watchlist}
    for _rank, sig, _sc in money_top:
        name_by_id.setdefault(sig.stock_id, sig.stock_name)

    entries_map: dict[str, UniverseEntry] = {}

    def _merge(
        stock_id: str,
        stock_name: str,
        *,
        reason_part: str,
        money_rank: int | None = None,
        event_rank: int | None = None,
        sm_score: float | None = None,
        ev_score: float | None = None,
        headline: str | None = None,
    ) -> None:
        cur = entries_map.get(stock_id)
        if cur is None:
            entries_map[stock_id] = UniverseEntry(
                stock_id=stock_id,
                stock_name=stock_name,
                pool_reason=reason_part,
                money_rank=money_rank,
                event_rank=event_rank,
                smart_money_score=sm_score,
                event_score=ev_score,
                headline=headline,
            )
            return
        if cur.pool_reason == reason_part:
            pool_reason = cur.pool_reason
        else:
            pool_reason = "both"
        entries_map[stock_id] = UniverseEntry(
            stock_id=stock_id,
            stock_name=stock_name or cur.stock_name,
            pool_reason=pool_reason,
            money_rank=money_rank or cur.money_rank,
            event_rank=event_rank or cur.event_rank,
            smart_money_score=sm_score if sm_score is not None else cur.smart_money_score,
            event_score=ev_score if ev_score is not None else cur.event_score,
            headline=headline or cur.headline,
        )

    for rank, sig, sc in money_top:
        _merge(
            sig.stock_id,
            sig.stock_name,
            reason_part="money",
            money_rank=rank,
            sm_score=round(sc, 3),
        )

    for row in event_top:
        _merge(
            row.stock_id,
            name_by_id.get(row.stock_id, ""),
            reason_part="event",
            event_rank=row.rank,
            ev_score=round(row.event_score, 3),
            headline=row.event.headline or None,
        )

    def _sort_key(e: UniverseEntry) -> tuple:
        in_both = 0 if e.pool_reason == "both" else 1
        best_rank = min(
            r for r in (e.money_rank, e.event_rank) if r is not None
        ) if (e.money_rank or e.event_rank) else 999
        return (in_both, best_rank, -(e.smart_money_score or 0), -(e.event_score or 0))

    entries = sorted(entries_map.values(), key=_sort_key)[:max_pool]

    return ResearchUniverseResult(
        prev_date=aligned.prev_date,
        curr_date=aligned.curr_date,
        etf_codes=aligned.etf_codes,
        money_top=money_top,
        event_top=event_top,
        entries=entries,
    )


def print_research_universe_report(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    top_n: int = DEFAULT_TOP_N,
    max_pool: int = DEFAULT_MAX_POOL,
    events_path=None,
    quiet: bool = False,
) -> ResearchUniverseResult | None:
    result = build_research_universe(
        conn,
        etf_codes,
        top_n=top_n,
        max_pool=max_pool,
        events_path=events_path,
    )
    print("")
    print("=== Research Universe（Money Top10 ∪ Event Top10）===")
    if result is None:
        print("  略過：無法建立對齊 cohort（需 ≥2 檔 ETF 同 prev→curr）")
        return None

    print(
        f"  窗口 {result.prev_date} → {result.curr_date}；"
        f"對齊 {','.join(result.etf_codes)}；"
        f"聯集 {len(result.entries)} 檔（上限 {max_pool}）"
    )
    if not result.event_top:
        print(f"  Event 通道：無事件（可編輯 {events_path_hint()}）")
    elif quiet:
        print(f"  Event Top{top_n}：{len(result.event_top)} 檔")

    print("")
    print(f"{'代號':>6} {'名稱':<8} {'來源':<6} {'MF#':>4} {'Ev#':>4} {'SM分':>6} {'Ev分':>6} 摘要")
    for e in result.entries:
        mf = str(e.money_rank) if e.money_rank else "—"
        ev = str(e.event_rank) if e.event_rank else "—"
        sm = f"{e.smart_money_score:.2f}" if e.smart_money_score is not None else "—"
        evs = f"{e.event_score:.2f}" if e.event_score is not None else "—"
        hint = (e.headline or "")[:28]
        print(
            f"  {e.stock_id:>6} {e.stock_name:<8} {e.pool_reason:<6} "
            f"{mf:>4} {ev:>4} {sm:>6} {evs:>6} {hint}"
        )
    return result


def parse_etf_codes(arg: str | None) -> tuple[str, ...]:
    if not arg:
        return DEFAULT_ETF_CODES
    return tuple(c.strip().upper() for c in arg.split(",") if c.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description="雙引擎 Research Universe 報告")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--max-pool", type=int, default=DEFAULT_MAX_POOL)
    parser.add_argument("--events-file", type=Path, default=DEFAULT_EVENTS_PATH)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    codes = parse_etf_codes(args.etf_codes)
    conn = connect(args.db)
    try:
        print_research_universe_report(
            conn,
            codes,
            top_n=args.top_n,
            max_pool=args.max_pool,
            events_path=args.events_file,
            quiet=args.quiet,
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
