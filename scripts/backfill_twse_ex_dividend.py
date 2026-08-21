#!/usr/bin/env python3
"""TWSE 除權除息計算結果表（TWT49U）→ stock_ex_adjust_event 還原因子回補。

上市股的除權息還原因子來源。上櫃股不走這裡——櫃買日行情自帶「次日參考價」，
由 ``backfill_tpex_daily_prices.py`` 順手記錄（``anchor_kind='cum'``）。

因子定義：``factor = 除權息參考價 / 除權息前收盤價``。除權息日當天的總報酬
應為 ``close(ex) / 除權息參考價 - 1``，直接用原始價算會低估掉整個股利。

單次查詢可涵蓋一整年，故全期只需十餘次請求。
"""
from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from stock_db import connect
from stock_db.util import DEFAULT_DB_PATH, utc_now_iso

ENDPOINT = "https://www.twse.com.tw/rwd/zh/exRight/TWT49U"
SOURCE = "twse_twt49u"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


def roc_to_iso(s: str) -> str | None:
    try:
        y, rest = str(s).split("年", 1)
        m, rest = rest.split("月", 1)
        return f"{int(y) + 1911:04d}-{int(m):02d}-{int(rest.rstrip('日')):02d}"
    except (ValueError, IndexError):
        return None


def _num(s) -> float | None:
    t = str(s or "").replace(",", "").strip()
    if not t or t in {"-", "--", "N/A"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def fetch_year(year: int, retries: int = 4) -> list[list]:
    end = min(f"{year}1231", date.today().strftime("%Y%m%d"))
    url = f"{ENDPOINT}?startDate={year}0101&endDate={end}&response=json"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.load(resp)
            if payload.get("stat") != "OK":
                return []
            return payload.get("data") or []
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                http.client.IncompleteRead, http.client.HTTPException,
                ConnectionError, OSError) as exc:
            if attempt == retries - 1:
                raise
            print(f"    retry {attempt + 1} {year} ({exc})", flush=True)
            time.sleep(5 * (attempt + 1))
    return []


def to_rows(raw: list[list]) -> list[dict]:
    out = []
    for r in raw:
        if len(r) < 7:
            continue
        ex_date = roc_to_iso(r[0])
        prev_close, ref_price = _num(r[3]), _num(r[4])
        if not ex_date or not prev_close or not ref_price or prev_close <= 0:
            continue
        out.append(
            {
                "stock_id": str(r[1]).strip(),
                "anchor_date": ex_date,
                "anchor_kind": "ex",
                "prev_close": prev_close,
                "ref_price": ref_price,
                "factor": ref_price / prev_close,
                "kind": str(r[6]).strip() or None,
                "source": SOURCE,
            }
        )
    return out


def upsert(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_ex_adjust_event (
            stock_id, anchor_date, anchor_kind, prev_close, ref_price,
            factor, kind, source, synced_at
        ) VALUES (
            :stock_id, :anchor_date, :anchor_kind, :prev_close, :ref_price,
            :factor, :kind, :source, :synced_at
        )
        ON CONFLICT(stock_id, anchor_date, source) DO UPDATE SET
            prev_close=excluded.prev_close, ref_price=excluded.ref_price,
            factor=excluded.factor, kind=excluded.kind,
            synced_at=excluded.synced_at
    """
    conn.executemany(sql, [{**r, "synced_at": synced_at} for r in rows])
    conn.commit()
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start-year", type=int, default=2011)
    ap.add_argument("--end-year", type=int, default=2026)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--sleep", type=float, default=3.0)
    args = ap.parse_args()

    conn = connect(args.db)
    total = 0
    try:
        for year in range(args.start_year, args.end_year + 1):
            rows = to_rows(fetch_year(year))
            n = upsert(conn, rows)
            total += n
            print(f"{year}: {n:>5,} 筆除權息事件", flush=True)
            if year != args.end_year:
                time.sleep(args.sleep)
    finally:
        conn.close()
    print(f"\n合計 {total:,} 筆")
    return 0


if __name__ == "__main__":
    sys.exit(main())
