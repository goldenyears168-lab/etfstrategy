"""Holdings Live TA → ``ops.live_ta`` (observe · mini poll).

Universe = **current** holdings（優先：富邦 live → ops.holdings → 本地 snapshot）
∪ optional ``OPS_LIVE_TA_STOCKS`` extras. Does **not** union stale DB with ops.

Two modes (honest horizon — do **not** claim false precision):
  - **disposition** (``OPS_LIVE_TA_DISPOSITION``, default ``2492``): ~20‑min
    call-auction clock; ``pre_match`` is the real ~2‑minute checkpoint window.
  - **continuous** (normal holdings): last print + % vs prev close + short
    1m-bar momentum (~1–2 min lookback). This is short momentum, **not** a
    price prediction 2 minutes ahead.

Also attaches **observe-only 30m TA** (last closed half-hour from 5m bars):
return vs bar open, vs session VWAP, simple bias — not an Order signal.
Research badge ``fade30_observe`` uses champion rule ``fade_idx_or_inside``:
afternoon ``fade_near_ext`` **and** 0050 opening-range still intact
(research OOS ~71%; stock-heterogeneous; do **not** claim live hit-rate /
Order graduation). EMA/beta filters are intentionally omitted.

**Data source (default: all Fubon Neo · ``.venv-fubon``)**
  1. ``intraday.quote`` → ``last_print`` / 昨收漲跌% · bid/ask
  2. ``intraday.candles(timeframe=1)`` → 1–2m short momentum (TTL ~45s)
  3. ``intraday.candles(timeframe=5)`` → 30m observe + fade30 (TTL ~90s)
  4. 0050 5m (shared TTL cache) → ``idx_or_inside`` gate for fade30
  One ``connect_fubon(realtime=True)`` per poll (not per stock).

Yahoo Chart is **off by default**. Enable only for emergency/debug via
``OPS_LIVE_TA_YAHOO_FALLBACK=1`` (or ``OPS_LIVE_TA_QUOTE=yahoo`` for quote-only
debug). On Fubon miss: reuse TTL cache; else mark field not ready / error —
poll stays up.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests

from ops_console_sync import (
    delete_live_ta_except,
    fetch_latest_ops_holdings_payload,
    now_tpe_iso,
    supabase_ops_configured,
    upsert_live_ta,
)


def _tw_yahoo_symbol_candidates(stock_id: str) -> tuple[str, ...]:
    """Local copy of ``yahoo_chart_sync.tw_yahoo_symbol_candidates`` (no pandas).

    Kept here so ``.venv-fubon`` can run Live TA without importing yahoo_chart_sync
    (that module pulls pandas at import time; mini fubon venv often lacks it).
    """
    sid = stock_id.strip()
    if not sid:
        return ()
    if sid.startswith("^"):
        return (sid,)
    index_map = {
        "IX0001": "^TWII",
        "TAIEX": "^TWII",
        "TWII": "^TWII",
        "IR0002": "0050.TW",
    }
    mapped = index_map.get(sid) or index_map.get(sid.upper())
    if mapped:
        return (mapped,)
    if sid.startswith("00") and len(sid) <= 5:
        return (f"{sid}.TW",)
    return (f"{sid}.TW", f"{sid}.TWO")
_TPE = ZoneInfo("Asia/Taipei")
# 第二款處置常見 20 分集合競價節點（含 13:30 收盤撮合提醒）
_AUCTION_MINUTES: tuple[int, ...] = tuple(range(9 * 60, 13 * 60 + 21, 20)) + (13 * 60 + 30,)
_DEFAULT_STOCKS: tuple[tuple[str, str], ...] = (("2492", "華新科"),)
_DEFAULT_DISPOSITION: tuple[str, ...] = ("2492",)
# Continuous TW cash session (approx.)
_CONT_OPEN_MIN = 9 * 60
_CONT_CLOSE_MIN = 13 * 60 + 30
# First useful closed 30m window ends ~09:30 (open 09:00 + 6×5m).
_TA30M_READY_MIN = 9 * 60 + 30
# Inter-symbol gaps (rate-limit hygiene).
_YAHOO_GAP_SEC = 0.15
_FUBON_QUOTE_GAP_SEC = float(os.environ.get("OPS_LIVE_TA_FUBON_GAP_SEC", "0.05") or "0.05")
_FUBON_CANDLES_GAP_SEC = float(os.environ.get("OPS_LIVE_TA_CANDLES_GAP_SEC", "0.06") or "0.06")
_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_YAHOO_UA = "Mozilla/5.0 (compatible; ops-live-ta/1.1)"
# Candles TTL (quote still every poll ~15s).
_MOM_1M_TTL_SEC = float(os.environ.get("OPS_LIVE_TA_CANDLES_1M_TTL", "45") or "45")
_TA30M_CACHE_TTL_SEC = float(
    os.environ.get("OPS_LIVE_TA_CANDLES_5M_TTL")
    or os.environ.get("OPS_LIVE_TA_30M_TTL_SEC", "90")
    or "90"
)
# mom cache: sid → (fetched_at, closes, meta)
_MOM_1M_CACHE: dict[str, tuple[float, list[float], dict[str, Any]]] = {}
# 30m snap cache: sid → (fetched_at, anchors)
_TA30M_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_TA30M_BIAS_EPS = 0.15  # % · quiet zone around flat
# Champion fade30 gate · 0050 OR-inside (shared across ~19 stocks / poll).
_FADE30_RULE_ID = "fade_idx_or_inside"
_FADE30_BENCH_SID = "0050"
_FADE30_BENCH_TTL_SEC = float(
    os.environ.get("OPS_LIVE_TA_FADE30_BENCH_TTL", "120") or "120"
)
_FADE30_BENCH_CACHE: tuple[float, list[dict[str, Any]], dict[str, Any]] | None = None
_FADE30_DISCLAIMER_ZH = (
    "研究觀察·fade_idx_or_inside（fade∧0050 OR未破）；"
    "OOS研究約71%·非保證·非下單訊號"
)


def _quote_backend() -> str:
    """``fubon`` (default) or ``yahoo`` · env ``OPS_LIVE_TA_QUOTE``."""
    raw = (os.environ.get("OPS_LIVE_TA_QUOTE") or "fubon").strip().lower()
    return "yahoo" if raw in {"yahoo", "yfinance", "chart"} else "fubon"


def _yahoo_fallback_enabled() -> bool:
    """Emergency Yahoo for bars/price when Fubon misses · default off."""
    if _quote_backend() == "yahoo":
        return True
    raw = (os.environ.get("OPS_LIVE_TA_YAHOO_FALLBACK") or "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


_INDEX_DISPLAY_NAMES: dict[str, str] = {
    "IX0001": "加權指數",
    "TAIEX": "加權指數",
    "TWII": "加權指數",
    "IR0002": "電子指數",
}


def _yahoo_only_symbol(stock_id: str) -> bool:
    """Indices / Yahoo aliases — Fubon marketdata has no TEJ IX/IR quotes."""
    sid = (stock_id or "").strip().upper()
    if not sid:
        return False
    if sid.startswith("^"):
        return True
    if sid in _INDEX_DISPLAY_NAMES:
        return True
    if sid.startswith(("IX", "IR")) and len(sid) >= 5:
        return True
    return False


def _qget(obj: Any, *keys: str) -> Any:
    """Read camelCase / snake_case from dict or SDK object."""
    if obj is None:
        return None
    for key in keys:
        if isinstance(obj, dict):
            if key in obj and obj[key] is not None:
                return obj[key]
            continue
        val = getattr(obj, key, None)
        if val is not None:
            return val
        # last_price ↔ lastPrice
        if "_" in key:
            parts = key.split("_")
            camel = parts[0] + "".join(p.title() for p in parts[1:])
            val = getattr(obj, camel, None)
            if val is not None:
                return val
        else:
            # lastPrice → last_price
            snake = "".join(("_" + c.lower() if c.isupper() else c) for c in key)
            if snake != key:
                val = getattr(obj, snake, None)
                if val is not None:
                    return val
    return None


def _as_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        out = float(val)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def parse_fubon_marketdata_quote(raw: Any, *, stock_id: str) -> tuple[float | None, dict[str, Any]]:
    """Map Fubon/Fugle ``intraday.quote`` → last print + anchors meta."""
    meta: dict[str, Any] = {
        "quote_source": None,
        "price_source": None,
        "bar_source": None,
    }
    if raw is None:
        meta["fubon_error"] = "empty_quote"
        return None, meta

    last_trade = _qget(raw, "lastTrade", "last_trade") or {}
    last_trial = _qget(raw, "lastTrial", "last_trial") or {}
    is_trial = bool(_qget(raw, "isTrial", "is_trial"))
    trial_px = _as_float(_qget(last_trial, "price"))
    trade_px = _as_float(_qget(last_trade, "price"))
    last_px = _as_float(_qget(raw, "lastPrice", "last_price"))
    close_px = _as_float(_qget(raw, "closePrice", "close_price"))

    # Disposition / call-auction: ``isTrial`` → prefer lastTrial (試算價) so the
    # UI ticks between matches. lastTrade / closePrice stick until next auction.
    if is_trial and trial_px is not None:
        px = trial_px
        meta["price_source"] = "fubon_last_trial"
        meta["is_trial"] = True
    elif is_trial and last_px is not None:
        px = last_px
        meta["price_source"] = "fubon_last_price"
        meta["is_trial"] = True
    else:
        px = trade_px or last_px or close_px or trial_px
        if trade_px is not None:
            meta["price_source"] = "fubon_last_trade"
        elif last_px is not None:
            meta["price_source"] = "fubon_last_price"
        elif close_px is not None:
            meta["price_source"] = "fubon_close_price"
        elif trial_px is not None:
            meta["price_source"] = "fubon_last_trial"
        if is_trial:
            meta["is_trial"] = True

    # Mid bid/ask only if no trade print (e.g. thin names mid auction).
    if px is None:
        bids = _qget(raw, "bids") or []
        asks = _qget(raw, "asks") or []
        bid0 = _as_float(_qget(bids[0], "price")) if bids else None
        ask0 = _as_float(_qget(asks[0], "price")) if asks else None
        if bid0 and ask0:
            px = _round_px((bid0 + ask0) / 2.0)
            meta["price_source"] = "fubon_bid_ask_mid"
        elif ask0:
            px = ask0
            meta["price_source"] = "fubon_ask"
        elif bid0:
            px = bid0
            meta["price_source"] = "fubon_bid"

    if px is None:
        meta["fubon_error"] = "no_last_price"
        return None, meta

    px = _round_px(float(px))
    meta["quote_source"] = f"fubon:marketdata:{stock_id}"
    if trial_px is not None:
        meta["fubon_trial_price"] = _round_px(trial_px)
    if trade_px is not None:
        meta["fubon_last_trade_price"] = _round_px(trade_px)
    prev = _as_float(
        _qget(raw, "previousClose", "previous_close", "referencePrice", "reference_price")
    )
    if prev is not None:
        meta["fubon_prev_close"] = _round_px(prev)
    chg_pct = _qget(raw, "changePercent", "change_percent")
    try:
        if chg_pct is not None:
            meta["fubon_change_percent"] = float(chg_pct)
    except (TypeError, ValueError):
        pass
    bid = None
    ask = None
    bids = _qget(raw, "bids") or []
    asks = _qget(raw, "asks") or []
    if bids:
        bid = _as_float(_qget(bids[0], "price"))
    if asks:
        ask = _as_float(_qget(asks[0], "price"))
    if bid is not None:
        meta["fubon_bid"] = bid
    if ask is not None:
        meta["fubon_ask"] = ask
    name = _qget(raw, "name")
    if name:
        meta["fubon_name"] = str(name)
    return px, meta


def fetch_fubon_marketdata_quotes(
    symbols: list[str],
    *,
    session: Any | None = None,
    gap_sec: float = _FUBON_QUOTE_GAP_SEC,
) -> tuple[dict[str, tuple[float | None, dict[str, Any]]], str | None]:
    """One login · N ``intraday.quote`` calls. Returns (by_sid, connect_error).

    ``session`` may be a pre-opened ``FubonSession`` (caller owns lifecycle).
    When ``session`` is None this helper connects and does not close the SDK
    (same pattern as holdings_pulse / premarket collect).
    """
    out: dict[str, tuple[float | None, dict[str, Any]]] = {}
    if not symbols:
        return out, None
    try:
        from order.fubon_session import connect_fubon

        sess = session if session is not None else connect_fubon(realtime=True)
        rest_stock = sess.sdk.marketdata.rest_client.stock
    except Exception as exc:  # noqa: BLE001 — poll must degrade to Yahoo
        return out, f"fubon_connect:{exc}"

    for i, sid in enumerate(symbols):
        sid_s = str(sid).strip()
        if not sid_s:
            continue
        if i > 0 and gap_sec > 0:
            time.sleep(gap_sec)
        try:
            raw = rest_stock.intraday.quote(symbol=sid_s)
        except Exception as exc:  # noqa: BLE001
            out[sid_s] = (None, {"quote_source": None, "fubon_error": str(exc)[:200]})
            continue
        out[sid_s] = parse_fubon_marketdata_quote(raw, stock_id=sid_s)
    return out, None


def parse_fubon_candles(raw: Any, *, stock_id: str, timeframe: str | int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Map Fubon ``intraday.candles`` → list of {ts, open, high, low, close, volume}."""
    tf = str(timeframe).strip() or "1"
    meta: dict[str, Any] = {}
    if raw is None:
        meta["fubon_candles_error"] = "empty_candles"
        return [], meta

    data = _qget(raw, "data") or []
    if not isinstance(data, list):
        meta["fubon_candles_error"] = "bad_data"
        return [], meta

    out: list[dict[str, Any]] = []
    for row in data:
        close = _as_float(_qget(row, "close", "Close"))
        if close is None:
            continue
        o = _as_float(_qget(row, "open", "Open")) or close
        h = _as_float(_qget(row, "high", "High")) or close
        lo = _as_float(_qget(row, "low", "Low")) or close
        vol_raw = _qget(row, "volume", "Volume")
        try:
            vol = float(vol_raw) if vol_raw is not None else 0.0
        except (TypeError, ValueError):
            vol = 0.0
        ts_raw = _qget(row, "date", "time", "ts", "datetime")
        ts: datetime | None = None
        if isinstance(ts_raw, datetime):
            ts = ts_raw.astimezone(_TPE) if ts_raw.tzinfo else ts_raw.replace(tzinfo=_TPE)
        elif isinstance(ts_raw, (int, float)):
            # seconds or ms epoch
            epoch = float(ts_raw)
            if epoch > 1e12:
                epoch /= 1000.0
            ts = datetime.fromtimestamp(epoch, tz=_TPE)
        elif isinstance(ts_raw, str) and ts_raw.strip():
            text = ts_raw.strip().replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                # e.g. 2026-07-24 09:01:00
                try:
                    parsed = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    parsed = None
            if parsed is not None:
                ts = parsed.astimezone(_TPE) if parsed.tzinfo else parsed.replace(tzinfo=_TPE)
        if ts is None:
            continue
        out.append(
            {
                "ts": ts,
                "open": float(o),
                "high": float(h),
                "low": float(lo),
                "close": float(close),
                "volume": max(0.0, vol),
            }
        )
    out.sort(key=lambda b: b["ts"])
    src = f"fubon:candles:{stock_id}:{tf}"
    if tf == "1":
        meta["bar_source"] = src
    else:
        meta["ta_30m_source"] = src
    if not out:
        meta["fubon_candles_error"] = "empty_parsed"
    return out, meta


