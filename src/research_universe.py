#!/usr/bin/env python3
"""
Research Universe：ETF 持股變化 Money Flow Top N（成分股聯集）。
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from project_config import (
    DEFAULT_ETF_CODES,
    DEFAULT_MAX_POOL,
    DEFAULT_TOP_N,
    parse_etf_codes,
)
from signal_engine import StockSignal, build_aligned_signals
from stock_db import DEFAULT_DB_PATH, connect, load_etf_constituent_watchlist


@dataclass(frozen=True)
class UniverseEntry:
    stock_id: str
    stock_name: str
    pool_reason: str  # money
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
    del event_window_days, events_path
    aligned = build_aligned_signals(conn, etf_codes)
    if aligned is None:
        return None

    money_top = rank_money_flow(aligned.signals, top_n=top_n)
    watchlist = load_etf_constituent_watchlist(conn, etf_codes)
    name_by_id = {w["stock_id"]: w.get("stock_name", "") for w in watchlist}
    for _rank, sig, _sc in money_top:
        name_by_id.setdefault(sig.stock_id, sig.stock_name)

    entries: list[UniverseEntry] = []
    for rank, sig, sc in money_top:
        entries.append(
            UniverseEntry(
                stock_id=sig.stock_id,
                stock_name=sig.stock_name,
                pool_reason="money",
                money_rank=rank,
                event_rank=None,
                smart_money_score=round(sc, 3),
                event_score=None,
                headline=None,
            )
        )

    entries = entries[:max_pool]

    return ResearchUniverseResult(
        prev_date=aligned.prev_date,
        curr_date=aligned.curr_date,
        etf_codes=aligned.etf_codes,
        money_top=money_top,
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
    del events_path, quiet
    result = build_research_universe(
        conn,
        etf_codes,
        top_n=top_n,
        max_pool=max_pool,
    )
    print("")
    print("=== Research Universe（Money Flow Top N）===")
    if result is None:
        print("  略過：無法建立對齊 cohort（需 ≥2 檔 ETF 同 prev→curr）")
        return None

    print(
        f"  窗口 {result.prev_date} → {result.curr_date}；"
        f"對齊 {','.join(result.etf_codes)}；"
        f"聯集 {len(result.entries)} 檔（上限 {max_pool}）"
    )

    print("")
    print(f"{'代號':>6} {'名稱':<8} {'來源':<6} {'MF#':>4} {'SM分':>6}")
    for e in result.entries:
        mf = str(e.money_rank) if e.money_rank else "—"
        sm = f"{e.smart_money_score:.2f}" if e.smart_money_score is not None else "—"
        print(f"  {e.stock_id:>6} {e.stock_name:<8} {e.pool_reason:<6} {mf:>4} {sm:>6}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Research Universe 報告（Money Flow）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--max-pool", type=int, default=DEFAULT_MAX_POOL)
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
            quiet=args.quiet,
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
