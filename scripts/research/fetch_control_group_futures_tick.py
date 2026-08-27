#!/usr/bin/env python3
"""下載「跑過分點漏斗但因超額<2pp被刷掉」對照組個股期貨逐筆成交（IS+OOS兩期間）.

用來檢驗 expert_pool_futures_open_breakout_scan.py 找到的開盤動能訊號，是不是
「因為專家池篩選」——如果同樣經過漏斗評估、只是分點alpha沒過關的這組股票也有
一樣的效應，代表訊號跟分點共識無關，只是這批股票（AI/熱門供應鏈概念股）本身
波動大、動能強。

用法：
  PYTHONPATH=src .venv/bin/python \
    scripts/research/fetch_control_group_futures_tick.py
"""

from __future__ import annotations

import csv
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import stock_db
from finmind_client import fetch_finmind_json

STOCK_FUTURES: dict[str, str] = {
    "1101": "DFF", "1303": "CAF", "1560": "EOF", "2301": "FQF", "2313": "FTF",
    "2330": "CDF", "2345": "OPF", "2353": "DSF", "2357": "DJF", "2360": "MJF",
    "2368": "RKF", "2379": "GJF", "2382": "DKF", "2395": "VIF", "2404": "GOF",
    "2449": "GRF", "2454": "DVF", "2603": "CZF", "2891": "CNF", "3006": "IIF",
    "3008": "IJF", "3017": "RAF", "3231": "DXF", "3260": "NDF", "3264": "NEF",
    "3532": "QDF", "3661": "UMF", "5274": "PZF", "5371": "NMF", "5483": "NOF",
    "6139": "UBF", "6278": "KEF", "6285": "KGF", "6770": "QZF", "8069": "NVF",
    "8150": "QUF", "8299": "NWF",
}

IS_DAYS = [
    "2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16", "2026-07-17",
    "2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23", "2026-07-24",
    "2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31",
    "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07",
    "2026-08-10", "2026-08-11",
]
OOS_DAYS = [
    "2025-10-20", "2025-10-21", "2025-10-22", "2025-10-23", "2025-10-27",
    "2025-10-28", "2025-10-29", "2025-10-30", "2025-10-31", "2025-11-03",
    "2025-11-04", "2025-11-05", "2025-11-06", "2025-11-07", "2025-11-10",
    "2025-11-11", "2025-11-12", "2025-11-13", "2025-11-14", "2025-11-17",
    "2025-11-18", "2025-11-19", "2025-11-20", "2025-11-21", "2025-11-24",
]

SLEEP = 0.3
OUT_DIR = Path(__file__).resolve().parents[2] / "reports/research/expert_pool_futures_tick"


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def fetch_day_ticks(futures_id: str, day: str) -> list[dict]:
    payload = fetch_finmind_json(
        {"dataset": "TaiwanFuturesTick", "data_id": futures_id, "start_date": day},
        timeout=60,
    )
    return payload.get("data") or []


def near_month_contract(rows: list[dict]) -> str | None:
    from collections import defaultdict

    vol_by_contract: dict[str, float] = defaultdict(float)
    for r in rows:
        cd = str(r.get("contract_date", ""))
        if "/" in cd:
            continue
        vol_by_contract[cd] += float(r.get("volume") or 0)
    if not vol_by_contract:
        return None
    return max(vol_by_contract, key=lambda k: vol_by_contract[k])


def fetch_period(sid: str, fid: str, days: list[str], tag: str) -> None:
    all_ticks: list[dict] = []
    contract_by_day: dict[str, str] = {}
    for day in days:
        try:
            rows = fetch_day_ticks(fid, day)
        except Exception as exc:
            log(f"    {day}: FETCH FAIL {exc}")
            time.sleep(SLEEP)
            continue
        contract = near_month_contract(rows)
        if not contract:
            time.sleep(SLEEP)
            continue
        kept = [r for r in rows if str(r.get("contract_date", "")) == contract]
        contract_by_day[day] = contract
        all_ticks.extend(kept)
        time.sleep(SLEEP)
    all_ticks.sort(key=lambda r: r["date"])
    if not all_ticks:
        log(f"  {sid} [{tag}]: 無資料，跳過")
        return
    out_path = OUT_DIR / f"ctrl_{sid}_{fid}_tick_{days[0]}_{days[-1]}.csv"
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "futures_id", "contract_date", "price", "volume"])
        w.writeheader()
        w.writerows(all_ticks)
    contracts = "/".join(sorted(set(contract_by_day.values())))
    log(f"  {sid} [{tag}]: {len(all_ticks)} 筆 -> {out_path.name}（合約：{contracts}）")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log(f"對照組 {len(STOCK_FUTURES)} 檔 · IS {len(IS_DAYS)}天 + OOS {len(OOS_DAYS)}天")
    for i, (sid, fid) in enumerate(STOCK_FUTURES.items(), 1):
        log(f"[{i}/{len(STOCK_FUTURES)}] {sid} ({fid})")
        fetch_period(sid, fid, IS_DAYS, "IS")
        fetch_period(sid, fid, OOS_DAYS, "OOS")
    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
