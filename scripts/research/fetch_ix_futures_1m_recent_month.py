#!/usr/bin/env python3
"""下載景碩個股期貨（IXF）近一個月逐筆資料，重建成1分K CSV。

FinMind 沒有現成的期貨分鐘K dataset（TaiwanFuturesMinute 不存在），
只有 TaiwanFuturesTick（一次只能抓一天）。逐日抓取後只留當天成交量
最大的「近月」合約（排除跨月價差單），再依分鐘做 OHLCV 聚合。

用法：
  PYTHONPATH=src .venv/bin/python \
    scripts/research/fetch_ix_futures_1m_recent_month.py
"""

from __future__ import annotations

import csv
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import stock_db
from finmind_client import fetch_finmind_json

FUTURES_ID = "IXF"
STOCK_ID = "3189"
N_DAYS = 22
SLEEP = 0.3
OUT_DIR = Path(__file__).resolve().parents[2] / "reports/research"


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def trading_days(n: int) -> list[str]:
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT DISTINCT trade_date FROM stock_daily_bars "
            "ORDER BY trade_date DESC LIMIT ?",
            (n,),
        ).fetchall()
    finally:
        con.close()
    return sorted(r[0] for r in rows)


def fetch_day_ticks(day: str) -> list[dict]:
    payload = fetch_finmind_json(
        {"dataset": "TaiwanFuturesTick", "data_id": FUTURES_ID, "start_date": day},
        timeout=60,
    )
    return payload.get("data") or []


def near_month_contract(rows: list[dict]) -> str | None:
    vol_by_contract: dict[str, float] = defaultdict(float)
    for r in rows:
        cd = str(r.get("contract_date", ""))
        if "/" in cd:
            continue
        vol_by_contract[cd] += float(r.get("volume") or 0)
    if not vol_by_contract:
        return None
    return max(vol_by_contract, key=lambda k: vol_by_contract[k])


def resample_1m(rows: list[dict], contract: str) -> list[dict]:
    buckets: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for r in rows:
        if str(r.get("contract_date", "")) != contract:
            continue
        price = float(r.get("price") or 0)
        vol = float(r.get("volume") or 0)
        if price <= 0:
            continue
        ts = r["date"]  # 'YYYY-MM-DD HH:MM:SS'
        minute_key = ts[:16]  # 'YYYY-MM-DD HH:MM'
        buckets[minute_key].append((price, vol))
    bars = []
    for minute_key in sorted(buckets):
        ticks = buckets[minute_key]
        prices = [p for p, _ in ticks]
        bars.append(
            {
                "minute": minute_key,
                "open": prices[0],
                "high": max(prices),
                "low": min(prices),
                "close": prices[-1],
                "volume": sum(v for _, v in ticks),
                "n_ticks": len(ticks),
                "contract_date": contract,
            }
        )
    return bars


def main() -> int:
    days = trading_days(N_DAYS)
    log(f"目標交易日 {len(days)} 天：{days[0]} ~ {days[-1]}")

    all_bars: list[dict] = []
    contract_by_day: dict[str, str] = {}
    for day in days:
        try:
            rows = fetch_day_ticks(day)
        except Exception as exc:
            log(f"  {day}: FETCH FAIL {exc}")
            time.sleep(SLEEP)
            continue
        contract = near_month_contract(rows)
        if not contract:
            log(f"  {day}: 無資料（未掛牌或無成交）")
            time.sleep(SLEEP)
            continue
        bars = resample_1m(rows, contract)
        contract_by_day[day] = contract
        all_bars.extend(bars)
        log(f"  {day}: 近月={contract} · tick={len(rows)} · 1m bars={len(bars)}")
        time.sleep(SLEEP)

    if not all_bars:
        log("無任何資料，未寫檔")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"ix_futures_{STOCK_ID}_1m_{days[0]}_{days[-1]}.csv"
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=["minute", "open", "high", "low", "close", "volume", "n_ticks", "contract_date"]
        )
        w.writeheader()
        w.writerows(all_bars)

    log(f"寫入 {out_path}（{len(all_bars)} 根1分K）")
    log(f"逐日近月合約：{contract_by_day}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
