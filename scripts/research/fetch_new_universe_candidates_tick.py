#!/usr/bin/env python3
"""下載「借券低+流動性夠」新篩出的26檔候選個股期貨IS+OOS逐筆成交.

這批股票不在原本67檔（30專家池+37對照組）之列，是用同一套「借券餘額低+
成交金額夠」的邏輯，從全域251檔個股期貨清單裡另外篩出來的候選，看看能不能
再擴大訊號可用的標的池。

用法：
  PYTHONPATH=src .venv/bin/python \
    scripts/research/fetch_new_universe_candidates_tick.py
"""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime
from pathlib import Path

from fetch_expert_pool_futures_tick_recent_month import fetch_day_ticks, near_month_contract

SLEEP = 0.3
OUT_DIR = Path(__file__).resolve().parents[2] / "reports/research/expert_pool_futures_tick"

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


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


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
    out_path = OUT_DIR / f"newu_{sid}_{fid}_tick_{days[0]}_{days[-1]}.csv"
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "futures_id", "contract_date", "price", "volume"])
        w.writeheader()
        w.writerows(all_ticks)
    contracts = "/".join(sorted(set(contract_by_day.values())))
    log(f"  {sid} [{tag}]: {len(all_ticks)} 筆 -> {out_path.name}（合約：{contracts}）")


def main() -> int:
    STOCK_FUTURES = json.loads(Path("/tmp/new_candidates_futures.json").read_text())
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log(f"新候選 {len(STOCK_FUTURES)} 檔 · IS {len(IS_DAYS)}天 + OOS {len(OOS_DAYS)}天")
    for i, (sid, fid) in enumerate(STOCK_FUTURES.items(), 1):
        log(f"[{i}/{len(STOCK_FUTURES)}] {sid} ({fid})")
        fetch_period(sid, fid, IS_DAYS, "IS")
        fetch_period(sid, fid, OOS_DAYS, "OOS")
    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
