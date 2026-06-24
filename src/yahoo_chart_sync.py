"""Yahoo Chart API helpers — raw OHLCV, adj close, dividends/splits."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

import pandas as pd

YAHOO_SOURCE = "yahoo"
YAHOO_KBAR_SOURCE = "yahoo"
YFINANCE_TW_SOURCE = "yfinance"
YFINANCE_US_SOURCE = "yfinance"

# daily_bars.code → Yahoo symbol
YAHOO_INDEX_CODES: dict[str, str] = {
    "IX0001": "^TWII",
    "IR0002": "0050.TW",
    "TSM_ADR": "TSM",
    "SOX": "^SOX",
    "SMH": "SMH",
}

DEFAULT_US_RESEARCH_TICKERS: tuple[str, ...] = (
    "TSM",
    "NVDA",
    "SMH",
    "SPY",
    "AM",
    "APP",
    "CHWY",
    "COHR",
    "FSLR",
    "GOOGL",
    "PANW",
    "PLTR",
    "RDDT",
    "SHOP",
    "SMCI",
    "TSLA",
    "VRT",
    "WFRD",
    "XLE",
)

DEFAULT_YAHOO_BACKFILL_START = date(2019, 1, 1)


@dataclass(frozen=True)
class YahooDailyBar:
    trade_date: str
    open: float | None
    high: float | None
    low: float | None
    close: float
    adj_close: float | None
    volume: float | None


@dataclass(frozen=True)
class YahooCorporateAction:
    ex_date: str
    action_type: Literal["dividend", "split"]
    amount: float | None = None
    split_numerator: float | None = None
    split_denominator: float | None = None
    split_ratio: str | None = None


def _flatten_yf_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.droplevel(1)
    return df


def _float_or_none(value: object) -> float | None:
    if isinstance(value, pd.Series):
        value = value.iloc[0] if len(value) else None
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def fetch_yahoo_daily_bars(
    yahoo_symbol: str,
    start: date,
    end: date,
) -> list[YahooDailyBar]:
    """Raw close + Adj Close via yfinance (Chart API wrapper)."""
    import yfinance as yf

    df = yf.download(
        yahoo_symbol,
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        progress=False,
        auto_adjust=False,
        actions=False,
        threads=False,
    )
    df = _flatten_yf_columns(df)
    if df.empty:
        return []

    bars: list[YahooDailyBar] = []
    for idx, row in df.iterrows():
        close = _float_or_none(row.get("Close"))
        if close is None or close <= 0:
            continue
        adj = _float_or_none(row.get("Adj Close"))
        bars.append(
            YahooDailyBar(
                trade_date=str(idx.date()),
                open=_float_or_none(row.get("Open")),
                high=_float_or_none(row.get("High")),
                low=_float_or_none(row.get("Low")),
                close=close,
                adj_close=adj if adj is not None else close,
                volume=_float_or_none(row.get("Volume")),
            )
        )
    return bars


def fetch_yahoo_corporate_actions(
    yahoo_symbol: str,
    start: date,
    end: date,
) -> list[YahooCorporateAction]:
    import yfinance as yf

    ticker = yf.Ticker(yahoo_symbol)
    actions = ticker.actions
    if actions is None or actions.empty:
        return []

    out: list[YahooCorporateAction] = []
    for idx, row in actions.iterrows():
        ex = str(idx.date())
        if ex < start.isoformat() or ex > end.isoformat():
            continue
        div = _float_or_none(row.get("Dividends"))
        if div is not None and div > 0:
            out.append(YahooCorporateAction(ex_date=ex, action_type="dividend", amount=div))
        split = _float_or_none(row.get("Stock Splits"))
        if split is not None and split > 0:
            numer = split
            denom = 1.0
            ratio = f"{int(split)}:1" if split >= 1 else f"1:{int(round(1 / split))}"
            out.append(
                YahooCorporateAction(
                    ex_date=ex,
                    action_type="split",
                    split_numerator=numer,
                    split_denominator=denom,
                    split_ratio=ratio,
                )
            )
    return out


def tw_yahoo_symbol_candidates(stock_id: str) -> tuple[str, ...]:
    sid = stock_id.strip()
    if sid.startswith("00") and len(sid) <= 5:
        return (f"{sid}.TW",)
    return (f"{sid}.TW", f"{sid}.TWO")


def fetch_tw_daily_bars(
    stock_id: str,
    start: date,
    end: date,
) -> tuple[list[YahooDailyBar], str | None]:
    """Try .TW then .TWO."""
    for symbol in tw_yahoo_symbol_candidates(stock_id):
        bars = fetch_yahoo_daily_bars(symbol, start, end)
        if bars:
            return bars, symbol
        time.sleep(0.35)
    return [], None


def daily_bars_rows_from_yahoo(
    code: str,
    yahoo_symbol: str,
    start: date,
    end: date,
    *,
    source: str = YAHOO_SOURCE,
) -> list[dict]:
    bars = fetch_yahoo_daily_bars(yahoo_symbol, start, end)
    return [
        {
            "code": code,
            "date": b.trade_date,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "adj_close": b.adj_close,
            "volume": int(b.volume) if b.volume is not None else None,
            "spread": None,
            "source": source,
        }
        for b in bars
    ]


def stock_daily_bars_rows_from_yahoo(
    stock_id: str,
    start: date,
    end: date,
    *,
    source: str = YFINANCE_TW_SOURCE,
) -> tuple[list[dict], str | None]:
    bars, yahoo_symbol = fetch_tw_daily_bars(stock_id, start, end)
    rows = [
        {
            "stock_id": stock_id,
            "trade_date": b.trade_date,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "adj_close": b.adj_close,
            "volume": int(b.volume) if b.volume is not None else None,
            "source": source,
        }
        for b in bars
    ]
    return rows, yahoo_symbol


def us_daily_bars_rows_from_yahoo(
    ticker: str,
    start: date,
    end: date,
    *,
    source: str = YFINANCE_US_SOURCE,
) -> list[dict]:
    bars = fetch_yahoo_daily_bars(ticker.upper(), start, end)
    return [
        {
            "ticker": ticker.upper(),
            "trade_date": b.trade_date,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "adj_close": b.adj_close,
            "volume": b.volume,
            "source": source,
        }
        for b in bars
    ]


def corporate_action_rows(
    symbol_key: str,
    yahoo_symbol: str,
    start: date,
    end: date,
    *,
    source: str = YAHOO_SOURCE,
) -> list[dict]:
    actions = fetch_yahoo_corporate_actions(yahoo_symbol, start, end)
    return [
        {
            "symbol_key": symbol_key,
            "ex_date": a.ex_date,
            "action_type": a.action_type,
            "amount": a.amount,
            "split_numerator": a.split_numerator,
            "split_denominator": a.split_denominator,
            "split_ratio": a.split_ratio,
            "source": source,
        }
        for a in actions
    ]


YahooIntradayInterval = Literal["1m", "1h"]


def fetch_yahoo_intraday_df(
    yahoo_symbol: str,
    start: date,
    end: date,
    *,
    interval: YahooIntradayInterval = "1h",
) -> pd.DataFrame:
    """Yahoo Chart intraday OHLCV via yfinance.

    1m：約最近 30 日 · 1h：可覆蓋數月（台股回測 H1 用 1h + 近端 1m）。
    """
    import yfinance as yf

    ticker = yf.Ticker(yahoo_symbol)
    df = ticker.history(
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        interval=interval,
        auto_adjust=False,
        actions=False,
    )
    return _flatten_yf_columns(df)


def yahoo_intraday_rows_to_db(
    stock_id: str,
    df: pd.DataFrame,
    *,
    source: str = YAHOO_KBAR_SOURCE,
) -> list[dict]:
    """DataFrame index → stock_kbar_1m rows（Asia/Taipei 牆鐘）。"""
    if df is None or df.empty:
        return []
    out: list[dict] = []
    for idx, row in df.iterrows():
        ts = pd.Timestamp(idx)
        if ts.tzinfo is not None:
            ts = ts.tz_convert("Asia/Taipei")
        trade_date = ts.date().isoformat()
        minute = ts.strftime("%H:%M:%S")
        close = _float_or_none(row.get("Close"))
        if close is None or close <= 0:
            continue
        out.append(
            {
                "stock_id": stock_id,
                "trade_date": trade_date,
                "minute": minute,
                "open": _float_or_none(row.get("Open")),
                "high": _float_or_none(row.get("High")),
                "low": _float_or_none(row.get("Low")),
                "close": close,
                "volume": int(_float_or_none(row.get("Volume")) or 0) or None,
                "source": source,
            }
        )
    return out


def fetch_tw_intraday_kbar_rows(
    stock_id: str,
    start: date,
    end: date,
    *,
    interval: YahooIntradayInterval = "1h",
    source: str = YAHOO_KBAR_SOURCE,
) -> tuple[list[dict], str | None]:
    """Try .TW / .TWO · 回傳 DB rows + 使用的 Yahoo symbol。"""
    for symbol in tw_yahoo_symbol_candidates(stock_id):
        df = fetch_yahoo_intraday_df(symbol, start, end, interval=interval)
        rows = yahoo_intraday_rows_to_db(stock_id, df, source=source)
        if rows:
            return rows, symbol
        time.sleep(0.35)
    return [], None
