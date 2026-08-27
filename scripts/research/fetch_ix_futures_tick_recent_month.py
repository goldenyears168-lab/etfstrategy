#!/usr/bin/env python3
"""下載景碩個股期貨（IXF）近一個月逐筆成交（不聚合）。

FinMind TaiwanFuturesTick 一次只能抓一天，逐日抓取後只留當天成交量
最大的「近月」合約（排除跨月價差單），原始逐筆直接寫出。

用法：
  PYTHONPATH=src .venv/bin/python \
    scripts/research/fetch_ix_futures_tick_recent_month.py
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


def main() -> int:
    days = trading_days(N_DAYS)
    log(f"目標交易日 {len(days)} 天：{days[0]} ~ {days[-1]}")

    all_ticks: list[dict] = []
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
        kept = [r for r in rows if str(r.get("contract_date", "")) == contract]
        contract_by_day[day] = contract
        all_ticks.extend(kept)
        log(f"  {day}: 近月={contract} · 逐筆={len(kept)}（原始{len(rows)}筆，含其他月/價差單被排除）")
        time.sleep(SLEEP)

    if not all_ticks:
        log("無任何資料，未寫檔")
        return 1

    all_ticks.sort(key=lambda r: r["date"])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"ix_futures_{STOCK_ID}_tick_{days[0]}_{days[-1]}.csv"
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "futures_id", "contract_date", "price", "volume"])
        w.writeheader()
        w.writerows(all_ticks)

    log(f"寫入 {out_path}（{len(all_ticks)} 筆逐筆成交）")
    log(f"逐日近月合約：{contract_by_day}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
