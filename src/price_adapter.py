"""執行評估／盤中監控共用報價 adapter（FinMind tick snapshot + Yahoo / manual fallback）。"""

from __future__ import annotations

import contextlib
import logging
import os

from finmind_client import fetch_tick_snapshots, finmind_token

DEFAULT_BATCH_SIZE = 40
MARKET_YAHOO_SUFFIX = {"TSE": "TW", "OTC": "TWO"}
YAHOO_SUFFIX_FALLBACK = ("TW", "TWO")


def fetch_finmind_tick_rows(
    stock_ids: list[str],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> tuple[list[dict], str | None]:
    """批次拉 FinMind taiwan_stock_tick_snapshot（與 intraday_monitor 共用）。"""
    if not stock_ids:
        return [], None
    rows: list[dict] = []
    for i in range(0, len(stock_ids), batch_size):
        chunk = stock_ids[i : i + batch_size]
        chunk_rows, err = fetch_tick_snapshots(chunk)
        if err:
            return rows, err
        rows.extend(chunk_rows)
    return rows, None


def yahoo_fallback_enabled() -> bool:
    raw = os.environ.get("EXECUTION_EVAL_YAHOO_FALLBACK", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def yahoo_suffix_order(stock_id: str, market_map: dict[str, str] | None) -> tuple[str, ...]:
    """依 DB 上市櫃別決定 Yahoo ticker 後綴順序，避免上櫃股先打 .TW 噴 404。"""
    market = (market_map or {}).get(stock_id, "").upper()
    primary = MARKET_YAHOO_SUFFIX.get(market)
    if primary == "TWO":
        return ("TWO", "TW")
    if primary == "TW":
        return ("TW", "TWO")
    return YAHOO_SUFFIX_FALLBACK


@contextlib.contextmanager
def _quiet_yfinance():
    """抑制 yfinance 對預期失敗 ticker 的 stderr 噪音。"""
    ylog = logging.getLogger("yfinance")
    prev_level = ylog.level
    ylog.setLevel(logging.CRITICAL)
    try:
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            with contextlib.redirect_stderr(devnull):
                yield
    finally:
        ylog.setLevel(prev_level)


def fetch_yahoo_last_price(
    stock_id: str,
    *,
    market_map: dict[str, str] | None = None,
) -> float | None:
    """單檔 1 分 K 最新價；上櫃股優先 .TWO。"""
    import yfinance as yf

    for suffix in yahoo_suffix_order(stock_id, market_map):
        with _quiet_yfinance():
            try:
                df = yf.Ticker(f"{stock_id}.{suffix}").history(
                    period="1d", interval="1m", auto_adjust=False
                )
            except Exception:
                continue
        if df is None or df.empty:
            continue
        close = float(df["Close"].iloc[-1])
        if close > 0:
            return close
    return None


def fetch_yahoo_last_prices(
    stock_ids: list[str],
    *,
    market_map: dict[str, str] | None = None,
) -> dict[str, float]:
    """盤中 1 分 K 最新價（延遲可能 15 分鐘）。"""
    out: dict[str, float] = {}
    for sid in stock_ids:
        close = fetch_yahoo_last_price(sid, market_map=market_map)
        if close is not None:
            out[sid] = close
    return out


def prices_from_tick_rows(rows: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows:
        sid = str(row.get("stock_id", "")).strip()
        close = float(row.get("close") or 0)
        if sid and close > 0:
            out[sid] = close
    return out


def resolve_snapshot_prices(
    stock_ids: list[str],
    *,
    manual: dict[str, float] | None = None,
    source: str = "manual",
    market_map: dict[str, str] | None = None,
) -> tuple[dict[str, float], str | None, list[str]]:
    """
    合併 FinMind tick 與手動價；manual 覆寫同代號 FinMind 價。

    回傳 (prices, source_label, notices)。
    """
    manual_map = dict(manual or {})
    notices: list[str] = []
    finmind_prices: dict[str, float] = {}

    use_finmind = source == "finmind"
    if source == "auto":
        use_finmind = bool(finmind_token())

    if use_finmind:
        need = [sid for sid in stock_ids if sid not in manual_map]
        if need:
            rows, err = fetch_finmind_tick_rows(need)
            if err:
                notices.append(f"FinMind: {err}")
            finmind_prices = prices_from_tick_rows(rows)

    merged = {**finmind_prices, **manual_map}
    yahoo_prices: dict[str, float] = {}
    missing = [sid for sid in stock_ids if sid not in merged]
    use_yahoo = source == "yahoo" or (
        source == "auto" and yahoo_fallback_enabled() and bool(missing)
    )
    if use_yahoo and missing:
        yahoo_prices = fetch_yahoo_last_prices(missing, market_map=market_map)
        if yahoo_prices:
            merged = {**merged, **yahoo_prices}
            parts = ", ".join(f"{sid}={merged[sid]:g}" for sid in stock_ids if sid in merged)
            if source == "yahoo":
                notices.append(f"Yahoo 1m 報價：{parts}（延遲可能 15 分鐘）")
            else:
                notices.append(
                    f"Yahoo 1m 備援：{parts}（FinMind tick 不可用或非 Sponsor）"
                )

    missing = [sid for sid in stock_ids if sid not in merged]
    if missing:
        notices.append(f"缺少報價：{','.join(missing)}")

    if finmind_prices and yahoo_prices:
        label = "finmind_tick+yahoo_1m"
    elif finmind_prices:
        label = "finmind_tick"
    elif yahoo_prices:
        label = "yahoo_1m"
    else:
        label = None
    return merged, label, notices


def is_price_notice_error(notice: str) -> bool:
    return notice.startswith("缺少報價") or notice.startswith("請提供")
