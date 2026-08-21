#!/usr/bin/env python3
"""全市場三大法人買賣超（TWSE T86 ＋ 櫃買 insti/dailyTrade）→ stock_institutional_daily。

**動機**：既有 finmind 來源的宇宙是 ETF 成分股 watchlist，實測 2026-08-18 只有
**271 檔**，而全市場有 1,300+ 檔普通股。三大法人是最常被引用的籌碼維度，卻是
目前覆蓋最差的一塊。TWSE `T86?selectType=ALL` 一天一次請求即涵蓋全市場
（2026-08-19 實測 15,211 列，含 ETF／權證，下游用 4 碼代號過濾）。

寫入 ``source='twse_t86'`` / ``'tpex_insti'``，不覆蓋既有 finmind 列。
⚠️ 下游查詢時必須去重（同 stock-day 會有兩個 source），做法見
``scripts/research/sbl_fee_cross_section_panel.py`` 的 ``load_chips``。

欄位對應（單位：股）——``foreign_net`` 採「外陸資（不含外資自營商）＋ 外資自營商」，
``dealer_self_net`` 採官方的「自營商買賣超股數」（＝自行買賣＋避險）。

⚠️ **與 finmind 的口徑差異（已對帳確認，不是 bug）**：外資與投信兩欄兩來源
完全相同（2026-08-19 實測 2408 為 −13,747,190／+380,000，2492 為
+3,180,325／−2,000），但 ``dealer_self_net`` 不同——finmind 只計「自營商
(自行買賣)」，本來源計官方總額（含避險），故 ``three_institution_net`` 也會差
一個避險部位（2408 −14,426,841 vs finmind −14,203,331）。**本來源才是官方
三大法人買賣超口徑**；混用兩個 source 做時間序列會在銜接處出現假跳動。
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
from datetime import date, datetime, timedelta
from pathlib import Path

from stock_db import connect
from stock_db.util import DEFAULT_DB_PATH, utc_now_iso

TWSE_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TPEX_URL = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
# 櫃買憑證鏈缺 Subject Key Identifier，本機 Python 預設開 VERIFY_X509_STRICT 會擋掉
_CTX = ssl.create_default_context()
_CTX.verify_flags &= ~ssl.VERIFY_X509_STRICT

_RETRYABLE = (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
              http.client.IncompleteRead, http.client.HTTPException,
              ConnectionError, OSError)


def _num(s) -> float | None:
    t = str(s or "").replace(",", "").strip()
    if not t or t in {"-", "--"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _get(url: str, retries: int = 5) -> dict | None:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Referer": "https://www.twse.com.tw/"})
            with urllib.request.urlopen(req, timeout=60, context=_CTX) as r:
                raw = r.read()
            return json.loads(raw) if raw.strip() else None
        except _RETRYABLE as exc:
            if attempt == retries - 1:
                raise
            print(f"    retry {attempt + 1} ({exc})", flush=True)
            time.sleep(5 * (attempt + 1))
    return None


def _table(payload: dict, keyword: str) -> dict | None:
    """TWSE T86 把 fields/data 放在**頂層**，櫃買才放在 tables 陣列裡——兩種都吃。"""
    for t in payload.get("tables") or []:
        if any(keyword in str(f) for f in (t.get("fields") or [])):
            return t
    if any(keyword in str(f) for f in (payload.get("fields") or [])):
        return payload
    return None


def fetch_twse(day: date) -> list[dict]:
    p = _get(f"{TWSE_URL}?date={day:%Y%m%d}&selectType=ALL&response=json")
    if not p or p.get("stat") != "OK":
        return []
    t = _table(p, "三大法人買賣超股數")
    if not t:
        return []
    f = [str(x).strip() for x in t["fields"]]
    i = {n: k for k, n in enumerate(f)}
    need = ("證券代號", "外陸資買賣超股數(不含外資自營商)", "外資自營商買賣超股數",
            "投信買賣超股數", "自營商買賣超股數", "三大法人買賣超股數")
    if any(k not in i for k in need):
        return []
    iso = day.isoformat()
    out = []
    for r in t.get("data") or []:
        fo = _num(r[i["外陸資買賣超股數(不含外資自營商)"]])
        fd = _num(r[i["外資自營商買賣超股數"]])
        it = _num(r[i["投信買賣超股數"]])
        de = _num(r[i["自營商買賣超股數"]])
        tot = _num(r[i["三大法人買賣超股數"]])
        if tot is None:
            continue
        out.append({
            "stock_id": str(r[i["證券代號"]]).strip(),
            "trade_date": iso,
            "close_price": None,
            "foreign_net": (fo or 0) + (fd or 0),
            "investment_trust_net": it,
            "dealer_self_net": de,
            "three_institution_net": tot,
            "source": "twse_t86",
        })
    return out


def fetch_tpex(day: date) -> list[dict]:
    p = _get(f"{TPEX_URL}?type=Daily&sect=EW&date={day:%Y/%m/%d}&response=json")
    if not p:
        return []
    t = _table(p, "買賣超股數")
    if not t:
        return []
    f = [str(x).strip() for x in t["fields"]]
    # 櫃買欄名重複（每個法人別各有 買進/賣出/買賣超），依位置取「買賣超」欄
    idx = [k for k, n in enumerate(f) if n == "買賣超股數"]
    if len(idx) < 3:
        return []
    iso = day.isoformat()
    out = []
    for r in t.get("data") or []:
        vals = [_num(r[k]) if k < len(r) else None for k in idx]
        tot = _num(r[-1]) if len(r) > idx[-1] else None
        if tot is None:
            tot = sum(v for v in vals if v is not None) or None
        if tot is None:
            continue
        out.append({
            "stock_id": str(r[0]).strip(),
            "trade_date": iso,
            "close_price": None,
            "foreign_net": vals[0],
            "investment_trust_net": vals[1] if len(vals) > 1 else None,
            "dealer_self_net": vals[2] if len(vals) > 2 else None,
            "three_institution_net": tot,
            "source": "tpex_insti",
        })
    return out


def upsert(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    at = utc_now_iso()
    conn.executemany(
        """INSERT INTO stock_institutional_daily (
               stock_id, trade_date, close_price, foreign_net,
               investment_trust_net, dealer_self_net, three_institution_net,
               source, synced_at
           ) VALUES (
               :stock_id, :trade_date, :close_price, :foreign_net,
               :investment_trust_net, :dealer_self_net, :three_institution_net,
               :source, :synced_at)
           ON CONFLICT(stock_id, trade_date, source) DO UPDATE SET
               foreign_net=excluded.foreign_net,
               investment_trust_net=excluded.investment_trust_net,
               dealer_self_net=excluded.dealer_self_net,
               three_institution_net=excluded.three_institution_net,
               synced_at=excluded.synced_at""",
        [{**r, "synced_at": at} for r in rows])
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
                "SELECT DISTINCT trade_date FROM stock_institutional_daily "
                "WHERE source='twse_t86' AND trade_date BETWEEN ? AND ?",
                (start.isoformat(), end.isoformat()))
            done = {r[0] for r in cur}
            if done:
                print(f"resume: 已有 {len(done)} 個交易日，將跳過", flush=True)
        total = days = 0
        day, t0 = start, time.time()
        while day <= end:
            if day.weekday() >= 5 or day.isoformat() in done:
                day += timedelta(days=1)
                continue
            rows = fetch_twse(day)
            if rows:
                total += upsert(conn, rows)
                time.sleep(args.sleep)
                total += upsert(conn, fetch_tpex(day))
                days += 1
                if days % 25 == 0:
                    print(f"  {day}  {days} 交易日 / {total:,} 列  "
                          f"({(time.time() - t0) / 60:.1f} 分)", flush=True)
            time.sleep(args.sleep)
            day += timedelta(days=1)
        print(f"\n完成：{days} 交易日 / {total:,} 列")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