def fetch_fubon_candles(
    stock_id: str,
    *,
    timeframe: str | int = "1",
    rest_stock: Any | None = None,
    session: Any | None = None,
    gap_sec: float = 0.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One ``intraday.candles`` call. Caller should reuse ``rest_stock`` across symbols."""
    sid = stock_id.strip()
    tf = str(timeframe).strip() or "1"
    meta: dict[str, Any] = {}
    if not sid:
        meta["fubon_candles_error"] = "empty_symbol"
        return [], meta
    if gap_sec > 0:
        time.sleep(gap_sec)
    try:
        if rest_stock is None:
            from order.fubon_session import connect_fubon

            sess = session if session is not None else connect_fubon(realtime=True)
            rest_stock = sess.sdk.marketdata.rest_client.stock
        raw = rest_stock.intraday.candles(symbol=sid, timeframe=tf)
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        meta["fubon_candles_error"] = err[:200]
        if "429" in err or "Rate limit" in err.lower():
            meta["fubon_rate_limited"] = True
        return [], meta
    return parse_fubon_candles(raw, stock_id=sid, timeframe=tf)


def mom_meta_from_1m_bars(
    bars_1m: list[dict[str, Any]],
    *,
    last_print: float | None,
    bar_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build mom_* anchors from Fubon (or compatible) 1m OHLCV bars."""
    meta = dict(bar_meta or {})
    closes = [float(b["close"]) for b in bars_1m if b.get("close") is not None]
    meta["n_1m_bars"] = len(closes)
    meta["momentum_horizon"] = "1m_bars_lookback_1_2"
    if not closes:
        return meta
    px = float(last_print) if last_print is not None else float(closes[-1])
    meta.update(_mom_from_closes(closes, px))
    if bars_1m:
        meta["bar_time"] = bars_1m[-1]["ts"].isoformat()
    return meta


def get_mom_1m_anchors(
    stock_id: str,
    *,
    last_print: float | None = None,
    rest_stock: Any | None = None,
    session: Any | None = None,
    force_refresh: bool = False,
    cache_ttl_sec: float | None = None,
) -> dict[str, Any]:
    """1m short-momentum anchors with TTL cache (default ~45s)."""
    ttl = _MOM_1M_TTL_SEC if cache_ttl_sec is None else float(cache_ttl_sec)
    cached = _MOM_1M_CACHE.get(stock_id)
    now_ts = time.time()
    if not force_refresh and cached is not None and (now_ts - cached[0]) < ttl:
        closes, meta0 = cached[1], dict(cached[2])
        age = now_ts - cached[0]
        meta0["mom_cached_age_sec"] = round(age, 1)
        px = float(last_print) if last_print is not None else (closes[-1] if closes else None)
        if px is not None and closes:
            meta0.update(_mom_from_closes(closes, px))
            meta0["n_1m_bars"] = len(closes)
        return meta0

    yahoo_only = _yahoo_only_symbol(stock_id)
    if yahoo_only:
        bars, bar_meta = [], {}
    else:
        bars, bar_meta = fetch_fubon_candles(
            stock_id, timeframe="1", rest_stock=rest_stock, session=session
        )
    if not bars and (yahoo_only or _yahoo_fallback_enabled()):
        ypx, ymeta = fetch_yahoo_chart_quote(stock_id)
        out = {
            "bar_source": ymeta.get("quote_source"),
            "momentum_horizon": ymeta.get("momentum_horizon"),
            "mom_1bar_pct": ymeta.get("mom_1bar_pct"),
            "mom_2bar_pct": ymeta.get("mom_2bar_pct"),
            "n_1m_bars": ymeta.get("n_1m_bars"),
            "bar_source_fallback": "yahoo",
        }
        if bar_meta.get("fubon_candles_error"):
            out["fubon_candles_error"] = bar_meta["fubon_candles_error"]
        if ypx is None and ymeta.get("yahoo_error"):
            out["bar_source_error"] = ymeta.get("yahoo_error")
        closes = []
        # Seed cache from yahoo 1m if chart returned closes via side path — mom already in ymeta
        _MOM_1M_CACHE[stock_id] = (now_ts, closes, dict(out))
        return out

    if not bars:
        # Stale cache better than empty
        if cached is not None:
            closes, meta0 = cached[1], dict(cached[2])
            meta0["mom_cached_age_sec"] = round(now_ts - cached[0], 1)
            meta0["fubon_candles_error"] = bar_meta.get("fubon_candles_error") or "fetch_failed"
            px = float(last_print) if last_print is not None else (closes[-1] if closes else None)
            if px is not None and closes:
                meta0.update(_mom_from_closes(closes, px))
            return meta0
        return {
            "bar_source": None,
            "fubon_candles_error": bar_meta.get("fubon_candles_error") or "no_1m_bars",
            "momentum_horizon": "1m_bars_lookback_1_2",
        }

    closes = [float(b["close"]) for b in bars]
    meta = mom_meta_from_1m_bars(bars, last_print=last_print, bar_meta=bar_meta)
    _MOM_1M_CACHE[stock_id] = (now_ts, closes, dict(meta))
    return meta


def clear_mom_1m_cache() -> None:
    _MOM_1M_CACHE.clear()


def fetch_last_print(
    stock_id: str,
    *,
    fubon_quote: tuple[float | None, dict[str, Any]] | None = None,
    prefer_fubon: bool | None = None,
    rest_stock: Any | None = None,
    session: Any | None = None,
    attach_mom: bool = True,
) -> tuple[float | None, dict[str, Any]]:
    """Prefer Fubon last/% · attach Fubon 1m mom (TTL). Yahoo only if fallback on.

    Index symbols (IX0001 等) always use Yahoo — Fubon has no TEJ index quotes.
    """
    yahoo_only = _yahoo_only_symbol(stock_id)
    use_fubon = (
        False
        if yahoo_only
        else (_quote_backend() == "fubon" if prefer_fubon is None else prefer_fubon)
    )
    allow_yahoo = True if yahoo_only else _yahoo_fallback_enabled()
    fubon_px: float | None = None
    fubon_meta: dict[str, Any] = {}
    if use_fubon and fubon_quote is not None:
        fubon_px, fubon_meta = fubon_quote
    elif use_fubon and fubon_quote is None:
        by_sid, err = fetch_fubon_marketdata_quotes([stock_id], session=session)
        if err:
            fubon_meta = {"fubon_error": err}
        else:
            fubon_px, fubon_meta = by_sid.get(stock_id, (None, {}))

    yahoo_px: float | None = None
    yahoo_meta: dict[str, Any] = {}
    if (not use_fubon) or (fubon_px is None and allow_yahoo) or (
        attach_mom and allow_yahoo and use_fubon is False
    ):
        if not use_fubon or fubon_px is None:
            yahoo_px, yahoo_meta = fetch_last_print_yahoo(stock_id)

    if fubon_px is not None:
        meta = dict(fubon_meta)
        if attach_mom:
            mom = get_mom_1m_anchors(
                stock_id,
                last_print=fubon_px,
                rest_stock=rest_stock,
                session=session,
            )
            for key, val in mom.items():
                if val is not None:
                    meta[key] = val
        return fubon_px, meta

    if allow_yahoo:
        if not yahoo_meta:
            yahoo_px, yahoo_meta = fetch_last_print_yahoo(stock_id)
        meta = dict(yahoo_meta)
        if fubon_meta.get("fubon_error"):
            meta["fubon_error"] = fubon_meta["fubon_error"]
        elif use_fubon:
            meta.setdefault("fubon_error", "no_quote")
        return yahoo_px, meta

    # Fubon miss · no Yahoo fallback
    meta = dict(fubon_meta)
    meta.setdefault("fubon_error", meta.get("fubon_error") or "no_quote")
    if attach_mom:
        mom = get_mom_1m_anchors(
            stock_id,
            last_print=None,
            rest_stock=rest_stock,
            session=session,
        )
        meta.update({k: v for k, v in mom.items() if v is not None})
    return None, meta


def open_fubon_rest_stock(
    session: Any | None = None,
) -> tuple[Any | None, Any | None, str | None]:
    """Return (rest_stock, session_or_none, connect_error)."""
    try:
        from order.fubon_session import connect_fubon

        sess = session if session is not None else connect_fubon(realtime=True)
        return sess.sdk.marketdata.rest_client.stock, sess, None
    except Exception as exc:  # noqa: BLE001
        return None, None, f"fubon_connect:{exc}"


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


def _round_px(px: float) -> float:
    """Drop float noise (Yahoo often returns 60.599998…)."""
    if px >= 1000:
        return round(px, 1)
    if px >= 100:
        return round(px, 1)
    return round(px, 2)


def _mom_from_closes(closes: list[float], px: float) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if len(closes) >= 2 and closes[-2] > 0:
        out["mom_1bar_pct"] = round((px / closes[-2] - 1.0) * 100, 3)
    if len(closes) >= 3 and closes[-3] > 0:
        out["mom_2bar_pct"] = round((px / closes[-3] - 1.0) * 100, 3)
    return out


def _floor_30m(ts: datetime) -> datetime:
    ts = ts.astimezone(_TPE)
    return ts.replace(minute=(ts.minute // 30) * 30, second=0, microsecond=0)


def _parse_chart_ohlcv(
    chart: dict[str, Any],
) -> list[dict[str, Any]]:
    """Parse Yahoo chart result → list of {ts, open, high, low, close, volume}."""
    timestamps = chart.get("timestamp") or []
    quote = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    out: list[dict[str, Any]] = []
    n = len(timestamps)
    for i in range(n):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        try:
            close = float(c)
            o = float(opens[i]) if i < len(opens) and opens[i] is not None else close
            h = float(highs[i]) if i < len(highs) and highs[i] is not None else close
            lo = float(lows[i]) if i < len(lows) and lows[i] is not None else close
            vol = float(volumes[i]) if i < len(volumes) and volumes[i] is not None else 0.0
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        ts = datetime.fromtimestamp(int(timestamps[i]), tz=_TPE)
        out.append(
            {
                "ts": ts,
                "open": o,
                "high": h,
                "low": lo,
                "close": close,
                "volume": max(0.0, vol),
            }
        )
    return out


def session_vwap_from_bars(bars: list[dict[str, Any]]) -> float | None:
    """Session VWAP from typical price × volume (observe · not broker VWAP)."""
    num = 0.0
    den = 0.0
    for b in bars:
        vol = float(b.get("volume") or 0.0)
        if vol <= 0:
            continue
        typical = (float(b["high"]) + float(b["low"]) + float(b["close"])) / 3.0
        num += typical * vol
        den += vol
    if den <= 0:
        return None
    return num / den


def last_closed_30m_window(
    bars_5m: list[dict[str, Any]],
    *,
    now: datetime,
) -> tuple[list[dict[str, Any]], datetime | None]:
    """Return the last fully closed 30m bucket (6×5m) ending at/before now.

    TW cash open 09:00 → first closed window ends 09:30. Incomplete current
    half-hour is excluded (PIT / no look-ahead into forming bar).
    """
    now = now.astimezone(_TPE)
    if not bars_5m:
        return [], None
    # End of last closed 30m slot (e.g. at 10:05 → 10:00).
    end = _floor_30m(now)
    if now < end + timedelta(seconds=5):
        # Still inside the minute of the boundary; treat as not yet closed.
        end = end - timedelta(minutes=30)
    if end.hour * 60 + end.minute < _TA30M_READY_MIN:
        return [], None
    start = end - timedelta(minutes=30)
    window = [b for b in bars_5m if start <= b["ts"] < end]
    if len(window) < 4:
        # Yahoo sometimes drops a bar; still usable if ≥4 of 6.
        return [], end
    return window, end


def compute_ta_30m_snapshot(
    bars_5m: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    last_print: float | None = None,
    bench_5m: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Pure observe-only 30m fields from session 5m bars (no network).

    Ready after first closed half-hour (~09:30) with enough 5m bars.
    Bias is descriptive (vs bar open + session VWAP), not a forecast.
    Optional ``bench_5m`` (0050) gates research ``fade30_observe`` via
    ``fade_idx_or_inside`` (fade_near_ext ∧ index OR intact).
    """
    now = (now or datetime.now(_TPE)).astimezone(_TPE)
    base: dict[str, Any] = {
        "ta_30m_ready": False,
        "ta_30m_ret_pct": None,
        "ta_30m_vs_open_pct": None,
        "ta_30m_vs_vwap_pct": None,
        "ta_30m_bias": None,
        "ta_30m_note_zh": "觀察用・非下單訊號；等第一根閉合 30m（約 09:30）。",
        "ta_30m_bar_end": None,
        "ta_30m_n_5m": 0,
        "fade30_observe": None,
        "fade30_rule": _FADE30_RULE_ID,
        "idx_or_inside": None,
    }
    mins = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        base["ta_30m_note_zh"] = "假日無 30m 觀察。"
        return base
    if mins < _TA30M_READY_MIN:
        base["ta_30m_note_zh"] = "開盤前半小時觀察中；約 09:30 後才有閉合 30m。"
        return base

    day = now.date()
    session = [
        b
        for b in bars_5m
        if b["ts"].astimezone(_TPE).date() == day
        and (b["ts"].hour * 60 + b["ts"].minute) >= _CONT_OPEN_MIN
    ]
    window, bar_end = last_closed_30m_window(session, now=now)
    if not window or bar_end is None:
        base["ta_30m_n_5m"] = len(session)
        base["ta_30m_note_zh"] = "尚缺閉合 30m（需約 6×5m）；觀察用・非下單訊號。"
        return base

    o = float(window[0]["open"])
    c = float(window[-1]["close"])
    hi = max(float(b["high"]) for b in window)
    lo = min(float(b["low"]) for b in window)
    if o <= 0 or c <= 0:
        base["ta_30m_note_zh"] = "30m OHLC 無效；觀察用・非下單訊號。"
        return base

    ret_pct = round((c / o - 1.0) * 100, 3)
    vs_open = ret_pct
    vwap = session_vwap_from_bars(session)
    ref_px = float(last_print) if last_print and last_print > 0 else c
    vs_vwap: float | None = None
    if vwap and vwap > 0:
        vs_vwap = round((ref_px / vwap - 1.0) * 100, 3)

    # Simple confluence bias (expert-practice-ish · not a hit-rate claim).
    bias = "中性"
    if ret_pct >= _TA30M_BIAS_EPS and (vs_vwap is None or vs_vwap >= 0):
        bias = "偏多"
    elif ret_pct <= -_TA30M_BIAS_EPS and (vs_vwap is None or vs_vwap <= 0):
        bias = "偏空"
    elif vs_vwap is not None and abs(ret_pct) < _TA30M_BIAS_EPS:
        if vs_vwap >= _TA30M_BIAS_EPS:
            bias = "偏多"
        elif vs_vwap <= -_TA30M_BIAS_EPS:
            bias = "偏空"

    note_parts = [
        f"閉合30m至 {bar_end.strftime('%H:%M')}",
        f"bar {ret_pct:+.2f}%",
        f"區間 {lo:.2f}–{hi:.2f}",
    ]
    if vs_vwap is not None:
        note_parts.append(f"現價相對VWAP {vs_vwap:+.2f}%")
    note_parts.append("觀察用・非下單訊號")

    out: dict[str, Any] = {
        "ta_30m_ready": True,
        "ta_30m_ret_pct": ret_pct,
        "ta_30m_vs_open_pct": vs_open,
        "ta_30m_vs_vwap_pct": vs_vwap,
        "ta_30m_bias": bias,
        "ta_30m_note_zh": "｜".join(note_parts),
        "ta_30m_bar_end": bar_end.isoformat(),
        "ta_30m_n_5m": len(window),
        "ta_30m_high": _round_px(hi),
        "ta_30m_low": _round_px(lo),
        "fade30_observe": None,
        "fade30_rule": _FADE30_RULE_ID,
        "idx_or_inside": None,
    }

    # Research champion observe badge — never Order / never live hit-rate claim.
    try:
        from research.intraday_direction_thermometer import (  # noqa: WPS433
            Bar,
            fade_near_ext_from_bars,
        )

        # PIT: only completed 5m bars strictly before closed 30m end.
        thermo_bars = [
            Bar(
                ts=b["ts"],
                open=float(b["open"]),
                high=float(b["high"]),
                low=float(b["low"]),
                close=float(b["close"]),
                volume=float(b.get("volume") or 0.0),
            )
            for b in session
            if b["ts"] < bar_end
        ]
        asof = thermo_bars[-1].ts if thermo_bars else bar_end
        inside = idx_or_inside_at(bench_5m or [], asof=asof)
        out["idx_or_inside"] = inside
        layer = fade_near_ext_from_bars(thermo_bars, midday_only=True)
        fade_on = bool(layer.ready and layer.temp in (-1, 1))
        # Champion: fade_near_ext ∧ 0050 OR-inside (unknown OR → no badge).
        if fade_on and inside is True:
            out["fade30_observe"] = {
                "temp": layer.temp,
                "label": layer.label,
                "action": layer.action,
                "reason": layer.reason,
                "rule": _FADE30_RULE_ID,
                "idx_or_inside": True,
                "disclaimer_zh": _FADE30_DISCLAIMER_ZH,
            }
            out["ta_30m_note_zh"] += "｜研究觀察 fade30·大盤OR未破"
    except Exception:  # noqa: BLE001 — observe must not break poll
        pass
    return out


def idx_or_inside_at(
    bench_5m: list[dict[str, Any]],
    *,
    asof: datetime,
) -> bool | None:
    """Whether bench (0050) close at ``asof`` is inside session opening range.

    Mirrors research ``fade_idx_or_inside`` / TRACK_VS_MARKET: first
    ``TA30M_OR_BARS`` session bars define OR; last bench close ≤ asof must
    sit in [OR_lo, OR_hi]. Returns ``None`` when bars are insufficient.
    """
    if not bench_5m:
        return None
    try:
        from research.intraday_direction_thermometer import (  # noqa: WPS433
            Bar,
            TA30M_OR_BARS,
            opening_range_hl,
        )
    except Exception:  # noqa: BLE001
        return None

    asof_tpe = asof.astimezone(_TPE)
    day = asof_tpe.date()
    bprev = [
        Bar(
            ts=b["ts"],
            open=float(b["open"]),
            high=float(b["high"]),
            low=float(b["low"]),
            close=float(b["close"]),
            volume=float(b.get("volume") or 0.0),
        )
        for b in bench_5m
        if b["ts"].astimezone(_TPE).date() == day
        and (b["ts"].hour * 60 + b["ts"].minute) >= _CONT_OPEN_MIN
        and b["ts"] <= asof_tpe
    ]
    if len(bprev) < TA30M_OR_BARS:
        return None
    or_hl = opening_range_hl(bprev)
    if or_hl is None:
        return None
    or_hi, or_lo = or_hl
    bc = bprev[-1].close
    return bool(or_lo <= bc <= or_hi)


def get_fade30_bench_5m(
    *,
    force_refresh: bool = False,
    rest_stock: Any | None = None,
    session: Any | None = None,
    cache_ttl_sec: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """0050 5m bars for fade30 OR gate · aggressive shared TTL cache."""
    global _FADE30_BENCH_CACHE
    ttl = _FADE30_BENCH_TTL_SEC if cache_ttl_sec is None else float(cache_ttl_sec)
    cached = _FADE30_BENCH_CACHE
    if (
        not force_refresh
        and cached is not None
        and (time.time() - cached[0]) < ttl
    ):
        bars, meta = cached[1], dict(cached[2])
        meta["fade30_bench_cached_age_sec"] = round(time.time() - cached[0], 1)
        return list(bars), meta

    meta: dict[str, Any] = {"fade30_bench": _FADE30_BENCH_SID}
    bars: list[dict[str, Any]] = []
    if rest_stock is not None or _quote_backend() == "fubon":
        bars, fmeta = fetch_fubon_candles(
            _FADE30_BENCH_SID,
            timeframe="5",
            rest_stock=rest_stock,
            session=session,
            gap_sec=0.0,
        )
        meta.update(fmeta)
        if bars:
            meta["fade30_bench_source"] = meta.get("fubon_candles_source") or "fubon"
    if not bars and _yahoo_fallback_enabled():
        bars, ymeta = fetch_yahoo_5m_bars(_FADE30_BENCH_SID)
        meta.update(ymeta)
        if bars:
            meta["fade30_bench_source"] = ymeta.get("ta_30m_source") or "yahoo"
            meta["fade30_bench_source_fallback"] = "yahoo"
    if bars:
        _FADE30_BENCH_CACHE = (time.time(), list(bars), dict(meta))
    elif cached is not None:
        # Stale reuse on miss — avoid hammering while poll continues.
        bars, meta = list(cached[1]), dict(cached[2])
        meta["fade30_bench_stale"] = True
        meta["fade30_bench_cached_age_sec"] = round(time.time() - cached[0], 1)
    return bars, meta


def get_ta_30m_anchors(
    stock_id: str,
    *,
    now: datetime | None = None,
    last_print: float | None = None,
    bars_5m: list[dict[str, Any]] | None = None,
    force_refresh: bool = False,
    cache_ttl_sec: float | None = None,
    rest_stock: Any | None = None,
    session: Any | None = None,
    bench_5m: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """30m observe anchors with TTL cache (default ~90s). Prefer Fubon 5m candles."""
    now = (now or datetime.now(_TPE)).astimezone(_TPE)
    ttl = _TA30M_CACHE_TTL_SEC if cache_ttl_sec is None else float(cache_ttl_sec)
    cached = _TA30M_CACHE.get(stock_id)
    if (
        not force_refresh
        and bars_5m is None
        and cached is not None
        and (time.time() - cached[0]) < ttl
    ):
        out = dict(cached[1])
        out["ta_30m_cached_age_sec"] = round(time.time() - cached[0], 1)
        return out

    meta: dict[str, Any] = {}
    yahoo_only = _yahoo_only_symbol(stock_id)
    if bars_5m is None:
        if yahoo_only:
            bars_5m, meta = [], {}
        else:
            bars_5m, meta = fetch_fubon_candles(
                stock_id,
                timeframe="5",
                rest_stock=rest_stock,
                session=session,
                gap_sec=_FUBON_CANDLES_GAP_SEC if rest_stock is not None else 0.0,
            )
        if not bars_5m and (yahoo_only or _yahoo_fallback_enabled()):
            bars_5m, ymeta = fetch_yahoo_5m_bars(stock_id)
            meta = dict(ymeta)
            meta["ta_30m_source_fallback"] = "yahoo"
            if ymeta.get("ta_30m_source"):
                meta["ta_30m_source"] = ymeta["ta_30m_source"]
        elif not bars_5m and cached is not None:
            out = dict(cached[1])
            out["ta_30m_cached_age_sec"] = round(time.time() - cached[0], 1)
            out["fubon_candles_error"] = meta.get("fubon_candles_error") or "fetch_failed"
            return out

    bench_meta: dict[str, Any] = {}
    if bench_5m is None:
        bench_5m, bench_meta = get_fade30_bench_5m(
            rest_stock=rest_stock,
            session=session,
        )
    snap = compute_ta_30m_snapshot(
        bars_5m, now=now, last_print=last_print, bench_5m=bench_5m
    )
    snap.update(meta)
    if bench_meta:
        for k in (
            "fade30_bench",
            "fade30_bench_source",
            "fade30_bench_source_fallback",
            "fade30_bench_cached_age_sec",
            "fade30_bench_stale",
        ):
            if k in bench_meta:
                snap[k] = bench_meta[k]
    if bars_5m or snap.get("ta_30m_ready"):
        _TA30M_CACHE[stock_id] = (time.time(), dict(snap))
    return snap


def clear_ta_30m_cache() -> None:
    global _FADE30_BENCH_CACHE
    _TA30M_CACHE.clear()
    _FADE30_BENCH_CACHE = None


def fetch_yahoo_5m_bars(stock_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Yahoo Chart v8 5m bars for today (lightweight vs yfinance)."""
    meta: dict[str, Any] = {"ta_30m_source": None}
    for symbol in _tw_yahoo_symbol_candidates(stock_id):
        try:
            resp = requests.get(
                _YAHOO_CHART_URL.format(symbol=symbol),
                params={"interval": "5m", "range": "1d", "includePrePost": "false"},
                headers={"User-Agent": _YAHOO_UA},
                timeout=12,
            )
        except requests.RequestException as exc:
            meta["ta_30m_error"] = str(exc)[:200]
            continue
        if resp.status_code >= 400:
            meta["ta_30m_error"] = f"http{resp.status_code}"
            continue
        try:
            payload = resp.json()
        except ValueError as exc:
            meta["ta_30m_error"] = str(exc)[:200]
            continue
        results = ((payload.get("chart") or {}).get("result")) or []
        if not results:
            err = ((payload.get("chart") or {}).get("error")) or {}
            if err:
                meta["ta_30m_error"] = str(err)[:200]
            continue
        bars = _parse_chart_ohlcv(results[0])
        if not bars:
            meta["ta_30m_error"] = "empty_5m"
            continue
        meta["ta_30m_source"] = f"yahoo_chart:{symbol}:5m"
        return bars, meta
    return [], meta


def fetch_yahoo_chart_quote(stock_id: str) -> tuple[float | None, dict[str, Any]]:
    """Light Yahoo Chart v8 quote: ``regularMarketPrice`` + optional 1m closes.

    Prefer this over full yfinance history — updates with each poll (not stuck
    waiting for a closed 1m bar) and works when 1m bars are empty (e.g. some
    disposition names mid call-auction).
    """
    meta: dict[str, Any] = {"quote_source": None}
    for symbol in _tw_yahoo_symbol_candidates(stock_id):
        try:
            resp = requests.get(
                _YAHOO_CHART_URL.format(symbol=symbol),
                params={"interval": "1m", "range": "1d", "includePrePost": "false"},
                headers={"User-Agent": _YAHOO_UA},
                timeout=12,
            )
        except requests.RequestException as exc:
            meta["yahoo_error"] = str(exc)[:200]
            continue
        if resp.status_code >= 400:
            meta["yahoo_error"] = f"http{resp.status_code}"
            continue
        try:
            payload = resp.json()
        except ValueError as exc:
            meta["yahoo_error"] = str(exc)[:200]
            continue
        results = ((payload.get("chart") or {}).get("result")) or []
        if not results:
            err = ((payload.get("chart") or {}).get("error")) or {}
            if err:
                meta["yahoo_error"] = str(err)[:200]
            continue
        chart = results[0]
        m = chart.get("meta") or {}
        raw_px = m.get("regularMarketPrice")
        if raw_px is None:
            continue
        try:
            px = _round_px(float(raw_px))
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        meta["quote_source"] = f"yahoo_chart:{symbol}:1m"
        meta["yahoo_prev_close"] = m.get("chartPreviousClose") or m.get("previousClose")
        meta["momentum_horizon"] = "1m_bars_lookback_1_2"
        closes_raw = (((chart.get("indicators") or {}).get("quote") or [{}])[0].get("close")) or []
        closes = [float(c) for c in closes_raw if c is not None]
        meta["n_1m_bars"] = len(closes)
        if closes:
            meta.update(_mom_from_closes(closes, px))
        return px, meta
    return None, meta


def fetch_last_print_yahoo(stock_id: str) -> tuple[float | None, dict[str, Any]]:
    """Latest print + short momentum · Chart API first, yfinance 1d history fallback."""
    px, meta = fetch_yahoo_chart_quote(stock_id)
    if px is not None:
        return px, meta

    # Fallback: short history only (avoid 5‑day 1m pull — slow + sticky).
    # Lazy-import: needs pandas/yfinance (.venv); skip cleanly under .venv-fubon.
    try:
        from yahoo_chart_sync import fetch_yahoo_intraday_df
    except ImportError as exc:
        meta["yahoo_error"] = f"hist_fallback_unavailable:{exc}"
        return None, meta

    today = datetime.now(_TPE).date()
    start = today - timedelta(days=1)
    for interval in ("1m", "5m"):
        for symbol in _tw_yahoo_symbol_candidates(stock_id):
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
            last = _round_px(float(close.iloc[-1]))
            meta["quote_source"] = f"yahoo_hist:{symbol}:{interval}"
            meta["bar_time"] = str(close.index[-1])
            closes = [_round_px(float(x)) for x in close.tolist()]
            meta.update(_mom_from_closes(closes, last))
            if interval == "1m":
                meta["momentum_horizon"] = "1m_bars_lookback_1_2"
            else:
                meta["momentum_horizon"] = f"{interval}_bars_lookback_1_2"
            return last, meta
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
    known = _INDEX_DISPLAY_NAMES.get((stock_id or "").strip().upper())
    if known:
        return known
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
    """Latest ``order_holdings_snapshot`` with shares > 0 (may be stale · fallback only)."""
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


def load_holdings_from_fubon_session(session: Any) -> list[tuple[str, str | None]]:
    """Live Fubon inventories with qty > 0（當下持倉）."""
    try:
        from order.fubon_account import _held_qty, account_snapshot
    except ImportError:
        return []
    try:
        snap = account_snapshot(session)
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple[str, str | None]] = []
    for h in snap.get("holdings") or []:
        if not isinstance(h, dict):
            continue
        try:
            if int(_held_qty(h)) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        sid = str(h.get("stock_no") or h.get("stock_id") or "").strip()
        if not sid:
            continue
        name = str(h.get("stock_name") or h.get("name") or "").strip() or None
        out.append((sid, name))
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


def load_live_ta_watch_from_ops() -> list[tuple[str, str | None]]:
    """Website-managed extras from ``ops.live_ta_watch`` / ``ops_live_ta_watch``."""
    if not supabase_ops_configured():
        return []
    try:
        from ops_console_sync import _headers, _rest

        resp = requests.get(
            _rest("ops_live_ta_watch"),
            headers=_headers(prefer="return=representation"),
            params={"select": "stock_id,stock_name", "order": "stock_id"},
            timeout=30,
        )
    except Exception:  # noqa: BLE001
        return []
    if resp.status_code >= 400:
        return []
    rows = resp.json()
    if not isinstance(rows, list):
        return []
    out: list[tuple[str, str | None]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        sid = str(r.get("stock_id") or "").strip()
        if not sid:
            continue
        name = str(r.get("stock_name") or "").strip() or None
        out.append((sid, name))
    return out


def resolve_live_ta_universe(
    conn: sqlite3.Connection | None = None,
    *,
    extras_raw: str | None = None,
    include_ops_holdings: bool = True,
    live_holdings: list[tuple[str, str | None]] | None = None,
    fubon_session: Any | None = None,
) -> list[tuple[str, str | None]]:
    """Current holdings ∪ website watch ∪ ``OPS_LIVE_TA_STOCKS`` extras.

    Priority for holdings (do **not** union stale sources):
      1. ``live_holdings`` if provided
      2. live Fubon session inventories if ``fubon_session`` given
      3. ``ops.holdings`` (cloud · evening／manual sync)
      4. local ``order_holdings_snapshot`` fallback only if ops empty

    Then merge ``ops.live_ta_watch`` (website) + env/CLI extras.
    """
    env_extras = parse_stock_list(extras_raw, default_if_empty=False)
    web_extras = load_live_ta_watch_from_ops()
    primary: list[tuple[str, str | None]] = []
    if live_holdings is not None:
        primary = list(live_holdings)
    elif fubon_session is not None:
        primary = load_holdings_from_fubon_session(fubon_session)
    if not primary and include_ops_holdings:
        primary = load_holdings_from_ops()
    if not primary:
        primary = load_holdings_from_db(conn)
    merged = _merge_pairs(primary, web_extras, env_extras)
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
    ta_30m: dict[str, Any] | None = None,
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
    # Only auto-fetch when caller did not supply a quote attempt (avoids a second
    # Fubon login after run_live_ta_poll already batched).
    if px is None and quote_meta is None:
        px, qmeta = fetch_last_print(stock_id)
        meta.update(qmeta)
    if px is not None:
        px = _round_px(float(px))
    # Prefer Fubon previousClose (aligned with realtime last), then Yahoo
    # session previousClose. Local stock_daily_bars can lag days and inflate
    # 漲跌% (e.g. 6505).
    prev_db = fetch_prev_close_db(conn, stock_id)
    prev_fubon: float | None = None
    fprev = meta.get("fubon_prev_close")
    try:
        if fprev is not None:
            prev_fubon = float(fprev)
            if prev_fubon <= 0:
                prev_fubon = None
    except (TypeError, ValueError):
        prev_fubon = None
    prev_yahoo: float | None = None
    yprev = meta.get("yahoo_prev_close")
    try:
        if yprev is not None:
            prev_yahoo = float(yprev)
            if prev_yahoo <= 0:
                prev_yahoo = None
    except (TypeError, ValueError):
        prev_yahoo = None
    if prev_fubon is not None:
        prev = prev_fubon
        prev_src = "fubon"
    elif prev_yahoo is not None:
        prev = prev_yahoo
        prev_src = "yahoo"
    elif prev_db is not None:
        prev = prev_db
        prev_src = "db"
    else:
        prev = None
        prev_src = None
    name = resolve_stock_name(conn, stock_id, stock_name) or meta.get("fubon_name")
    pct_from_prev: float | None = None
    fchg = meta.get("fubon_change_percent")
    if isinstance(fchg, (int, float)) and prev_fubon is not None:
        pct_from_prev = round(float(fchg), 3)
    elif px and prev:
        pct_from_prev = round((px / prev - 1.0) * 100, 3)
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

    if ta_30m is not None:
        ta30 = dict(ta_30m)
    else:
        try:
            ta30 = get_ta_30m_anchors(stock_id, now=now, last_print=px)
        except Exception as exc:  # noqa: BLE001
            ta30 = {
                "ta_30m_ready": False,
                "ta_30m_note_zh": f"30m 暫不可用（{str(exc)[:80]}）；觀察用・非下單訊號。",
                "fade30_observe": None,
                "fade30_rule": _FADE30_RULE_ID,
                "idx_or_inside": None,
            }
    anchors: dict[str, Any] = {
        "mode": mode,
        "horizon": horizon,
        "auction_interval_min": 20 if is_disp else None,
        "prev_close": prev,
        "prev_close_db": prev_db,
        "prev_close_source": prev_src,
        "pct_from_prev": pct_from_prev,
        "mins_to_next": (
            max(0, int((nxt - now).total_seconds() // 60)) if nxt else None
        ),
        **meta,
        **ta30,
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
    fubon_session: Any | None = None,
) -> list[dict[str, Any]]:
    pairs = (
        stocks
        if stocks is not None
        else resolve_live_ta_universe(conn, fubon_session=fubon_session)
    )
    disp = disposition_ids if disposition_ids is not None else parse_disposition_ids()
    out: list[dict[str, Any]] = []

    fubon_by_sid: dict[str, tuple[float | None, dict[str, Any]]] = {}
    fubon_connect_err: str | None = None
    rest_stock: Any | None = None
    use_fubon = _quote_backend() == "fubon"
    if use_fubon and pairs:
        rest_stock, _sess, fubon_connect_err = open_fubon_rest_stock(fubon_session)
        if fubon_connect_err:
            print(f"[ops_live_ta] Fubon unavailable: {fubon_connect_err}")
            if not _yahoo_fallback_enabled():
                print("[ops_live_ta] Yahoo fallback OFF · quotes/bars may be empty this round")
        else:
            sids = [sid for sid, _ in pairs if not _yahoo_only_symbol(sid)]
            fubon_by_sid, batch_err = (
                fetch_fubon_marketdata_quotes(sids, session=fubon_session or _sess)
                if sids
                else ({}, None)
            )
            if batch_err:
                fubon_connect_err = batch_err
                print(f"[ops_live_ta] Fubon quote batch error: {batch_err}")
            # Shared 0050 5m once / poll for fade30 OR gate (TTL cache).
            try:
                get_fade30_bench_5m(
                    rest_stock=rest_stock, session=fubon_session or _sess
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[ops_live_ta] fade30 bench warn: {exc}")

    for i, (sid, name) in enumerate(pairs):
        # Prefer Fubon candle gap over Yahoo gap when on Fubon path.
        # yahoo_gap_sec<=0 disables all inter-symbol sleeps (tests / dry burst).
        if yahoo_gap_sec <= 0:
            gap = 0.0
        elif use_fubon and rest_stock is not None:
            gap = _FUBON_CANDLES_GAP_SEC
        else:
            gap = yahoo_gap_sec
        if i > 0 and gap > 0:
            time.sleep(gap)
        yahoo_only = _yahoo_only_symbol(sid)
        if use_fubon and not yahoo_only:
            fubon_quote = fubon_by_sid.get(sid)
            if fubon_quote is None:
                err = fubon_connect_err or "missing_from_batch"
                fubon_quote = (None, {"fubon_error": err})
        else:
            fubon_quote = None
        px, qmeta = fetch_last_print(
            sid,
            fubon_quote=fubon_quote,
            prefer_fubon=False if yahoo_only else use_fubon,
            rest_stock=None if yahoo_only else rest_stock,
            session=None if yahoo_only else fubon_session,
            attach_mom=True,
        )
        try:
            ta30 = get_ta_30m_anchors(
                sid,
                last_print=px,
                rest_stock=rest_stock,
                session=fubon_session,
            )
        except Exception as exc:  # noqa: BLE001
            ta30 = {
                "ta_30m_ready": False,
                "ta_30m_note_zh": f"30m 暫不可用（{str(exc)[:80]}）；觀察用・非下單訊號。",
                "fade30_observe": None,
                "fade30_rule": _FADE30_RULE_ID,
                "idx_or_inside": None,
            }
        state = build_live_ta_state(
            sid,
            stock_name=name,
            conn=conn,
            last_print=px,
            quote_meta=qmeta,
            disposition_ids=disp,
            ta_30m=ta30,
        )
        row = asdict(state)
        if not dry_run:
            upsert_live_ta(row)
        out.append(row)
    if not dry_run and out:
        try:
            n_del = delete_live_ta_except([r["stock_id"] for r in out])
            if n_del:
                print(f"[ops_live_ta] pruned {n_del} former holdings from ops.live_ta")
        except Exception as exc:  # noqa: BLE001
            print(f"[ops_live_ta] prune warn: {exc}")
    return out
