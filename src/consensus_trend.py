"""跨 ETF 共識時間序列（持股快照 · 加碼 ETF 檔數趨勢）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from holdings_research import ADD_ACTIONS, AlignedCohort
from stock_db import compute_etf_holdings_changes, list_etf_snapshot_dates


@dataclass(frozen=True)
class ConsensusTrendPoint:
    date: str
    etf_add_count: int

    def to_dict(self) -> dict:
        return {"date": self.date, "etf_add_count": self.etf_add_count}


def resolve_cohort_for_curr_date(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    curr_date: str,
    *,
    min_etfs: int = 2,
) -> AlignedCohort | None:
    buckets: dict[tuple[str, str], list[str]] = {}
    for etf_code in etf_codes:
        dates = list_etf_snapshot_dates(conn, etf_code)
        if curr_date not in dates:
            continue
        idx = dates.index(curr_date)
        if idx + 1 >= len(dates):
            continue
        prev = dates[idx + 1]
        buckets.setdefault((curr_date, prev), []).append(etf_code)
    if not buckets:
        return None
    (curr, prev), members = max(buckets.items(), key=lambda item: len(item[1]))
    if len(members) < min_etfs:
        return None
    return AlignedCohort(prev_date=prev, curr_date=curr, etf_codes=tuple(members))


def _etf_add_counts_at_cohort(
    conn: sqlite3.Connection,
    cohort: AlignedCohort,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for etf_code in cohort.etf_codes:
        rows = compute_etf_holdings_changes(
            conn, etf_code, cohort.curr_date, cohort.prev_date
        )
        for row in rows:
            if row["action"] not in ADD_ACTIONS:
                continue
            delta = float(row["share_delta"] or 0)
            if delta <= 0:
                continue
            sid = str(row["stock_id"])
            counts[sid] = counts.get(sid, 0) + 1
    return counts


def list_recent_snapshot_dates(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    max_dates: int = 8,
) -> list[str]:
    all_dates: set[str] = set()
    for etf in etf_codes:
        all_dates.update(list_etf_snapshot_dates(conn, etf))
    return sorted(all_dates, reverse=True)[:max_dates]


def build_consensus_trend(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    stock_id: str,
    *,
    max_points: int = 6,
) -> list[ConsensusTrendPoint]:
    """最近數個對齊窗口的加碼 ETF 檔數（舊→新）。"""
    points: list[ConsensusTrendPoint] = []
    for curr in reversed(list_recent_snapshot_dates(conn, etf_codes, max_dates=max_points + 2)):
        cohort = resolve_cohort_for_curr_date(conn, etf_codes, curr)
        if cohort is None:
            continue
        counts = _etf_add_counts_at_cohort(conn, cohort)
        points.append(
            ConsensusTrendPoint(
                date=cohort.curr_date,
                etf_add_count=counts.get(stock_id, 0),
            )
        )
        if len(points) >= max_points:
            break
    points.reverse()
    return points


def consensus_trend_label(points: list[ConsensusTrendPoint]) -> str | None:
    if len(points) < 2:
        return None
    first = points[0].etf_add_count
    last = points[-1].etf_add_count
    if last < first - 1:
        return "衰退"
    if last > first:
        return "擴張"
    return "穩定"


def consensus_trend_summary(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    stock_id: str,
) -> dict:
    points = build_consensus_trend(conn, etf_codes, stock_id)
    label = consensus_trend_label(points)
    out: dict = {
        "consensus_trend": [p.to_dict() for p in points],
    }
    if label:
        out["consensus_trend_label"] = label
    if points:
        out["consensus_etf_add_latest"] = points[-1].etf_add_count
    return out
