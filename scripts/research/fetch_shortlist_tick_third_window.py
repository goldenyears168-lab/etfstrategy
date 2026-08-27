#!/usr/bin/env python3
"""下載6檔核心存活名單在第三段（2025-11-28~2025-12-19，0050盤整段）的逐筆成交.

用來對 expert_pool_futures_open_breakout_scan.py 找到、且cost-stress後仍存活的
6檔核心名單（3017/5371/3081/6213/2308/2345）做第三段獨立window驗證，跟IS(偏多月)
OOS(拉回月)不同市場狀態再對照一次。

用法：
  PYTHONPATH=src .venv/bin/python \
    scripts/research/fetch_shortlist_tick_third_window.py
"""

from __future__ import annotations

import csv
import time
from datetime import datetime
from pathlib import Path

from fetch_expert_pool_futures_tick_recent_month import fetch_day_ticks, near_month_contract

SLEEP = 0.3
OUT_DIR = Path(__file__).resolve().parents[2] / "reports/research/expert_pool_futures_tick"

STOCK_FUTURES = {
    "3017": "RAF", "5371": "NMF", "3081": "OTF", "6213": "KBF", "2308": "FRF", "2345": "OPF",
}
DAYS = [
    "2025-11-28", "2025-12-01", "2025-12-02", "2025-12-03", "2025-12-04",
    "2025-12-05", "2025-12-08", "2025-12-09", "2025-12-10", "2025-12-11",
    "2025-12-12", "2025-12-15", "2025-12-16", "2025-12-17", "2025-12-18",
    "2025-12-19",
]


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log(f"第三窗 {len(DAYS)} 天 · {len(STOCK_FUTURES)} 檔")
    for sid, fid in STOCK_FUTURES.items():
        all_ticks = []
        contract_by_day = {}
        for d in DAYS:
            try:
                rows = fetch_day_ticks(fid, d)
            except Exception as exc:
                log(f"  {sid} {d}: FAIL {exc}")
                time.sleep(SLEEP)
                continue
            contract = near_month_contract(rows)
            if not contract:
                time.sleep(SLEEP)
                continue
            kept = [r for r in rows if str(r.get("contract_date", "")) == contract]
            contract_by_day[d] = contract
            all_ticks.extend(kept)
            time.sleep(SLEEP)
        all_ticks.sort(key=lambda r: r["date"])
        if not all_ticks:
            log(f"  {sid}: 無資料")
            continue
        out_path = OUT_DIR / f"w3_{sid}_{fid}_tick_{DAYS[0]}_{DAYS[-1]}.csv"
        with out_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["date", "futures_id", "contract_date", "price", "volume"])
            w.writeheader()
            w.writerows(all_ticks)
        contracts = "/".join(sorted(set(contract_by_day.values())))
        log(f"  {sid}: {len(all_ticks)} 筆 -> {out_path.name}（合約：{contracts}）")
    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
