"""US ES/NQ overnight futures · PIT snapshots for TW intraday entry polls."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

TZ_TW = ZoneInfo("Asia/Taipei")
TZ_ET = ZoneInfo("America/New_York")
US_RTH_CLOSE = time(16, 0)

ES_YAHOO = "ES=F"
NQ_YAHOO = "NQ=F"
US_FUTURES_CODES = ("ES", "NQ")
YAHOO_BY_CODE = {"ES": ES_YAHOO, "NQ": NQ_YAHOO}

# ABC v3 skip-09 polls (min_poll_idx >= 1)
DEFAULT_CAPTURE_LABELS = (
    "09:30",
    "10:00",
    "10:30",
    "11:00",
    "11:30",
    "12:00",
    "12:30",
    "13:00",
    "13:30",
)


def tw_poll_to_et(session_date: str, minute: str) -> datetime:
    hh, mm = minute.split(":")
    return datetime(
        int(session_date[:4]),
        int(session_date[5:7]),
        int(session_date[8:10]),
        int(hh),
        int(mm),
        tzinfo=TZ_TW,
    ).astimezone(TZ_ET)


def fetch_yahoo_intraday_closes(
    yahoo_symbol: str,
    start: date,
    end: date,
    *,
    interval: str = "1h",
) -> pd.Series:
    """Datetime-indexed close (US/Eastern naive from yfinance → localized ET)."""
    import yfinance as yf

    df = yf.download(
        yahoo_symbol,
        start=start.isoformat(),
        end=(end + timedelta(days=2)).isoformat(),
        interval=interval,
        progress=False,
        auto_adjust=False,
        threads=False,
    )
    if df.empty:
        return pd.Series(dtype=float)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)
    s = df["Close"].dropna().astype(float)
    if s.index.tz is None:
        s.index = s.index.tz_localize(TZ_ET)
    else:
        s.index = s.index.tz_convert(TZ_ET)
    return s.sort_index()


def fetch_yahoo_daily_closes(yahoo_symbol: str, start: date, end: date) -> pd.Series:
    import yfinance as yf

    df = yf.download(
        yahoo_symbol,
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        progress=False,
        auto_adjust=False,
        threads=False,
    )
    if df.empty:
        return pd.Series(dtype=float)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)
    out = df["Close"].dropna().astype(float)
    out.index = pd.to_datetime(out.index).strftime("%Y-%m-%d")
    return out


def prior_us_rth_close_date(dt_et: datetime, us_trade_dates: list[str]) -> str | None:
    """Last US equity RTH close strictly before dt_et (4pm ET boundaries)."""
    if not us_trade_dates:
        return None
    cal = sorted(us_trade_dates)
    d = dt_et.date().isoformat()
    t = dt_et.timetz().replace(tzinfo=None)
    if t >= US_RTH_CLOSE and d in cal:
        return d
    prior = [x for x in cal if x < d]
    return prior[-1] if prior else None


def price_at_or_before(intraday: pd.Series, dt_et: datetime) -> float | None:
    if intraday.empty:
        return None
    sub = intraday[intraday.index <= dt_et]
    if sub.empty:
        return None
    val = float(sub.iloc[-1])
    return val if val > 0 else None


def overnight_pct(now_px: float | None, prior_close: float | None) -> float | None:
    if now_px is None or prior_close is None or prior_close <= 0:
        return None
    return round((now_px - prior_close) / prior_close * 100.0, 4)


def build_snapshot_row(
    tw_session_date: str,
    capture_label: str,
    *,
    es_intraday: pd.Series,
    nq_intraday: pd.Series,
    es_daily: pd.Series,
    nq_daily: pd.Series,
    us_trade_dates: list[str],
    source: str = "yahoo_1h",
) -> dict[str, Any] | None:
    dt_et = tw_poll_to_et(tw_session_date, capture_label)
    us_prior = prior_us_rth_close_date(dt_et, us_trade_dates)
    if not us_prior:
        return None
    es_prior = float(es_daily[us_prior]) if us_prior in es_daily.index else None
    nq_prior = float(nq_daily[us_prior]) if us_prior in nq_daily.index else None
    es_px = price_at_or_before(es_intraday, dt_et)
    nq_px = price_at_or_before(nq_intraday, dt_et)
    if es_px is None and nq_px is None:
        return None
    captured_at = tw_poll_to_et(tw_session_date, capture_label).astimezone(TZ_TW).isoformat()
    return {
        "tw_session_date": tw_session_date,
        "capture_label": capture_label,
        "captured_at": captured_at,
        "us_prior_trade_date": us_prior,
        "es_price": es_px,
        "nq_price": nq_px,
        "es_prior_close": es_prior,
        "nq_prior_close": nq_prior,
        "es_overnight_pct": overnight_pct(es_px, es_prior),
        "nq_overnight_pct": overnight_pct(nq_px, nq_prior),
        "source": source,
    }


def build_snapshots_for_dates(
    tw_dates: list[str],
    *,
    capture_labels: tuple[str, ...] = DEFAULT_CAPTURE_LABELS,
    interval: str = "1h",
) -> list[dict[str, Any]]:
    if not tw_dates:
        return []
    start = date.fromisoformat(min(tw_dates)) - timedelta(days=5)
    end = date.fromisoformat(max(tw_dates)) + timedelta(days=1)
    es_intra = fetch_yahoo_intraday_closes(ES_YAHOO, start, end, interval=interval)
    nq_intra = fetch_yahoo_intraday_closes(NQ_YAHOO, start, end, interval=interval)
    es_daily = fetch_yahoo_daily_closes(ES_YAHOO, start - timedelta(days=30), end)
    nq_daily = fetch_yahoo_daily_closes(NQ_YAHOO, start - timedelta(days=30), end)
    us_dates = sorted(set(es_daily.index.astype(str).tolist()) | set(nq_daily.index.astype(str).tolist()))

    rows: list[dict[str, Any]] = []
    for d in sorted(tw_dates):
        for label in capture_labels:
            row = build_snapshot_row(
                d,
                label,
                es_intraday=es_intra,
                nq_intraday=nq_intra,
                es_daily=es_daily,
                nq_daily=nq_daily,
                us_trade_dates=us_dates,
                source=f"yahoo_{interval}",
            )
            if row:
                rows.append(row)
    return rows


def build_live_snapshot(
    tw_session_date: str | None = None,
    capture_label: str | None = None,
) -> dict[str, Any] | None:
    """Current ES/NQ vs last US RTH close."""
    now_tw = datetime.now(TZ_TW)
    d = tw_session_date or now_tw.date().isoformat()
    label = capture_label or now_tw.strftime("%H:%M")
    rows = build_snapshots_for_dates([d], capture_labels=(label,), interval="1h")
    return rows[0] if rows else None
