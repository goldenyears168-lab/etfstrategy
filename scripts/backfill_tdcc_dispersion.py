#!/usr/bin/env python3
"""集保股權分散表 → stock_holding_dispersion_weekly。

兩個來源：
  · **TDCC opendata**（``--weekly``）：一個請求拿全市場最新一週（~4,000 檔），
    每週更新用這條。比 FinMind 逐檔 893 次快兩個數量級。
  · **FinMind 快取**（``--from-pickle``）：把先前研究抓下的歷史一次灌進 DB。

級距碼一律正規化成 TDCC 的 "1".."17"（FinMind 的文字標籤會被轉碼），
兩個來源才能用同一組 SQL 查。散戶＝1–8（<50 張）、大戶＝12–15（>400 張）。

⚠️ PIT：``as_of_date`` 是週五結算日，集保下週一二才公布 —— 使用端必須
自己加 +4 個日曆天的緩衝，本檔只負責如實存入結算日。
"""
from __future__ import annotations

import argparse
import csv
import io
import ssl
import sys
import urllib.request
from pathlib import Path

from stock_db import DEFAULT_DB_PATH, connect
from stock_db.util import utc_now_iso

URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
_CTX = ssl.create_default_context()
_CTX.verify_flags &= ~ssl.VERIFY_X509_STRICT          # 見 tpex-ssl-x509-strict

# TDCC 級距碼 → (下界, 上界)；16=差異數調整、17=合計
BOUNDS = {
    "1": (1, 999), "2": (1000, 5000), "3": (5001, 10000), "4": (10001, 15000),
    "5": (15001, 20000), "6": (20001, 30000), "7": (30001, 40000),
    "8": (40001, 50000), "9": (50001, 100000), "10": (100001, 200000),
    "11": (200001, 400000), "12": (400001, 600000), "13": (600001, 800000),
    "14": (800001, 1000000), "15": (1000001, None),
}
# FinMind 文字標籤 → TDCC 級距碼
FINMIND = {
    "1-999": "1", "1,000-5,000": "2", "5,001-10,000": "3", "10,001-15,000": "4",
    "15,001-20,000": "5", "20,001-30,000": "6", "30,001-40,000": "7",
    "40,001-50,000": "8", "50,001-100,000": "9", "100,001-200,000": "10",
    "200,001-400,000": "11", "400,001-600,000": "12", "600,001-800,000": "13",
    "800,001-1,000,000": "14", "more than 1,000,001": "15",
    "差異數調整（說明4）": "16", "total": "17",
}
RETAIL = [str(i) for i in range(1, 9)]
BIG = ["12", "13", "14", "15"]


def _num(x, cast=float):
    try:
        return cast(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def upsert(conn, rows: list[tuple]) -> int:
    conn.executemany(
        """INSERT INTO stock_holding_dispersion_weekly
             (stock_id, as_of_date, level, level_lo, level_hi, people, shares,
              percent, source, synced_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(stock_id, as_of_date, level, source) DO UPDATE SET
             level_lo=excluded.level_lo, level_hi=excluded.level_hi,
             people=excluded.people, shares=excluded.shares,
             percent=excluded.percent, synced_at=excluded.synced_at""", rows)
    conn.commit()
    return len(rows)


def fetch_weekly(conn) -> int:
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    body = urllib.request.urlopen(req, timeout=120, context=_CTX).read().decode("utf-8-sig")
    now = utc_now_iso()
    rows = []
    for r in csv.DictReader(io.StringIO(body)):
        sid = (r.get("證券代號") or "").strip()
        lvl = (r.get("持股分級") or "").strip()
        d = (r.get("資料日期") or "").strip()
        if not (sid and lvl and len(d) == 8):
            continue
        lo, hi = BOUNDS.get(lvl, (None, None))
        rows.append((sid, f"{d[:4]}-{d[4:6]}-{d[6:]}", lvl, lo, hi,
                     _num(r.get("人數"), int), _num(r.get("股數")),
                     _num(r.get("占集保庫存數比例%")), "tdcc", now))
    if not rows:
        raise RuntimeError("TDCC 回傳 0 列")
    dates = {r[1] for r in rows}
    print(f"  TDCC {sorted(dates)} · {len({r[0] for r in rows}):,} 檔 · {len(rows):,} 列")
    return upsert(conn, rows)


def from_pickle(conn, path: Path) -> int:
    import pandas as pd
    d = pd.read_pickle(path)
    now = utc_now_iso()
    rows = []
    for t in d.itertuples():
        lvl = FINMIND.get(str(t.HoldingSharesLevel).strip())
        if not lvl:
            continue
        lo, hi = BOUNDS.get(lvl, (None, None))
        date = str(t.date)[:10]
        # FinMind 的 unit 是股數（可能為千股單位，僅存原值供對帳）
        rows.append((str(t.stock_id), date, lvl, lo, hi,
                     _num(t.people, int), _num(getattr(t, "unit", None)),
                     _num(t.percent), "finmind", now))
    print(f"  pickle {len({r[0] for r in rows}):,} 檔 · {len(rows):,} 列")
    return upsert(conn, rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--weekly", action="store_true", help="抓 TDCC 最新一週（全市場）")
    ap.add_argument("--from-pickle", type=Path, help="灌入 FinMind 歷史快取")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = ap.parse_args()
    if not (args.weekly or args.from_pickle):
        ap.error("至少要指定 --weekly 或 --from-pickle")
    conn = connect(args.db)
    try:
        n = 0
        if args.from_pickle:
            n += from_pickle(conn, args.from_pickle)
        if args.weekly:
            n += fetch_weekly(conn)
        print(f"完成：{n:,} 列寫入")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
