#!/usr/bin/env python3
"""櫃買中心（TPEX）上櫃股票行情 → stock_daily_bars 全市場價格回補。

配套 ``backfill_twse_daily_prices.py``（上市）。兩支合起來才是完整的台股
橫斷面——實測 TWSE 借券成交明細（t13sa710）裡有 18.8% 的標的是上櫃股，
只補上市會非隨機地砍掉小型股那一段。

端點回傳額外的「發行股數」欄位，一併存進 ``shares_outstanding``（市值與
學術口徑 short interest ratio 的分母）。

寫入 ``source='tpex_daily'``，不覆蓋既有列。與上市來源一樣**沒有除權息
還原**。
"""
from __future__ import annotations

import argparse
import http.client
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, date
from pathlib import Path

from stock_db import connect
from stock_db.util import DEFAULT_DB_PATH, utc_now_iso

ENDPOINT = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
SOURCE = "tpex_daily"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

# 櫃買的憑證鏈缺 Subject Key Identifier，而本機 Python 的預設 context 開了
# VERIFY_X509_STRICT（OpenSSL 3.x），會擋掉約 7/8 的連線。只關掉 strict 這一項，
# 憑證鏈驗證與主機名檢查全部保留——不是 CERT_NONE。
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.verify_flags &= ~ssl.VERIFY_X509_STRICT


def _num(s) -> float | None:
    if s is None:
        return None
    t = str(s).replace(",", "").replace("+", "").strip()
    if not t or t in {"-", "--", "---"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def fetch_day(day: date, retries: int = 5) -> tuple[list[dict], list[dict]]:
    """回傳 (日線列, 除權息還原事件列)。

    櫃買日行情附「次日參考價」，除權息日前一交易日該值 != 收盤價，其比值
    就是還原因子——不必另外去打除權息計算結果表。
    """
    url = f"{ENDPOINT}?date={day:%Y/%m/%d}&type=EW&response=json"
    payload = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as resp:
                payload = json.load(resp)
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                http.client.IncompleteRead, http.client.HTTPException,
                ConnectionError, OSError) as exc:
            if attempt == retries - 1:
                raise
            time.sleep(4 * (attempt + 1))
            print(f"    retry {attempt + 1} {day} ({exc})", flush=True)
    if not payload:
        return [], []
    # 端點對非交易日會回上一個交易日的資料——用回傳的 date 欄位擋掉，
    # 否則會把休市日灌成前一日的複本。
    if str(payload.get("date") or "") != f"{day:%Y%m%d}":
        return [], []

    iso = day.isoformat()
    out: list[dict] = []
    events: list[dict] = []
    for table in payload.get("tables") or []:
        fields = [str(f).strip() for f in (table.get("fields") or [])]
        if "收盤" not in fields:
            continue
        idx = {name: i for i, name in enumerate(fields)}
        for item in table.get("data") or []:
            close = _num(item[idx["收盤"]])
            if close is None or close <= 0:
                continue
            out.append(
                {
                    "stock_id": str(item[idx["代號"]]).strip(),
                    "trade_date": iso,
                    "open": _num(item[idx["開盤"]]),
                    "high": _num(item[idx["最高"]]),
                    "low": _num(item[idx["最低"]]),
                    "close": close,
                    "volume": _num(item[idx["成交股數"]]),
                    "amount": _num(item[idx["成交金額(元)"]]),
                    "adj_close": None,
                    "shares_outstanding": (
                        _num(item[idx["發行股數"]]) if "發行股數" in idx else None
                    ),
                    "source": SOURCE,
                }
            )
            ref = _num(item[idx["次日 參考價"]]) if "次日 參考價" in idx else None
            if ref and ref > 0 and abs(ref - close) / close > 1e-3:
                events.append(
                    {
                        "stock_id": str(item[idx["代號"]]).strip(),
                        "anchor_date": iso,
                        "anchor_kind": "cum",
                        "prev_close": close,
                        "ref_price": ref,
                        "factor": ref / close,
                        "kind": None,
                        "source": SOURCE,
                    }
                )
    return out, events


def upsert(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_daily_bars (
            stock_id, trade_date, open, high, low, close, volume, amount,
            adj_close, shares_outstanding, source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :open, :high, :low, :close, :volume, :amount,
            :adj_close, :shares_outstanding, :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, source) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume, amount=excluded.amount,
            shares_outstanding=excluded.shares_outstanding,
            synced_at=excluded.synced_at
    """
    conn.executemany(sql, [{**r, "synced_at": synced_at} for r in rows])
    conn.commit()
    return len(rows)


def upsert_events(conn, rows: list[dict]) -> int:
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
            prev_close=excluded.prev_close,
            ref_price=excluded.ref_price,
            factor=excluded.factor,
            synced_at=excluded.synced_at
    """
    conn.executemany(sql, [{**r, "synced_at": synced_at} for r in rows])
    conn.commit()
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--sleep", type=float, default=2.6)
    ap.add_argument("--no-resume", action="store_true")
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    conn = connect(args.db)
    try:
        done: set[str] = set()
        if not args.no_resume:
            cur = conn.execute(
                "SELECT DISTINCT trade_date FROM stock_daily_bars "
                "WHERE source=? AND trade_date BETWEEN ? AND ?",
                (SOURCE, start.isoformat(), end.isoformat()),
            )
            done = {r[0] for r in cur}
            if done:
                print(f"resume: 已有 {len(done)} 個交易日，將跳過", flush=True)

        total = days = holidays = ev_total = 0
        day = start
        t0 = time.time()
        while day <= end:
            if day.weekday() >= 5 or day.isoformat() in done:
                day += timedelta(days=1)
                continue
            rows, events = fetch_day(day)
            if rows:
                total += upsert(conn, rows)
                ev_total += upsert_events(conn, events)
                days += 1
                if days % 25 == 0:
                    print(
                        f"  {day}  累計 {days} 交易日 / {total:,} 列 / "
                        f"{ev_total:,} 還原事件  ({(time.time() - t0) / 60:.1f} 分)",
                        flush=True,
                    )
            else:
                holidays += 1
            time.sleep(args.sleep)
            day += timedelta(days=1)
        print(
            f"\n完成：{days} 交易日 / {total:,} 列 / {ev_total:,} 還原事件 · "
            f"{holidays} 個非交易日"
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
