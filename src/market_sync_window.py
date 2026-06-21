"""同步窗缺口判定（daily sync 增量 + 歷史 backfill 共用）。"""

from __future__ import annotations

from datetime import date, timedelta

SeriesCoverage = tuple[str | None, str | None, int]  # (min_date, max_date, count_in_window)


def min_rows_required(lookback_days: int) -> int:
    return max(5, lookback_days // 5)


def resolve_sync_window(
    *,
    start: date,
    end: date,
    min_rows: int,
    series: list[SeriesCoverage],
    force_refresh: bool,
    overlap_days: int = 7,
) -> tuple[str, date | None, date | None]:
    """
    回傳 (action, fetch_start, fetch_end)。
    action: skip | incremental | backfill | full
    """
    if force_refresh:
        return "full", start, end

    start_s = start.isoformat()
    end_s = end.isoformat()
    window_days = max(1, (end - start).days + 1)
    effective_min_rows = min(min_rows, min_rows_required(window_days))

    complete = True
    need_old = False
    need_new = False
    earliest_min: str | None = None
    latest_max: str | None = None

    for dmin, dmax, count in series:
        if dmax is None or count < effective_min_rows or dmax < end_s:
            need_new = True
            complete = False
        if dmin is None or dmin > start_s:
            need_old = True
            complete = False
        if dmin and (earliest_min is None or dmin < earliest_min):
            earliest_min = dmin
        if dmax and (latest_max is None or dmax > latest_max):
            latest_max = dmax

    if complete:
        return "skip", None, None

    if need_old and need_new:
        return "full", start, end

    if need_old:
        assert earliest_min is not None
        fetch_end = min(
            end,
            date.fromisoformat(earliest_min) + timedelta(days=overlap_days),
        )
        return "backfill", start, fetch_end

    assert latest_max is not None
    fetch_start = max(
        start,
        date.fromisoformat(latest_max) - timedelta(days=overlap_days),
    )
    return "incremental", fetch_start, end


def iter_calendar_chunks(
    start: date,
    end: date,
    chunk_days: int,
) -> list[tuple[date, date]]:
    """將 [start, end] 切成若干 chunk（含首尾）。"""
    if chunk_days < 1:
        raise ValueError("chunk_days 須 >= 1")
    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
        chunks.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return chunks
