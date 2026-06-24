#!/usr/bin/env python3
"""
同步 ETF / 指數日線至 SQLite（TEJ 優先，FinMind / Yahoo 備援）。

daily_sync 用法：
  --sync-db --sync-mode hybrid
  --benchmark-codes IX0001,IR0002 --etf-codes <4 ETFs> --history-days 90
  → daily_bars
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import subprocess
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

from finmind_client import fetch_finmind
from project_config import parse_etf_codes
from stock_db import DEFAULT_DB_PATH, connect, upsert_daily_bars
TEJ_BASE_URL = "https://api.tej.com.tw/api/datatables"

TEJ_BENCHMARKS: tuple[tuple[str, str], ...] = (
    ("IX0001", "TAIEX"),
    ("IR0002", "TW50_TR"),
)

YAHOO_BENCHMARKS: dict[str, str] = {
    "IX0001": "^TWII",
    "IR0002": "0050.TW",
}


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


def _sync_one_etf_daily_bars(
    conn,
    code: str,
    start: date,
    end: date,
    *,
    quiet: bool,
) -> int:
    """TEJ EWPRCD 優先；失敗或空資料時 fallback FinMind TaiwanStockPrice。"""
    source = "tej"
    try:
        bars = fetch_tej_etf_bars(code, start, end)
        if not bars:
            raise RuntimeError("TEJ 無可用資料")
    except Exception as tej_exc:  # noqa: BLE001
        try:
            bars = fetch_finmind_daily(code, start, end)
        except Exception as fm_exc:  # noqa: BLE001
            raise RuntimeError(f"TEJ: {tej_exc}; FinMind: {fm_exc}") from tej_exc
        if not bars:
            raise RuntimeError(f"TEJ: {tej_exc}; FinMind 無可用資料") from tej_exc
        source = "finmind"
        if not quiet:
            print(
                f"  TEJ ETF 日線 {code} 失敗，改用 FinMind：{tej_exc}",
                file=sys.stderr,
            )

    latest = max(b["date"] for b in bars)
    count = upsert_daily_bars(conn, bars)
    if not quiet:
        label = "TEJ" if source == "tej" else "FinMind"
        print(
            f"  {label} ETF 日線 {code}：upsert {count} 筆，"
            f"API 最新交易日 {latest}（重複按會覆寫同日資料）"
        )
    return count


def sync_etf_daily_bars(
    etf_codes: tuple[str, ...],
    db_path: Path,
    history_days: int,
    *,
    quiet: bool = False,
) -> int:
    """同步 ETF 日線至 daily_bars（TEJ EWPRCD；失敗 fallback FinMind）。"""
    if not etf_codes:
        return 0
    end = date.today()
    start = end - timedelta(days=history_days)
    if not quiet:
        print(f"  ETF 日線區間：{start} ～ {end}（{history_days} 日）")
    conn = connect(db_path)
    total = 0
    try:
        for code in etf_codes:
            try:
                total += _sync_one_etf_daily_bars(
                    conn, code, start, end, quiet=quiet
                )
            except Exception as exc:  # noqa: BLE001
                print(
                    f"  WARN ETF 日線略過 {code}：{exc}",
                    file=sys.stderr,
                )
                continue
    finally:
        conn.close()
    return total


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
                    "adj_close": float(bar["Adj Close"]) if pd.notna(bar.get("Adj Close")) else float(close_val),
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
    *,
    quiet: bool = False,
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
            if not quiet:
                print(
                    f"  TEJ 指數基準同步完成：upsert {count} 筆，"
                    f"API 最新交易日 {latest}（重複按會覆寫同日資料）"
                )
            return count
        except Exception as exc:  # noqa: BLE001
            print(f"  TEJ 指數同步失敗，改用 Yahoo 備援：{exc}", file=sys.stderr)
            yahoo_map = {code: YAHOO_BENCHMARKS[code] for code in codes if code in YAHOO_BENCHMARKS}
            missing = [code for code in codes if code not in yahoo_map]
            if missing and not quiet:
                print(f"  以下指數代碼無 Yahoo 備援，將略過：{', '.join(missing)}")
            elif missing:
                print(
                    f"  以下指數代碼無 Yahoo 備援，將略過：{', '.join(missing)}",
                    file=sys.stderr,
                )
            yahoo_rows = fetch_yahoo_index_bars(yahoo_map, start, end)
            if not yahoo_rows:
                return 0
            count = upsert_daily_bars(conn, yahoo_rows)
            if not quiet:
                print(f"  Yahoo 指數基準同步完成：{count} 筆")
            return count
    finally:
        conn.close()


def fetch_finmind_daily(
    code: str,
    start: date,
    end: date,
) -> list[dict]:
    raw = fetch_finmind("TaiwanStockPrice", code, start, end, timeout=30)
    rows: list[dict] = []
    for item in raw:
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


def main() -> None:
    parser = argparse.ArgumentParser(description="同步 ETF / 指數日線至 SQLite")
    parser.add_argument(
        "--sync-db",
        action="store_true",
        help="同步至 SQLite（TEJ 指數 + --etf-codes 日線）",
    )
    parser.add_argument(
        "--sync-mode",
        choices=("hybrid",),
        default="hybrid",
        help="同步模式（hybrid：TEJ 指數，失敗則 Yahoo）",
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
        help="額外同步 ETF 日線至 daily_bars（TEJ EWPRCD，失敗略過），逗號分隔",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="僅印一行摘要（WARN/失敗仍走 stderr）",
    )
    args = parser.parse_args()

    if not args.sync_db:
        parser.error("請加上 --sync-db（本腳本僅用於日線同步）")

    quiet = args.quiet
    benchmark_codes = parse_benchmark_codes(args.benchmark_codes)
    etf_codes = parse_etf_codes(args.etf_codes)
    if not quiet:
        print(f"同步至 {args.db}（{args.sync_mode}，最近 {args.history_days} 日）…")
        print(f"  指數基準代碼：{', '.join(benchmark_codes)}")
    bench_count = sync_tej_benchmarks(
        args.db, args.history_days, benchmark_codes, quiet=quiet
    )
    etf_bars = (
        sync_etf_daily_bars(etf_codes, args.db, args.history_days, quiet=quiet)
        if etf_codes
        else 0
    )
    if quiet:
        print(
            f"  日線：指數 {bench_count} 筆，ETF {etf_bars} 筆"
            f"（{', '.join(benchmark_codes)} + {len(etf_codes)} 檔）"
        )
    else:
        print(f"完成：ETF 日線 {etf_bars} 筆（指數基準見上方 TEJ/Yahoo 訊息）")


if __name__ == "__main__":
    main()
