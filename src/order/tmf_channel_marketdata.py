"""TMF front-month resolve + 1m candles via Fubon futopt marketdata."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from order.fubon_session import FubonSession

_TZ = ZoneInfo("Asia/Taipei")
_CACHE: dict[str, Any] = {"sym": None, "ts": 0.0}


def _ensure_realtime_once(session: FubonSession) -> None:
    """Prefer pooled one-shot realtime init; fall back to SDK call."""
    try:
        from tmf_channel.session_pool import ensure_realtime

        ensure_realtime(session)
    except Exception:
        if not getattr(session, "_tmf_realtime_ok", False):
            session.init_realtime()
            setattr(session, "_tmf_realtime_ok", True)


def resolve_front_symbol(
    session: FubonSession,
    *,
    product: str = "TMF",
    max_age_sec: float = 300.0,
) -> tuple[str, str, str]:
    now = time.time()
    if _CACHE["sym"] and now - float(_CACHE["ts"]) < max_age_sec:
        return _CACHE["sym"]
    _ensure_realtime_once(session)
    fut = session.sdk.marketdata.rest_client.futopt
    data = fut.intraday.tickers(type="FUTURE", exchange="TAIFEX", product=product)
    rows = data if isinstance(data, list) else (data.get("data") or data.get("tickers") or [])
    today = datetime.now(tz=_TZ).strftime("%Y-%m-%d")
    cands = []
    for r in rows or []:
        sym = str(r.get("symbol") or r.get("ticker") or "")
        end = str(r.get("endDate") or r.get("end_date") or r.get("contractDate") or "")[:10]
        name = str(r.get("name") or r.get("symbolName") or sym)
        if not sym.startswith(product):
            continue
        if end and end < today:
            continue
        cands.append((end or "9999-99-99", sym, name))
    if not cands:
        raise RuntimeError(f"No front-month {product} from Fubon tickers")
    cands.sort()
    end, sym, name = cands[0]
    _CACHE["sym"] = (sym, name, end)
    _CACHE["ts"] = now
    return sym, name, end


def _row_dt(row: dict) -> datetime | None:
    """Parse the row's own calendar date+time — never assume 'today'.

    Afterhours candles span two calendar dates (yesterday 15:00 → today
    05:00, or today 15:00 → tomorrow 05:00); collapsing to HH:MM only (as
    an earlier version of this module did) silently re-dated last night's
    bars onto today, producing e.g. an "open_pos" entered at a future
    timestamp. Mirrors live_v6_sim_server.py's _row_dt/_in_night_window,
    which got this right first.
    """
    t = str(row.get("date") or row.get("datetime") or row.get("time") or "")
    if not t:
        return None
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(_TZ)
    except ValueError:
        return None


def _in_night_window(dt: datetime, today) -> bool:
    yday = today - timedelta(days=1)
    d = dt.date()
    hm = dt.hour * 60 + dt.minute
    if d == yday and hm >= 15 * 60:
        return True
    if d == today and hm <= 5 * 60:
        return True
    if d == today and hm >= 15 * 60:
        return True
    return False


def _in_day_window(dt: datetime, today) -> bool:
    if dt.date() != today:
        return False
    hm = dt.hour * 60 + dt.minute
    return (8 * 60 + 45) <= hm <= (13 * 60 + 45)


def _normalize_bar(row: dict, dt: datetime) -> dict[str, Any] | None:
    try:
        o = float(row.get("open") or row.get("openPrice") or 0)
        h = float(row.get("high") or row.get("highPrice") or 0)
        l = float(row.get("low") or row.get("lowPrice") or 0)
        c = float(row.get("close") or row.get("closePrice") or row.get("lastPrice") or 0)
    except (TypeError, ValueError):
        return None
    if o <= 0 or c <= 0:
        return None
    v = float(row.get("volume") or row.get("tradeVolume") or 0)
    t = dt.isoformat(timespec="milliseconds")
    return dict(t=t, o=o, h=h, l=l, c=c, v=v)


def fetch_1m_bars(session: FubonSession, symbol: str) -> list[dict[str, Any]]:
    """Day (default candles) + afterhours, calendar-date-aware de-dupe.

    Keeps each row's own date (see _row_dt) instead of collapsing to
    HH:MM, so overlapping HH:MM across yesterday-night / today-day /
    tonight never collide or get mislabeled.
    """
    _ensure_realtime_once(session)
    fut = session.sdk.marketdata.rest_client.futopt
    today = datetime.now(tz=_TZ).date()
    day_res = fut.intraday.candles(symbol=symbol, timeframe="1")
    day_raw = list(day_res.get("data") or []) if isinstance(day_res, dict) else list(day_res or [])
    try:
        night_res = fut.intraday.candles(symbol=symbol, timeframe="1", session="afterhours")
        night_raw = (
            list(night_res.get("data") or []) if isinstance(night_res, dict) else list(night_res or [])
        )
    except Exception:
        night_raw = []

    by_t: dict[str, dict] = {}
    for row in night_raw:
        dt = _row_dt(row)
        if dt is None or not _in_night_window(dt, today):
            continue
        b = _normalize_bar(row, dt)
        if b:
            by_t[b["t"]] = b
    for row in day_raw:
        dt = _row_dt(row)
        if dt is None or not _in_day_window(dt, today):
            continue
        b = _normalize_bar(row, dt)
        if b:
            by_t[b["t"]] = b
    return [by_t[k] for k in sorted(by_t)]


def bars_to_arrays(day: str, bars: list[dict]) -> tuple:
    """``day`` kept only for call-site compatibility; each bar already
    carries its own full ISO timestamp (see fetch_1m_bars)."""
    O = [b["o"] for b in bars]
    H = [b["h"] for b in bars]
    L = [b["l"] for b in bars]
    C = [b["c"] for b in bars]
    V = [b.get("v") or 0 for b in bars]
    T = [b["t"] for b in bars]
    return O, H, L, C, V, T


def session_hhmm_now() -> str:
    return datetime.now(tz=_TZ).strftime("%H:%M")


def in_tmf_trade_window(hm: str | None = None) -> bool:
    hm = hm or session_hhmm_now()
    if "08:45" <= hm <= "13:45":
        return True
    if hm >= "15:00" or hm < "05:00":
        return True
    return False
