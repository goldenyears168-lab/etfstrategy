"""Disposition Live TA · ~20min call-auction reminder state (observe).

Poll-safe: compute phase/action/note from wall clock + last print; upsert
``ops.live_ta``. Quotes via Yahoo (existing ``yahoo_chart_sync`` path) with
optional local DB fallback — no Fubon required.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ops_console_sync import now_tpe_iso, upsert_live_ta
from yahoo_chart_sync import fetch_yahoo_intraday_df, tw_yahoo_symbol_candidates

_TPE = ZoneInfo("Asia/Taipei")
# 第二款處置常見 20 分集合競價節點（含 13:30 收盤撮合提醒）
_AUCTION_MINUTES: tuple[int, ...] = tuple(range(9 * 60, 13 * 60 + 21, 20)) + (13 * 60 + 30,)
_DEFAULT_STOCKS: tuple[tuple[str, str], ...] = (("2492", "華新科"),)


@dataclass(frozen=True)
class LiveTaState:
    stock_id: str
    stock_name: str | None
    asof: str
    last_print: float | None
    phase: str
    action: str
    note_zh: str
    next_auction_at: str | None
    anchors: dict[str, Any]


def parse_stock_list(raw: str | None = None) -> list[tuple[str, str | None]]:
    """``2492:華新科,2330:台積電`` or ``2492,2330`` · env ``OPS_LIVE_TA_STOCKS``."""
    text = (raw if raw is not None else os.environ.get("OPS_LIVE_TA_STOCKS", "")).strip()
    if not text:
        return list(_DEFAULT_STOCKS)
    out: list[tuple[str, str | None]] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            sid, name = part.split(":", 1)
            out.append((sid.strip(), name.strip() or None))
        else:
            out.append((part, None))
    return out or list(_DEFAULT_STOCKS)


def _hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def auction_slots_for_day(day: date) -> list[datetime]:
    return [
        datetime(day.year, day.month, day.day, m // 60, m % 60, tzinfo=_TPE)
        for m in _AUCTION_MINUTES
    ]


def next_auction(now: datetime) -> datetime | None:
    now = now.astimezone(_TPE)
    if now.weekday() >= 5:
        return None
    for slot in auction_slots_for_day(now.date()):
        if slot >= now - timedelta(seconds=5):
            return slot
    return None


def classify_auction_phase(now: datetime) -> tuple[str, str, str, datetime | None]:
    """Return phase, action, note_zh, next_auction_at."""
    now = now.astimezone(_TPE)
    if now.weekday() >= 5:
        return "weekend", "盤後", "假日休市；處置撮合提醒暫停。", None

    slots = auction_slots_for_day(now.date())
    open_t = slots[0]
    close_t = slots[-1]

    if now < open_t - timedelta(minutes=30):
        return (
            "pre_open",
            "等待開盤",
            f"距開盤撮合 {_hm(9 * 60)} 尚早；先對齊昨收與關鍵價。",
            open_t,
        )
    if now < open_t:
        mins = max(0, int((open_t - now).total_seconds() // 60))
        return (
            "pre_open",
            "準備開盤",
            f"約 {mins} 分後開盤撮合（{_hm(9 * 60)}）。勿搶早盤瞬間跳動。",
            open_t,
        )
    if now > close_t + timedelta(minutes=5):
        return "closed", "盤後", "今日撮合結束；明日再開。", None

    # find nearest past / next
    nxt: datetime | None = None
    prev: datetime | None = None
    for slot in slots:
        if slot <= now:
            prev = slot
        if slot >= now and nxt is None:
            nxt = slot
    if nxt is None:
        nxt = close_t

    secs_to = (nxt - now).total_seconds()
    secs_from = (now - prev).total_seconds() if prev else 9999

    if 0 <= secs_to <= 120:
        return (
            "pre_match",
            "注意撮合",
            f"下一檔集合競價約 {int(secs_to)} 秒（{nxt.strftime('%H:%M')}）。先看量能與是否失守錨點，勿追瞬間價。",
            nxt,
        )
    if prev is not None and 0 <= secs_from <= 90:
        return (
            "just_matched",
            "觀望結果",
            f"剛過 {prev.strftime('%H:%M')} 撮合；等成交價定錨再判斷，勿追第一下。",
            nxt if nxt > now else None,
        )
    mins = max(0, int(secs_to // 60))
    return (
        "between",
        "觀望",
        f"距下一撮合約 {mins} 分（{nxt.strftime('%H:%M')}）。觀察是否守住昨收／開盤錨。",
        nxt,
    )


def fetch_last_print_yahoo(stock_id: str) -> tuple[float | None, dict[str, Any]]:
    """Latest intraday close via yfinance 1m→5m."""
    meta: dict[str, Any] = {"quote_source": None}
    today = datetime.now(_TPE).date()
    start = today - timedelta(days=5)
    for interval in ("1m", "5m"):
        for symbol in tw_yahoo_symbol_candidates(stock_id):
            try:
                df = fetch_yahoo_intraday_df(symbol, start, today, interval=interval)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001 — keep poll alive
                meta["yahoo_error"] = str(exc)[:200]
                continue
            if df is None or df.empty:
                continue
            close = df["Close"].dropna()
            if close.empty:
                continue
            px = float(close.iloc[-1])
            meta["quote_source"] = f"yahoo:{symbol}:{interval}"
            meta["bar_time"] = str(close.index[-1])
            return px, meta
    return None, meta


def fetch_prev_close_db(conn: sqlite3.Connection | None, stock_id: str) -> float | None:
    if conn is None:
        return None
    try:
        row = conn.execute(
            """
            SELECT close FROM stock_daily_bars
            WHERE stock_id = ? AND close IS NOT NULL AND close > 0
            ORDER BY trade_date DESC LIMIT 1
            """,
            (stock_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    return float(row[0])


def resolve_stock_name(conn: sqlite3.Connection | None, stock_id: str, fallback: str | None) -> str | None:
    if fallback:
        return fallback
    if conn is None:
        return None
    try:
        row = conn.execute(
            """
            SELECT stock_name FROM rrg_universe_scores
            WHERE stock_id = ? AND stock_name IS NOT NULL AND TRIM(stock_name) != ''
            ORDER BY session_date DESC LIMIT 1
            """,
            (stock_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row and row[0]:
        return str(row[0])
    return None


def build_live_ta_state(
    stock_id: str,
    *,
    stock_name: str | None = None,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
    last_print: float | None = None,
    quote_meta: dict[str, Any] | None = None,
) -> LiveTaState:
    now = (now or datetime.now(_TPE)).astimezone(_TPE)
    phase, action, note, nxt = classify_auction_phase(now)
    meta = dict(quote_meta or {})
    px = last_print
    if px is None:
        px, qmeta = fetch_last_print_yahoo(stock_id)
        meta.update(qmeta)
    prev = fetch_prev_close_db(conn, stock_id)
    name = resolve_stock_name(conn, stock_id, stock_name)
    anchors: dict[str, Any] = {
        "auction_interval_min": 20,
        "prev_close": prev,
        "pct_from_prev": round((px / prev - 1.0) * 100, 3) if px and prev else None,
        "mins_to_next": (
            max(0, int((nxt - now).total_seconds() // 60)) if nxt else None
        ),
        **meta,
    }
    if prev and px and abs(px / prev - 1.0) >= 0.03 and phase in {"between", "pre_match"}:
        note = note + f"｜相對昨收 {anchors['pct_from_prev']:+.2f}%（≥3% 波動，謹慎）。"
    return LiveTaState(
        stock_id=stock_id,
        stock_name=name,
        asof=now.isoformat(),
        last_print=px,
        phase=phase,
        action=action,
        note_zh=note,
        next_auction_at=nxt.isoformat() if nxt else None,
        anchors=anchors,
    )


def publish_live_ta(state: LiveTaState) -> dict[str, Any]:
    row = asdict(state)
    upsert_live_ta(row)
    return row


def run_live_ta_poll(
    stocks: list[tuple[str, str | None]] | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    pairs = stocks if stocks is not None else parse_stock_list()
    out: list[dict[str, Any]] = []
    for sid, name in pairs:
        state = build_live_ta_state(sid, stock_name=name, conn=conn)
        row = asdict(state)
        if not dry_run:
            upsert_live_ta(row)
        out.append(row)
    return out
