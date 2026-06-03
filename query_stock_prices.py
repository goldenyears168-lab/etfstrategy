#!/usr/bin/env python3
"""
查詢台股即時／最新收盤價，並同步至 SQLite。

daily_sync 用法（Phase 0）：
  --sync-db --sync-mode hybrid --skip-watchlist
  --benchmark-codes IX0001,IR0002 --etf-codes <5 ETFs> --history-days 90
  → daily_bars（TEJ ETF/指數；FinMind fallback）

Legacy（非 daily）：
  - WATCHLIST + latest_quotes：僅手動 --sync-db 且不帶 --skip-watchlist 時寫入
  - Yahoo / 證交所 MIS：即時價備援
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

import pandas as pd
import requests
import yfinance as yf

from stock_db import (
    DEFAULT_DB_PATH,
    connect,
    load_latest_comparison,
    upsert_daily_bars,
    upsert_latest_quotes,
)

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
TEJ_BASE_URL = "https://api.tej.com.tw/api/datatables"

Market = Literal["TW", "TWO"]


@dataclass(frozen=True)
class Stock:
    code: str
    market: Market
    name: str

    @property
    def yahoo_symbol(self) -> str:
        return f"{self.code}.{self.market}"


# 圖片清單中的標的（友達 2409 只列一次）
WATCHLIST: tuple[Stock, ...] = (
    Stock("2303", "TW", "聯電"),
    Stock("9105", "TW", "泰金寶"),
    Stock("6147", "TWO", "頎邦"),
    Stock("4545", "TW", "銘鈺"),
    Stock("8150", "TW", "南茂"),
    Stock("2481", "TW", "強茂"),
    Stock("8291", "TWO", "尚茂"),
    Stock("2327", "TW", "國巨"),
    Stock("2409", "TW", "友達"),
    Stock("3037", "TW", "欣興"),
    Stock("2330", "TW", "台積電"),
    Stock("2344", "TW", "華邦電"),
    Stock("3481", "TW", "群創"),
    Stock("2317", "TW", "鴻海"),
    Stock("6770", "TW", "力積電"),
    Stock("6116", "TW", "彩晶"),
    Stock("2454", "TW", "聯發科"),
    Stock("2313", "TW", "華通"),
    Stock("8110", "TW", "華東"),
    Stock("2337", "TW", "旺宏"),
)

TWSE_MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
TWSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

TEJ_BENCHMARKS: tuple[tuple[str, str], ...] = (
    ("IX0001", "TAIEX"),
    ("IR0002", "TW50_TR"),
)

YAHOO_BENCHMARKS: dict[str, str] = {
    "IX0001": "^TWII",
    "IR0002": "0050.TW",
}

DEFAULT_ETF_CODES: tuple[str, ...] = ("00981A",)


def parse_benchmark_codes(arg: str | None) -> tuple[str, ...]:
    """將 --benchmark-codes 轉為代碼 tuple。"""
    if not arg:
        return tuple(code for code, _ in TEJ_BENCHMARKS)
    codes = tuple(
        code.strip().upper()
        for code in arg.split(",")
        if code.strip()
    )
    if not codes:
        return tuple(code for code, _ in TEJ_BENCHMARKS)
    return codes


def fetch_yahoo_prices(
    stocks: tuple[Stock, ...],
    period: str = "5d",
) -> pd.DataFrame:
    """用 Yahoo Finance 批次下載，取最近一筆收盤價。"""
    symbols = [s.yahoo_symbol for s in stocks]
    raw = yf.download(
        symbols,
        period=period,
        group_by="ticker",
        progress=False,
        threads=True,
        auto_adjust=True,
    )

    rows: list[dict] = []
    for stock in stocks:
        sym = stock.yahoo_symbol
        try:
            if len(stocks) == 1:
                frame = raw
            else:
                frame = raw[sym]
            frame = frame.dropna(subset=["Close"])
            if frame.empty:
                raise ValueError("無行情資料")
            last = frame.iloc[-1]
            prev = frame.iloc[-2] if len(frame) >= 2 else None
            close = float(last["Close"])
            prev_close = float(prev["Close"]) if prev is not None else None
            change = close - prev_close if prev_close is not None else None
            change_pct = (
                (change / prev_close * 100)
                if change is not None and prev_close
                else None
            )
            rows.append(
                {
                    "code": stock.code,
                    "name": stock.name,
                    "market": stock.market,
                    "symbol": sym,
                    "source": "yahoo",
                    "date": last.name.strftime("%Y-%m-%d")
                    if hasattr(last.name, "strftime")
                    else str(last.name)[:10],
                    "close": close,
                    "change": round(change, 2) if change is not None else None,
                    "change_pct": round(change_pct, 2) if change_pct is not None else None,
                    "volume": int(last["Volume"]) if pd.notna(last["Volume"]) else None,
                }
            )
        except Exception as exc:  # noqa: BLE001 — 彙整後再回報
            rows.append(
                {
                    "code": stock.code,
                    "name": stock.name,
                    "market": stock.market,
                    "symbol": sym,
                    "source": "yahoo",
                    "date": None,
                    "close": None,
                    "volume": None,
                    "error": str(exc),
                }
            )

    return pd.DataFrame(rows)


def fetch_twse_mis_price(code: str) -> dict | None:
    """
    證交所 MIS 即時報價（僅上市 TW）。
    回傳欄位：close, change, volume, time
    """
    params = {
        "ex_ch": f"tse_{code}.tw",
        "json": "1",
        "delay": "0",
    }
    resp = requests.get(
        TWSE_MIS_URL,
        params=params,
        headers=TWSE_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()
    items = payload.get("msgArray") or []
    if not items:
        return None

    row = items[0]
    price = row.get("z") or row.get("y")  # 成交價；無成交則昨收
    if not price or price == "-":
        return None

    return {
        "close": float(price),
        "change": row.get("f"),
        "volume": row.get("v"),
        "time": row.get("tlong") or row.get("t"),
        "source": "twse_mis",
    }


def enrich_with_twse_fallback(df: pd.DataFrame, stocks: tuple[Stock, ...]) -> pd.DataFrame:
    """Yahoo 失敗的上市股，改試 TWSE MIS。"""
    stock_by_code = {s.code: s for s in stocks}
    out = df.copy()

    for idx, row in out.iterrows():
        if pd.notna(row.get("close")):
            continue
        stock = stock_by_code.get(row["code"])
        if not stock or stock.market != "TW":
            continue
        try:
            quote = fetch_twse_mis_price(stock.code)
            if quote:
                out.at[idx, "close"] = quote["close"]
                out.at[idx, "source"] = quote["source"]
                out.at[idx, "error"] = None
        except Exception as exc:  # noqa: BLE001
            out.at[idx, "error"] = str(exc)

    return out


def finmind_headers() -> dict[str, str]:
    token = os.environ.get("FINMIND_TOKEN", "").strip()
    if token.startswith("eyJ") and len(token) > 100:
        return {"Authorization": f"Bearer {token}"}
    return {}


def tej_api_key() -> str:
    return os.environ.get("TEJ_API_KEY", "").strip()


def tej_get_json(path: str, params: dict) -> dict:
    """TEJ API via curl (--compressed). Avoids Python 3.14 SSL issues on api.tej.com.tw."""
    api_key = tej_api_key()
    if not api_key:
        raise RuntimeError("未設定 TEJ_API_KEY")
    query = urllib.parse.urlencode({**params, "api_key": api_key})
    url = f"{TEJ_BASE_URL}{path}?{query}"
    proc = subprocess.run(
        ["curl", "--compressed", "-sfS", "-m", "30", url],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()[:300]
        raise RuntimeError(f"TEJ HTTP 失敗: {detail}")
    return json.loads(proc.stdout)


def fetch_tej_index_bars(
    index_codes: tuple[str, ...],
    start: date,
    end: date,
) -> list[dict]:
    """用 TEJ EWIPRCD 抓指數日資料（只取 close + volume）。"""
    payload = tej_get_json(
        "/TWN/EWIPRCD.json",
        {
            "idx_id": ",".join(index_codes),
            "mdate.gte": start.isoformat(),
            "mdate.lte": end.isoformat(),
            "opts.sort": "mdate.asc",
        },
    )
    if payload.get("error"):
        err = payload["error"]
        raise RuntimeError(f"TEJ {err.get('code')}: {err.get('message')}")

    datatable = payload.get("datatable") or {}
    cols = [c.get("name") for c in datatable.get("columns") or []]
    rows = datatable.get("data") or []
    if not cols:
        return []

    close_candidates = ("close_d", "close", "ind_close", "price")
    vol_candidates = ("volume", "trading_volume", "amount", "turnover")
    close_col = next((c for c in close_candidates if c in cols), None)
    if close_col is None:
        # EWIPRCD 至少會有收盤欄位；若欄位名稱異動，明確報錯讓使用者知道。
        raise RuntimeError("TEJ EWIPRCD 找不到收盤欄位")
    volume_col = next((c for c in vol_candidates if c in cols), None)

    parsed: list[dict] = []
    for row in rows:
        item = dict(zip(cols, row))
        code = str(item.get("idx_id") or item.get("coid", "")).strip()
        mdate = str(item.get("mdate", ""))[:10]
        close_raw = item.get(close_col)
        if not code or not mdate or close_raw is None:
            continue
        volume_raw = item.get(volume_col) if volume_col else None
        parsed.append(
            {
                "code": code,
                "date": mdate,
                "open": None,
                "high": None,
                "low": None,
                "close": float(close_raw),
                "volume": int(volume_raw) if volume_raw not in (None, "") else None,
                "spread": None,
                "source": "tej",
            }
        )
    return parsed


def fetch_tej_etf_bars(
    etf_code: str,
    start: date,
    end: date,
) -> list[dict]:
    """用 TEJ EWPRCD 抓 ETF 日 OHLCV。"""
    payload = tej_get_json(
        "/TWN/EWPRCD.json",
        {
            "coid": etf_code,
            "mdate.gte": start.isoformat(),
            "mdate.lte": end.isoformat(),
            "opts.sort": "mdate.asc",
        },
    )
    if payload.get("error"):
        err = payload["error"]
        raise RuntimeError(f"TEJ {err.get('code')}: {err.get('message')}")

    datatable = payload.get("datatable") or {}
    cols = [c.get("name") for c in datatable.get("columns") or []]
    rows = datatable.get("data") or []
    if not cols:
        return []

    parsed: list[dict] = []
    for row in rows:
        item = dict(zip(cols, row))
        code = str(item.get("coid", "")).strip()
        mdate = str(item.get("mdate", ""))[:10]
        close_raw = item.get("close_adj") if item.get("close_adj") is not None else item.get("close_d")
        if not code or not mdate or close_raw is None:
            continue
        volume_raw = item.get("volume")
        volume = None
        if volume_raw not in (None, ""):
            volume = int(float(volume_raw) * 1000)
        parsed.append(
            {
                "code": code,
                "date": mdate,
                "open": float(item["open_adj"]) if item.get("open_adj") is not None else None,
                "high": float(item["high_adj"]) if item.get("high_adj") is not None else None,
                "low": float(item["low_adj"]) if item.get("low_adj") is not None else None,
                "close": float(close_raw),
                "volume": volume,
                "spread": None,
                "source": "tej",
            }
        )
    return parsed


def sync_etf_daily_bars(
    etf_codes: tuple[str, ...],
    db_path: Path,
    history_days: int,
) -> int:
    """同步 ETF 日線至 daily_bars（TEJ EWPRCD 優先，失敗則 FinMind）。"""
    if not etf_codes:
        return 0
    end = date.today()
    start = end - timedelta(days=history_days)
    print(f"  ETF 日線區間：{start} ～ {end}（{history_days} 日）")
    conn = connect(db_path)
    total = 0
    try:
        for code in etf_codes:
            try:
                bars = fetch_tej_etf_bars(code, start, end)
                if not bars:
                    raise RuntimeError("TEJ 無可用資料")
                latest = max(b["date"] for b in bars)
                count = upsert_daily_bars(conn, bars)
                print(
                    f"  TEJ ETF 日線 {code}：upsert {count} 筆，"
                    f"API 最新交易日 {latest}（重複按會覆寫同日資料）"
                )
                total += count
            except Exception as exc:  # noqa: BLE001
                print(f"  TEJ ETF 日線失敗 {code}，改用 FinMind：{exc}")
                bars = fetch_finmind_daily(code, start, end)
                latest = max(b["date"] for b in bars) if bars else "—"
                count = upsert_daily_bars(conn, bars)
                print(
                    f"  FinMind ETF 日線 {code}：upsert {count} 筆，"
                    f"API 最新交易日 {latest}"
                )
                total += count
    finally:
        conn.close()
    return total


def parse_etf_codes(arg: str | None) -> tuple[str, ...]:
    if not arg:
        return ()
    return tuple(code.strip().upper() for code in arg.split(",") if code.strip())


def fetch_yahoo_index_bars(
    mappings: dict[str, str],
    start: date,
    end: date,
) -> list[dict]:
    """TEJ 失敗時，用 Yahoo 補指數 close。"""
    rows: list[dict] = []
    for code, symbol in mappings.items():
        frame = yf.download(
            symbol,
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            progress=False,
            auto_adjust=True,
            threads=False,
        )
        if frame.empty:
            continue
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = frame.columns.droplevel(1)
        for idx, bar in frame.iterrows():
            close_val = bar["Close"]
            if isinstance(close_val, pd.Series):
                close_val = close_val.iloc[0]
            if pd.isna(close_val):
                continue
            open_val = bar["Open"] if pd.notna(bar.get("Open")) else None
            high_val = bar["High"] if pd.notna(bar.get("High")) else None
            low_val = bar["Low"] if pd.notna(bar.get("Low")) else None
            vol_val = bar["Volume"] if pd.notna(bar.get("Volume")) else None
            if isinstance(open_val, pd.Series):
                open_val = open_val.iloc[0] if pd.notna(open_val.iloc[0]) else None
            if isinstance(high_val, pd.Series):
                high_val = high_val.iloc[0] if pd.notna(high_val.iloc[0]) else None
            if isinstance(low_val, pd.Series):
                low_val = low_val.iloc[0] if pd.notna(low_val.iloc[0]) else None
            if isinstance(vol_val, pd.Series):
                vol_val = vol_val.iloc[0] if pd.notna(vol_val.iloc[0]) else None
            rows.append(
                {
                    "code": code,
                    "date": idx.strftime("%Y-%m-%d"),
                    "open": float(open_val) if open_val is not None else None,
                    "high": float(high_val) if high_val is not None else None,
                    "low": float(low_val) if low_val is not None else None,
                    "close": float(close_val),
                    "volume": int(vol_val) if vol_val is not None else None,
                    "spread": None,
                    "source": "yahoo",
                }
            )
    return rows


def sync_tej_benchmarks(
    db_path: Path,
    history_days: int,
    benchmark_codes: tuple[str, ...],
) -> int:
    """混合模式：同步 TEJ 指數基準，若 TEJ 失敗則退回 Yahoo。"""
    end = date.today()
    start = end - timedelta(days=history_days)
    codes = benchmark_codes
    if not codes:
        return 0
    conn = connect(db_path)
    try:
        try:
            bars = fetch_tej_index_bars(codes, start, end)
            if not bars:
                raise RuntimeError("TEJ 無可用資料")
            count = upsert_daily_bars(conn, bars)
            latest = max(b["date"] for b in bars) if bars else "—"
            print(
                f"  TEJ 指數基準同步完成：upsert {count} 筆，"
                f"API 最新交易日 {latest}（重複按會覆寫同日資料）"
            )
            return count
        except Exception as exc:  # noqa: BLE001
            print(f"  TEJ 指數同步失敗，改用 Yahoo 備援：{exc}")
            yahoo_map = {code: YAHOO_BENCHMARKS[code] for code in codes if code in YAHOO_BENCHMARKS}
            missing = [code for code in codes if code not in yahoo_map]
            if missing:
                print(f"  以下指數代碼無 Yahoo 備援，將略過：{', '.join(missing)}")
            yahoo_rows = fetch_yahoo_index_bars(yahoo_map, start, end)
            if not yahoo_rows:
                return 0
            count = upsert_daily_bars(conn, yahoo_rows)
            print(f"  Yahoo 指數基準同步完成：{count} 筆")
            return count
    finally:
        conn.close()


def fetch_finmind_daily(
    code: str,
    start: date,
    end: date,
) -> list[dict]:
    resp = requests.get(
        FINMIND_URL,
        params={
            "dataset": "TaiwanStockPrice",
            "data_id": code,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
        headers=finmind_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != 200:
        raise RuntimeError(payload.get("msg", "FinMind error"))

    rows: list[dict] = []
    for item in payload.get("data") or []:
        close = float(item["close"])
        spread = item.get("spread")
        spread_f = float(spread) if spread is not None and spread != "" else None
        rows.append(
            {
                "code": code,
                "date": str(item["date"])[:10],
                "open": float(item["open"]) if item.get("open") is not None else None,
                "high": float(item["max"]) if item.get("max") is not None else None,
                "low": float(item["min"]) if item.get("min") is not None else None,
                "close": close,
                "volume": int(item.get("Trading_Volume") or 0),
                "spread": spread_f,
                "source": "finmind",
            }
        )
    return rows


def finmind_latest_row(stock: Stock, start: date, end: date) -> dict | None:
    bars = fetch_finmind_daily(stock.code, start, end)
    if not bars:
        return None
    last = max(bars, key=lambda r: r["date"])
    spread = last.get("spread") or 0.0
    close = last["close"]
    base = close - spread
    change_pct = (spread / base * 100) if base else None
    return {
        "code": stock.code,
        "name": stock.name,
        "market": stock.market,
        "date": last["date"],
        "close": close,
        "change": spread,
        "change_pct": round(change_pct, 2) if change_pct is not None else None,
        "volume": last.get("volume"),
        "source": "finmind",
        "queried_at": datetime.now().isoformat(timespec="seconds"),
    }


def sync_to_db(
    stocks: tuple[Stock, ...],
    db_path: Path,
    history_days: int,
    mode: str = "legacy",
    benchmark_codes: tuple[str, ...] | None = None,
) -> tuple[int, int]:
    end = date.today()
    start = end - timedelta(days=history_days)
    conn = connect(db_path)

    bar_count = 0
    for stock in stocks:
        try:
            bars = fetch_finmind_daily(stock.code, start, end)
            bar_count += upsert_daily_bars(conn, bars)
        except Exception as exc:  # noqa: BLE001
            print(f"  FinMind 同步失敗 {stock.code} {stock.name}: {exc}")

    latest_rows: list[dict] = []
    if stocks:
        yahoo_df = fetch_yahoo_prices(stocks)
        queried_at = datetime.now().isoformat(timespec="seconds")
        for _, row in yahoo_df.iterrows():
            if pd.isna(row.get("close")):
                continue
            latest_rows.append(
                {
                    "code": row["code"],
                    "name": row["name"],
                    "market": row["market"],
                    "date": row["date"],
                    "close": float(row["close"]),
                    "change": row.get("change"),
                    "change_pct": row.get("change_pct"),
                    "volume": row.get("volume"),
                    "source": "yahoo",
                    "queried_at": queried_at,
                }
            )

        for stock in stocks:
            try:
                fm = finmind_latest_row(stock, start, end)
                if fm:
                    latest_rows.append(fm)
            except Exception as exc:  # noqa: BLE001
                print(f"  FinMind 最新價失敗 {stock.code}: {exc}")

    quote_count = upsert_latest_quotes(conn, latest_rows) if latest_rows else 0
    conn.close()

    if mode == "hybrid":
        sync_tej_benchmarks(
            db_path,
            history_days,
            benchmark_codes or tuple(code for code, _ in TEJ_BENCHMARKS),
        )

    return bar_count, quote_count


def print_compare_table(conn_path: Path, stocks: tuple[Stock, ...]) -> None:
    conn = connect(conn_path)
    codes = [s.code for s in stocks]
    rows = load_latest_comparison(conn, codes)
    conn.close()

    by_code: dict[str, dict[str, object]] = {}
    for row in rows:
        by_code.setdefault(row["code"], {})[row["source"]] = row

    print(f"\n{'代號':<6} {'名稱':<8} {'Yahoo日期':<12} {'Yahoo收盤':>10} {'FM日期':<12} {'FM收盤':>10} {'價差':>8}")
    print("-" * 72)
    for stock in stocks:
        src = by_code.get(stock.code, {})
        y = src.get("yahoo")
        f = src.get("finmind")
        y_close = f"{y['close']:>10.2f}" if y and y["close"] is not None else "—"
        f_close = f"{f['close']:>10.2f}" if f and f["close"] is not None else "—"
        y_date = (y["date"] if y else "—") or "—"
        f_date = (f["date"] if f else "—") or "—"
        diff = "—"
        if y and f and y["close"] is not None and f["close"] is not None:
            diff = f"{float(y['close']) - float(f['close']):+.2f}"
        print(
            f"{stock.code:<6} {stock.name:<8} {y_date:<12} {y_close:>10} "
            f"{f_date:<12} {f_close:>10} {diff:>8}"
        )


def print_table(df: pd.DataFrame) -> None:
    display = df.copy()
    if "error" in display.columns:
        display = display.drop(columns=["error"], errors="ignore")

    display["close"] = display["close"].map(
        lambda x: f"{x:,.2f}" if pd.notna(x) else "—"
    )
    cols = ["code", "name", "market", "close", "date", "source"]
    cols = [c for c in cols if c in display.columns]
    print(display[cols].to_string(index=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="查詢台股標的最新股價")
    parser.add_argument(
        "--source",
        choices=("yahoo", "yahoo+twse"),
        default="yahoo",
        help="yahoo：僅 Yahoo；yahoo+twse：Yahoo 失敗時補證交所 MIS",
    )
    parser.add_argument(
        "--period",
        default="5d",
        help="Yahoo 歷史區間，例如 1d、5d、1mo",
    )
    parser.add_argument(
        "--sync-db",
        action="store_true",
        help="同步至 SQLite：FinMind 日線 + Yahoo/FinMind 最新價",
    )
    parser.add_argument(
        "--sync-mode",
        choices=("legacy", "hybrid"),
        default="hybrid",
        help="同步模式：legacy（舊模式）或 hybrid（加 TEJ 指數，失敗則 Yahoo）",
    )
    parser.add_argument(
        "--benchmark-codes",
        default=",".join(code for code, _ in TEJ_BENCHMARKS),
        help="hybrid 模式使用的 TEJ 指數代碼，逗號分隔（例如 IX0001,IR0002）",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"SQLite 路徑（預設 {DEFAULT_DB_PATH}）",
    )
    parser.add_argument(
        "--history-days",
        type=int,
        default=90,
        help="FinMind 同步日線天數（預設 90）",
    )
    parser.add_argument(
        "--etf-codes",
        default="",
        help="額外同步 ETF 日線至 daily_bars（TEJ EWPRCD，失敗則 FinMind），逗號分隔",
    )
    parser.add_argument(
        "--skip-watchlist",
        action="store_true",
        help="daily_sync 用：略過 WATCHLIST 日線與 latest_quotes，只跑指數基準（hybrid）",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="顯示 Yahoo vs FinMind 最新價對照（需先 --sync-db 或已有資料）",
    )
    parser.add_argument(
        "--csv",
        metavar="PATH",
        help="將結果存成 CSV",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 輸出",
    )
    args = parser.parse_args()

    if args.sync_db:
        print(
            f"同步至 {args.db}（{args.sync_mode}，FinMind 最近 {args.history_days} 日）…"
        )
        benchmark_codes = parse_benchmark_codes(args.benchmark_codes)
        if args.sync_mode == "hybrid":
            print(f"  指數基準代碼：{', '.join(benchmark_codes)}")
        stocks = () if args.skip_watchlist else WATCHLIST
        if args.skip_watchlist:
            print("  略過 WATCHLIST（僅同步指數基準 + --etf-codes）")
        bars, quotes = sync_to_db(
            stocks,
            args.db,
            args.history_days,
            mode=args.sync_mode,
            benchmark_codes=benchmark_codes,
        )
        etf_codes = parse_etf_codes(args.etf_codes)
        etf_bars = sync_etf_daily_bars(etf_codes, args.db, args.history_days) if etf_codes else 0
        if args.skip_watchlist:
            print(f"完成：ETF 日線 {etf_bars} 筆（指數基準見上方 TEJ/Yahoo 訊息）")
        else:
            print(f"完成：日線 {bars} 筆、ETF 日線 {etf_bars} 筆、最新價 {quotes} 筆")
        if not args.compare and not args.csv and not args.json:
            return

    if args.compare:
        print_compare_table(args.db, WATCHLIST)

    if args.compare and not args.sync_db and not args.csv and not args.json:
        return

    if args.sync_db and args.compare and not args.csv and not args.json:
        return

    df = fetch_yahoo_prices(WATCHLIST, period=args.period)
    if args.source == "yahoo+twse":
        df = enrich_with_twse_fallback(df, WATCHLIST)

    df["queried_at"] = datetime.now().isoformat(timespec="seconds")

    if args.json:
        print(json.dumps(df.to_dict(orient="records"), ensure_ascii=False, indent=2))
    else:
        print(f"\n查詢時間：{df['queried_at'].iloc[0]}")
        print_table(df)

    failed = df[df["close"].isna()]
    if not failed.empty:
        print("\n以下標的未取得股價：")
        for _, row in failed.iterrows():
            print(f"  {row['code']} {row['name']}: {row.get('error', '未知錯誤')}")

    if args.csv:
        df.to_csv(args.csv, index=False, encoding="utf-8-sig")
        print(f"\n已儲存：{args.csv}")


if __name__ == "__main__":
    main()
