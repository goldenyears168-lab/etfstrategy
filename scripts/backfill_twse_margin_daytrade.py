#!/usr/bin/env python3
"""TWSE 全市場融資融券（MI_MARGN）＋ 當日沖銷（TWTB4U）→ SQLite 回補。

**動機**：``src/sync_stock_chip_daily.py`` 的宇宙是
``load_etf_constituent_watchlist()``——**只跟 ETF 成分股**。2026-07 成分股換血
後有 19 檔（含 2492 華新科、1101 台泥、2207 和泰車）掉出名單，融資融券與當沖
就從 2026-06-26 起靜默停更，不會報錯。本腳本改打 TWSE 全市場端點，一天一次
請求涵蓋所有標的，徹底拔掉 ETF 名單這個 gate。

寫入 ``source='twse_mi_margn'`` / ``'twse_twtb4u'``，不覆蓋既有 finmind 列。

當沖比例採「當沖成交股數 ÷ 全日成交股數」，不是買/賣金額之比（後者恆等
99~100%，無意義——見 stock_daytrade_daily 的既有 bug）。全日成交股數取自
``stock_daily_bars``；取不到時 ``daytrade_ratio_pct`` 留 NULL，不亂填。
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

MARGIN_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
DAYTRADE_URL = "https://www.twse.com.tw/exchangeReport/TWTB4U"
MARGIN_SOURCE = "twse_mi_margn"
DAYTRADE_SOURCE = "twse_twtb4u"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

_RETRYABLE = (
    urllib.error.URLError, TimeoutError, json.JSONDecodeError,
    http.client.IncompleteRead, http.client.HTTPException,
    ConnectionError, OSError,
)


def _num(s) -> float | None:
    t = str(s or "").replace(",", "").strip()
    if not t or t in {"-", "--"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _get(url: str, retries: int = 3) -> dict | None:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Referer": "https://www.twse.com.tw/"}
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
            if not raw.strip():
                return None
            return json.loads(raw)
        except _RETRYABLE as exc:
            if attempt == retries - 1:
                raise
            print(f"    retry {attempt + 1} ({exc})", flush=True)
            time.sleep(4 * (attempt + 1))
    return None


def _pick_table(payload: dict, keyword: str) -> dict | None:
    for t in payload.get("tables") or []:
        if any(keyword in str(f) for f in (t.get("fields") or [])):
            return t
    return None


def fetch_margin(day: date) -> list[dict]:
    """MI_MARGN 融資融券彙總。欄位 16 個，融資 6 欄在前、融券 6 欄在後。"""
    p = _get(f"{MARGIN_URL}?date={day:%Y%m%d}&selectType=ALL&response=json")
    if not p or p.get("stat") != "OK":
        return []
    t = _pick_table(p, "次一營業日限額")
    if not t:
        return []
    iso = day.isoformat()
    out = []
    for r in t.get("data") or []:
        if len(r) < 14:
            continue
        sid = str(r[0]).strip()
        margin_bal, short_bal = _num(r[6]), _num(r[12])
        if margin_bal is None and short_bal is None:
            continue
        prev_m, prev_s = _num(r[5]), _num(r[11])
        out.append(
            {
                "stock_id": sid,
                "trade_date": iso,
                "margin_balance": margin_bal,
                "margin_change": (
                    None if margin_bal is None or prev_m is None else margin_bal - prev_m
                ),
                "short_balance": short_bal,
                "short_change": (
                    None if short_bal is None or prev_s is None else short_bal - prev_s
                ),
                "source": MARGIN_SOURCE,
            }
        )
    return out


def fetch_daytrade(day: date) -> list[dict]:
    p = _get(f"{DAYTRADE_URL}?response=json&date={day:%Y%m%d}&selectType=All")
    if not p or p.get("stat") != "OK":
        return []
    t = _pick_table(p, "當日沖銷交易成交股數")
    if not t:
        return []
    iso = day.isoformat()
    out = []
    for r in t.get("data") or []:
        if len(r) < 4:
            continue
        vol = _num(r[3])
        if vol is None:
            continue
        out.append(
            {
                "stock_id": str(r[0]).strip(),
                "trade_date": iso,
                "daytrade_volume": vol,
                "source": DAYTRADE_SOURCE,
            }
        )
    return out


def upsert_margin(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    at = utc_now_iso()
    conn.executemany(
        """
        INSERT INTO stock_margin_daily (
            stock_id, trade_date, margin_balance, margin_change,
            short_balance, short_change, source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :margin_balance, :margin_change,
            :short_balance, :short_change, :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, source) DO UPDATE SET
            margin_balance=excluded.margin_balance,
            margin_change=excluded.margin_change,
            short_balance=excluded.short_balance,
            short_change=excluded.short_change,
            synced_at=excluded.synced_at
        """,
        [{**r, "synced_at": at} for r in rows],
    )
    conn.commit()
    return len(rows)


def upsert_daytrade(conn, rows: list[dict], day: date) -> int:
    """當沖比例 = 當沖成交股數 ÷ 全日成交股數（取自 stock_daily_bars）。"""
    if not rows:
        return 0
    cur = conn.execute(
        "SELECT stock_id, MAX(volume) FROM stock_daily_bars "
        "WHERE trade_date=? AND volume>0 GROUP BY stock_id",
        (day.isoformat(),),
    )
    totals = {r[0]: r[1] for r in cur}
    at = utc_now_iso()
    payload = []
    for r in rows:
        tv = totals.get(r["stock_id"])
        payload.append(
            {
                **r,
                "total_volume": tv,
                "daytrade_ratio_pct": (
                    round(r["daytrade_volume"] / tv * 100, 4) if tv else None
                ),
                "synced_at": at,
            }
        )
    conn.executemany(
        """
        INSERT INTO stock_daytrade_daily (
            stock_id, trade_date, daytrade_volume, total_volume,
            daytrade_ratio_pct, source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :daytrade_volume, :total_volume,
            :daytrade_ratio_pct, :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, source) DO UPDATE SET
            daytrade_volume=excluded.daytrade_volume,
            total_volume=excluded.total_volume,
            daytrade_ratio_pct=excluded.daytrade_ratio_pct,
            synced_at=excluded.synced_at
        """,
        payload,
    )
    conn.commit()
    return len(payload)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--sleep", type=float, default=2.5)
    ap.add_argument("--skip-daytrade", action="store_true")
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    conn = connect(args.db)
    try:
        m_total = d_total = days = 0
        day = start
        while day <= end:
            if day.weekday() >= 5:
                day += timedelta(days=1)
                continue
            m = fetch_margin(day)
            if m:
                m_total += upsert_margin(conn, m)
                days += 1
                if not args.skip_daytrade:
                    time.sleep(args.sleep)
                    d_total += upsert_daytrade(conn, fetch_daytrade(day), day)
                print(f"  {day}  融資券 {len(m):>5} 檔 · 當沖 {d_total:>7,} 累計", flush=True)
            time.sleep(args.sleep)
            day += timedelta(days=1)
        print(f"\n完成：{days} 交易日 · 融資券 {m_total:,} 列 · 當沖 {d_total:,} 列")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
