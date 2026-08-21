#!/usr/bin/env python3
"""TWSE 歷史借券成交明細（t13sa710）→ stock_sbl_fee_daily 日彙總回補。

**為什麼要自己回補**：``stock_lending_daily.fee_rate``（finmind 來源）存的是
當日「隨機一筆」逐筆成交的費率與數量，不是日彙總。實測 2408 於 2026-08-10
該表為「量 182 / 費率 0.01%」，而 t13sa710 逐筆實算為「量 6,731 / 量加權
4.244%」——差了兩個數量級，不能當訊號用。

**端點特性**（2026-08-20 實測）：
- 不帶 ``stockNo`` 即回傳全市場，單次查詢可涵蓋一整年
  （2025 全年 208,388 筆 / 244 交易日，無截斷跡象）
- 最早可回溯至 2004-01-05（民國 093 年），但早年極稀疏
  （2004 全年僅 1,311 筆 / 236 天）
- 費率單位是百分比（``3.25`` = 3.25%），數量單位是「交易單位」（張）

**涵蓋率警告**：本資料只涵蓋 TWSE 借券系統，不含證商／證金自辦通道。實測
2408 單日涵蓋率（對比 TWT72U 當日借出量）在 24%~100% 間波動，故日層級的
量加權費率代表性會隨涵蓋率變動——下游分析須把涵蓋率當控制變數。

用法::

    PYTHONPATH=src .venv/bin/python scripts/backfill_twse_sbl_fee.py --start-year 2004
    PYTHONPATH=src .venv/bin/python scripts/backfill_twse_sbl_fee.py --start-year 2026 --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date
from pathlib import Path

from stock_db import connect
from stock_db.util import DEFAULT_DB_PATH, utc_now_iso

ENDPOINT = "https://www.twse.com.tw/rwd/zh/lending/t13sa710"
SOURCE = "twse_t13sa710"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
EARLIEST_YEAR = 2004


def roc_to_iso(s: str) -> str:
    """``115年08月19日`` → ``2026-08-19``."""
    y, rest = s.split("年", 1)
    m, rest = rest.split("月", 1)
    d = rest.rstrip("日")
    return f"{int(y) + 1911:04d}-{int(m):02d}-{int(d):02d}"


def _num(s) -> float | None:
    if s is None:
        return None
    t = str(s).replace(",", "").strip()
    if not t or t in {"-", "--"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


class TwseRejected(RuntimeError):
    """TWSE 回了確定性的拒絕（例如查詢日期大於今日）——重試沒有意義。"""


def fetch_year(year: int, retries: int = 4) -> list[list]:
    # 當年度不能把 endDate 開到 12/31，TWSE 會回「查詢日期大於今日」整批拒絕。
    end = min(f"{year}1231", date.today().strftime("%Y%m%d"))
    if end < f"{year}0101":
        return []
    url = f"{ENDPOINT}?startDate={year}0101&endDate={end}&response=json"
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as resp:
                payload = json.load(resp)
            stat = payload.get("stat")
            if stat != "OK":
                if "大於今日" in str(stat) or "無資料" in str(stat):
                    raise TwseRejected(f"stat={stat!r}")
                raise RuntimeError(f"stat={stat!r}")
            return payload.get("data") or []
        except TwseRejected:
            raise
        except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
            last_err = exc
            sleep = 5 * (attempt + 1)
            print(f"    retry {attempt + 1}/{retries} after {sleep}s ({exc})", flush=True)
            time.sleep(sleep)
    raise RuntimeError(f"{year}: giving up after {retries} tries") from last_err


def aggregate(rows: list[list]) -> list[dict]:
    """逐筆 → (stock_id, trade_date, deal_type) 日彙總，外加 deal_type='ALL'。

    量加權費率 = sum(qty * rate) / sum(qty)。數量為 0 的成交（有出現過）不
    參與加權但仍計入筆數，避免除以零。
    """
    acc: dict[tuple[str, str, str], dict] = defaultdict(
        lambda: {
            "volume": 0.0,
            "wsum": 0.0,
            "term_wsum": 0.0,
            "tx_count": 0,
            "fee_rate_min": None,
            "fee_rate_max": None,
            "close": None,
        }
    )
    for r in rows:
        try:
            date_roc, name, deal_type = r[0], r[1], r[2]
            qty, rate, close, term = _num(r[3]), _num(r[4]), _num(r[5]), _num(r[7])
        except (IndexError, ValueError):
            continue
        if rate is None:
            continue
        stock_id = str(name).split()[0]
        trade_date = roc_to_iso(str(date_roc))
        qty = qty or 0.0
        for key in ((stock_id, trade_date, str(deal_type).strip()), (stock_id, trade_date, "ALL")):
            a = acc[key]
            a["volume"] += qty
            a["wsum"] += qty * rate
            if term is not None:
                a["term_wsum"] += qty * term
            a["tx_count"] += 1
            a["fee_rate_min"] = rate if a["fee_rate_min"] is None else min(a["fee_rate_min"], rate)
            a["fee_rate_max"] = rate if a["fee_rate_max"] is None else max(a["fee_rate_max"], rate)
            if close is not None:
                a["close"] = close

    out = []
    for (stock_id, trade_date, deal_type), a in acc.items():
        vol = a["volume"]
        out.append(
            {
                "stock_id": stock_id,
                "trade_date": trade_date,
                "deal_type": deal_type,
                "volume": vol,
                "fee_rate_vw": (a["wsum"] / vol) if vol > 0 else None,
                "fee_rate_min": a["fee_rate_min"],
                "fee_rate_max": a["fee_rate_max"],
                "tx_count": a["tx_count"],
                "close": a["close"],
                "term_days_vw": (a["term_wsum"] / vol) if vol > 0 else None,
                "source": SOURCE,
            }
        )
    return out


def upsert(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_sbl_fee_daily (
            stock_id, trade_date, deal_type, volume, fee_rate_vw,
            fee_rate_min, fee_rate_max, tx_count, close, term_days_vw,
            source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :deal_type, :volume, :fee_rate_vw,
            :fee_rate_min, :fee_rate_max, :tx_count, :close, :term_days_vw,
            :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, deal_type, source) DO UPDATE SET
            volume=excluded.volume,
            fee_rate_vw=excluded.fee_rate_vw,
            fee_rate_min=excluded.fee_rate_min,
            fee_rate_max=excluded.fee_rate_max,
            tx_count=excluded.tx_count,
            close=excluded.close,
            term_days_vw=excluded.term_days_vw,
            synced_at=excluded.synced_at
    """
    conn.executemany(sql, [{**r, "synced_at": synced_at} for r in rows])
    conn.commit()
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start-year", type=int, default=EARLIEST_YEAR)
    ap.add_argument("--end-year", type=int, default=2026)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--sleep", type=float, default=4.0, help="每年查詢間隔秒數")
    ap.add_argument("--dry-run", action="store_true", help="只抓不寫")
    args = ap.parse_args()

    conn = None if args.dry_run else connect(args.db)
    total_raw = total_agg = 0
    try:
        for year in range(args.start_year, args.end_year + 1):
            t0 = time.time()
            raw = fetch_year(year)
            agg = aggregate(raw)
            n = 0 if conn is None else upsert(conn, agg)
            total_raw += len(raw)
            total_agg += len(agg)
            dates = {r["trade_date"] for r in agg}
            ids = {r["stock_id"] for r in agg}
            print(
                f"{year}: raw={len(raw):>7,}  agg={len(agg):>7,}  written={n:>7,}  "
                f"days={len(dates):>3}  ids={len(ids):>4}  {time.time() - t0:.1f}s",
                flush=True,
            )
            if year != args.end_year:
                time.sleep(args.sleep)
    finally:
        if conn is not None:
            conn.close()
    print(f"\ntotal raw={total_raw:,}  aggregated rows={total_agg:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
