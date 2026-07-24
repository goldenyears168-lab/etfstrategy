"""Holdings Live TA → ``ops.live_ta`` (observe · mini poll).

Universe = current holdings ∪ optional ``OPS_LIVE_TA_STOCKS`` extras.

Two modes (honest horizon — do **not** claim false precision):
  - **disposition** (``OPS_LIVE_TA_DISPOSITION``, default ``2492``): ~20‑min
    call-auction clock; ``pre_match`` is the real ~2‑minute checkpoint window.
  - **continuous** (normal holdings): last print + % vs prev close + short
    1m-bar momentum (~1–2 min lookback). This is short momentum, **not** a
    price prediction 2 minutes ahead.

Quotes via Yahoo (``yahoo_chart_sync``) with optional local DB fallback —
no Fubon required for the poll itself.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ops_console_sync import fetch_latest_ops_holdings_payload, now_tpe_iso, upsert_live_ta
from yahoo_chart_sync import fetch_yahoo_intraday_df, tw_yahoo_symbol_candidates

_TPE = ZoneInfo("Asia/Taipei")
# 第二款處置常見 20 分集合競價節點（含 13:30 收盤撮合提醒）
_AUCTION_MINUTES: tuple[int, ...] = tuple(range(9 * 60, 13 * 60 + 21, 20)) + (13 * 60 + 30,)
_DEFAULT_STOCKS: tuple[tuple[str, str], ...] = (("2492", "華新科"),)
_DEFAULT_DISPOSITION: tuple[str, ...] = ("2492",)
# Continuous TW cash session (approx.)
_CONT_OPEN_MIN = 9 * 60
_CONT_CLOSE_MIN = 13 * 60 + 30
_YAHOO_GAP_SEC = 0.25


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


def parse_stock_list(
    raw: str | None = None,
    *,
    default_if_empty: bool = True,
) -> list[tuple[str, str | None]]:
    """``2492:華新科,2330:台積電`` or ``2492,2330`` · env ``OPS_LIVE_TA_STOCKS``."""
    text = (raw if raw is not None else os.environ.get("OPS_LIVE_TA_STOCKS", "")).strip()
    if not text:
        return list(_DEFAULT_STOCKS) if default_if_empty else []
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
    if out:
        return out
    return list(_DEFAULT_STOCKS) if default_if_empty else []


def parse_disposition_ids(raw: str | None = None) -> set[str]:
    """Stock ids that use 20‑min disposition auction clock · ``OPS_LIVE_TA_DISPOSITION``."""
    text = (raw if raw is not None else os.environ.get("OPS_LIVE_TA_DISPOSITION", "")).strip()
    if not text:
        return set(_DEFAULT_DISPOSITION)
    out = {p.strip() for p in text.split(",") if p.strip()}
    return out or set(_DEFAULT_DISPOSITION)


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
    """Disposition call-auction · return phase, action, note_zh, next_auction_at."""
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


def classify_continuous_phase(now: datetime) -> tuple[str, str, str, datetime | None]:
    """Normal continuous-auction session clock (no 20‑min disposition grid)."""
    now = now.astimezone(_TPE)
    if now.weekday() >= 5:
        return "weekend", "盤後", "假日休市；持倉短動能暫停。", None

    mins = now.hour * 60 + now.minute
    open_t = datetime(now.year, now.month, now.day, 9, 0, tzinfo=_TPE)
    close_t = datetime(now.year, now.month, now.day, 13, 30, tzinfo=_TPE)

    if mins < _CONT_OPEN_MIN - 30:
        return (
            "pre_open",
            "等待開盤",
            "連續競價尚未開始；對齊昨收即可（非處置 20 分撮合）。",
            open_t,
        )
    if mins < _CONT_OPEN_MIN:
        rem = max(0, int((open_t - now).total_seconds() // 60))
        return (
            "pre_open",
            "準備開盤",
            f"約 {rem} 分後開盤連續競價。短動能僅供參考，非未來兩分鐘預測。",
            open_t,
        )
    if mins > _CONT_CLOSE_MIN + 5:
        return "closed", "盤後", "今日連續競價結束。", None
    return (
        "continuous",
        "觀望",
        "連續競價中：顯示昨收漲跌與近 1–2 分 bar 短動能（非精準預測）。",
        close_t,
    )


def fetch_last_print_yahoo(stock_id: str) -> tuple[float | None, dict[str, Any]]:
    """Latest intraday close + short momentum via yfinance 1m→5m."""
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
            if len(close) >= 2:
                px1 = float(close.iloc[-2])
                if px1 > 0:
                    meta["mom_1bar_pct"] = round((px / px1 - 1.0) * 100, 3)
            if len(close) >= 3:
                px2 = float(close.iloc[-3])
                if px2 > 0:
                    meta["mom_2bar_pct"] = round((px / px2 - 1.0) * 100, 3)
            # ~2 min lookback only meaningful on 1m bars
            if interval == "1m":
                meta["momentum_horizon"] = "1m_bars_lookback_1_2"
            else:
                meta["momentum_horizon"] = f"{interval}_bars_lookback_1_2"
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


def load_holdings_from_db(conn: sqlite3.Connection | None) -> list[tuple[str, str | None]]:
    """Latest ``order_holdings_snapshot`` with shares > 0."""
    if conn is None:
        return []
    try:
        row = conn.execute(
            "SELECT snapshot_date FROM order_holdings_snapshot ORDER BY snapshot_date DESC LIMIT 1"
        ).fetchone()
        if not row:
            return []
        snap = str(row[0])
        cur = conn.execute(
            """
            SELECT stock_id, stock_name FROM order_holdings_snapshot
            WHERE snapshot_date = ? AND shares > 0
            ORDER BY stock_id
            """,
            (snap,),
        )
    except sqlite3.Error:
        return []
    out: list[tuple[str, str | None]] = []
    for sid, name in cur.fetchall():
        sid_s = str(sid).strip()
        if not sid_s:
            continue
        name_s = str(name).strip() if name else ""
        out.append((sid_s, name_s or None))
    return out


def load_holdings_from_ops() -> list[tuple[str, str | None]]:
    """Latest ``ops.holdings`` / ``ops_holdings`` by_symbol (website SSOT)."""
    try:
        payload = fetch_latest_ops_holdings_payload()
    except Exception:  # noqa: BLE001 — poll must stay up
        return []
    if not payload:
        return []
    by_sym = payload.get("by_symbol") or {}
    if not isinstance(by_sym, dict):
        return []
    names: dict[str, str] = {}
    for h in payload.get("holdings") or []:
        if not isinstance(h, dict):
            continue
        sid = str(h.get("stock_no") or h.get("stock_id") or "").strip()
        nm = str(h.get("stock_name") or h.get("name") or "").strip()
        if sid and nm:
            names[sid] = nm
    out: list[tuple[str, str | None]] = []
    for sid, qty in by_sym.items():
        sid_s = str(sid).strip()
        if not sid_s:
            continue
        try:
            if int(qty) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        out.append((sid_s, names.get(sid_s)))
    out.sort(key=lambda x: x[0])
    return out


def _merge_pairs(*groups: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    merged: dict[str, str | None] = {}
    for group in groups:
        for sid, name in group:
            sid_s = sid.strip()
            if not sid_s:
                continue
            prev = merged.get(sid_s)
            if sid_s not in merged:
                merged[sid_s] = name
            elif name and not prev:
                merged[sid_s] = name
    return sorted(merged.items(), key=lambda x: x[0])


def resolve_live_ta_universe(
    conn: sqlite3.Connection | None = None,
    *,
    extras_raw: str | None = None,
    include_ops_holdings: bool = True,
) -> list[tuple[str, str | None]]:
    """Holdings (DB ∪ ops) ∪ ``OPS_LIVE_TA_STOCKS`` extras; fallback default if empty."""
    db_rows = load_holdings_from_db(conn)
    ops_rows = load_holdings_from_ops() if include_ops_holdings else []
    extras = parse_stock_list(extras_raw, default_if_empty=False)
    merged = _merge_pairs(db_rows, ops_rows, extras)
    return merged if merged else list(_DEFAULT_STOCKS)


def _continuous_action_note(
    phase: str,
    base_action: str,
    base_note: str,
    *,
    mom_1: float | None,
    mom_2: float | None,
    pct_from_prev: float | None,
) -> tuple[str, str]:
    if phase != "continuous":
        return base_action, base_note
    parts = [base_note]
    if pct_from_prev is not None:
        parts.append(f"相對昨收 {pct_from_prev:+.2f}%")
    if mom_2 is not None:
        parts.append(f"近2分bar {mom_2:+.2f}%")
    elif mom_1 is not None:
        parts.append(f"近1分bar {mom_1:+.2f}%")
    action = base_action
    ref = mom_2 if mom_2 is not None else mom_1
    if ref is not None:
        if ref >= 0.35:
            action = "偏強"
        elif ref <= -0.35:
            action = "偏弱"
    return action, "｜".join(parts)


def build_live_ta_state(
    stock_id: str,
    *,
    stock_name: str | None = None,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
    last_print: float | None = None,
    quote_meta: dict[str, Any] | None = None,
    disposition_ids: set[str] | None = None,
) -> LiveTaState:
    now = (now or datetime.now(_TPE)).astimezone(_TPE)
    disp = disposition_ids if disposition_ids is not None else parse_disposition_ids()
    is_disp = stock_id in disp
    if is_disp:
        phase, action, note, nxt = classify_auction_phase(now)
        mode = "disposition"
        horizon = "disposition_auction_pre_match_~2min"
    else:
        phase, action, note, nxt = classify_continuous_phase(now)
        mode = "continuous"
        horizon = "continuous_1m_momentum_lookback_not_forecast"

    meta = dict(quote_meta or {})
    px = last_print
    if px is None:
        px, qmeta = fetch_last_print_yahoo(stock_id)
        meta.update(qmeta)
    prev = fetch_prev_close_db(conn, stock_id)
    name = resolve_stock_name(conn, stock_id, stock_name)
    pct_from_prev = round((px / prev - 1.0) * 100, 3) if px and prev else None
    mom_1 = meta.get("mom_1bar_pct")
    mom_2 = meta.get("mom_2bar_pct")
    if isinstance(mom_1, (int, float)):
        mom_1_f: float | None = float(mom_1)
    else:
        mom_1_f = None
    if isinstance(mom_2, (int, float)):
        mom_2_f: float | None = float(mom_2)
    else:
        mom_2_f = None

    if not is_disp:
        action, note = _continuous_action_note(
            phase,
            action,
            note,
            mom_1=mom_1_f,
            mom_2=mom_2_f,
            pct_from_prev=pct_from_prev,
        )

    anchors: dict[str, Any] = {
        "mode": mode,
        "horizon": horizon,
        "auction_interval_min": 20 if is_disp else None,
        "prev_close": prev,
        "pct_from_prev": pct_from_prev,
        "mins_to_next": (
            max(0, int((nxt - now).total_seconds() // 60)) if nxt else None
        ),
        **meta,
    }
    if (
        is_disp
        and prev
        and px
        and abs(px / prev - 1.0) >= 0.03
        and phase in {"between", "pre_match"}
    ):
        note = note + f"｜相對昨收 {anchors['pct_from_prev']:+.2f}%（≥3% 波動，謹慎）。"
    return LiveTaState(
        stock_id=stock_id,
        stock_name=name,
        asof=now.isoformat(),
        last_print=px,
        phase=phase,
        action=action,
        note_zh=note,
        # next_auction_at only meaningful for disposition 20‑min grid
        next_auction_at=nxt.isoformat() if (is_disp and nxt) else None,
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
    disposition_ids: set[str] | None = None,
    yahoo_gap_sec: float = _YAHOO_GAP_SEC,
) -> list[dict[str, Any]]:
    pairs = stocks if stocks is not None else resolve_live_ta_universe(conn)
    disp = disposition_ids if disposition_ids is not None else parse_disposition_ids()
    out: list[dict[str, Any]] = []
    for i, (sid, name) in enumerate(pairs):
        if i > 0 and yahoo_gap_sec > 0:
            time.sleep(yahoo_gap_sec)
        state = build_live_ta_state(
            sid,
            stock_name=name,
            conn=conn,
            disposition_ids=disp,
        )
        row = asdict(state)
        if not dry_run:
            upsert_live_ta(row)
        out.append(row)
    return out
