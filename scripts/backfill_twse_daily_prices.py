#!/usr/bin/env python3
"""TWSE 每日收盤行情（MI_INDEX / ALLBUT0999）→ stock_daily_bars 全市場價格回補。

**動機**：``stock_daily_bars`` 在 2024-05 以前每日只有約 100~160 檔，而且實測
2015 年的 156 檔全部活到 2026（零下市）——那是今日 ETF 成分股回補出來的
存活者偏誤宇宙。拿它做放空／借券研究會系統性刪掉「被放空到下市」的那一尾。
本腳本改用 TWSE 官方每日收盤行情，逐日抓當天實際掛牌的全部證券，含後來
下市的標的。

**寫入策略**：``stock_daily_bars`` 主鍵含 ``source``，本腳本一律寫
``source='twse_mi_index'``，不覆蓋既有 finmind 列。下游要自己決定優先序
（finmind 有 ``adj_close``，本來源沒有）。

**已知限制**：MI_INDEX 只給原始收盤價，**沒有除權息還原**。月頻報酬若直接
用原始價會在除息月低估高殖利率股報酬——下游必須另外處理（TWSE 除權除息
計算結果表 TWT49U），本腳本不負責。

用法::

    PYTHONPATH=src .venv/bin/python scripts/backfill_twse_daily_prices.py \
        --start 2011-01-01 --end 2024-05-31
"""
from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from stock_db import connect
from stock_db.util import DEFAULT_DB_PATH, utc_now_iso

ENDPOINT = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
SOURCE = "twse_mi_index"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


def _num(s) -> float | None:
    if s is None:
        return None
    t = str(s).replace(",", "").strip()
    if not t or t in {"-", "--", "---"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def fetch_day(day: date, retries: int = 6) -> list[dict]:
    """回傳當日全市場列；非交易日回空 list。"""
    url = f"{ENDPOINT}?date={day:%Y%m%d}&type=ALLBUT0999&response=json"
    payload = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.load(resp)
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                http.client.IncompleteRead, http.client.HTTPException,
                ConnectionError, OSError) as exc:
            if attempt == retries - 1:
                raise
            time.sleep(6 * (attempt + 1))
            print(f"    retry {attempt + 1} {day} ({exc})", flush=True)
    if not payload or payload.get("stat") != "OK":
        return []

    # 每日收盤行情那張表的欄位含「收盤價」，其餘（價格指數／大盤統計）沒有。
    table = None
    for t in payload.get("tables") or []:
        if any("收盤價" in str(f) for f in (t.get("fields") or [])):
            table = t
            break
    if table is None:
        return []

    fields = [str(f).strip() for f in table["fields"]]
    idx = {name: i for i, name in enumerate(fields)}
    need = ("證券代號", "成交股數", "成交金額", "開盤價", "最高價", "最低價", "收盤價")
    if any(k not in idx for k in need):
        return []

    iso = day.isoformat()
    out = []
    for item in table.get("data") or []:
        close = _num(item[idx["收盤價"]])
        if close is None or close <= 0:
            continue  # 全日無成交／暫停交易
        out.append(
            {
                "stock_id": str(item[idx["證券代號"]]).strip(),
                "trade_date": iso,
                "open": _num(item[idx["開盤價"]]),
                "high": _num(item[idx["最高價"]]),
                "low": _num(item[idx["最低價"]]),
                "close": close,
                "volume": _num(item[idx["成交股數"]]),
                "amount": _num(item[idx["成交金額"]]),
                "adj_close": None,
                "source": SOURCE,
            }
        )
    return out


def upsert(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_daily_bars (
            stock_id, trade_date, open, high, low, close, volume, amount,
            adj_close, source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :open, :high, :low, :close, :volume, :amount,
            :adj_close, :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, source) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume, amount=excluded.amount,
            synced_at=excluded.synced_at
    """
    conn.executemany(sql, [{**r, "synced_at": synced_at} for r in rows])
    conn.commit()
    return len(rows)


def already_done(conn, start: date, end: date) -> set[str]:
    cur = conn.execute(
        "SELECT DISTINCT trade_date FROM stock_daily_bars "
        "WHERE source=? AND trade_date BETWEEN ? AND ?",
        (SOURCE, start.isoformat(), end.isoformat()),
    )
    return {r[0] for r in cur}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--sleep", type=float, default=2.5)
    ap.add_argument("--no-resume", action="store_true", help="不跳過已抓過的日期")
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    conn = connect(args.db)
    try:
        done = set() if args.no_resume else already_done(conn, start, end)
        if done:
            print(f"resume: 已有 {len(done)} 個交易日，將跳過", flush=True)

        total = days = skipped = holidays = 0
        day = start
        t0 = time.time()
        while day <= end:
            if day.weekday() >= 5:
                day += timedelta(days=1)
                continue
            if day.isoformat() in done:
                skipped += 1
                day += timedelta(days=1)
                continue
            rows = fetch_day(day)
            if rows:
                total += upsert(conn, rows)
                days += 1
                if days % 25 == 0:
                    el = time.time() - t0
                    print(
                        f"  {day}  累計 {days} 交易日 / {total:,} 列  "
                        f"({el / 60:.1f} 分)",
                        flush=True,
                    )
            else:
                holidays += 1
            time.sleep(args.sleep)
            day += timedelta(days=1)
        print(
            f"\n完成：{days} 交易日 / {total:,} 列寫入 · "
            f"跳過 {skipped} 已有 · {holidays} 個非交易日"
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
