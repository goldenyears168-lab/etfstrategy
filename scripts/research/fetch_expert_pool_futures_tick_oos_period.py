#!/usr/bin/env python3
"""下載分點專家池個股期貨在指定 out-of-sample 期間的逐筆成交（每檔一個 CSV）.

用來驗證 expert_pool_futures_open_breakout_scan.py 在 2026-07-13~2026-08-11
（原樣本，偏多趨勢月）找到的訊號，換一個不同市場狀態的區間還站不站得住。

沿用 fetch_expert_pool_futures_tick_recent_month.py 同一套 STOCK_FUTURES 對照表
（2026-08-11 驗證過），與同一套近月合約篩選邏輯。

用法：
  PYTHONPATH=src .venv/bin/python \
    scripts/research/fetch_expert_pool_futures_tick_oos_period.py
"""

from __future__ import annotations

import csv
import time
from datetime import datetime
from pathlib import Path

from fetch_expert_pool_futures_tick_recent_month import (
    STOCK_FUTURES,
    fetch_day_ticks,
    near_month_contract,
)

SLEEP = 0.3
OUT_DIR = Path(__file__).resolve().parents[2] / "reports/research/expert_pool_futures_tick"

# 2025-10-20 ~ 2025-11-24：0050 從 64.75 拉回到 59.85（~-7.6%），跟原樣本
# （2026-07-13~2026-08-11 偏多趨勢月）明顯不同的市場狀態，供 out-of-sample 對照。
DAYS = [
    "2025-10-20", "2025-10-21", "2025-10-22", "2025-10-23", "2025-10-27",
    "2025-10-28", "2025-10-29", "2025-10-30", "2025-10-31", "2025-11-03",
    "2025-11-04", "2025-11-05", "2025-11-06", "2025-11-07", "2025-11-10",
    "2025-11-11", "2025-11-12", "2025-11-13", "2025-11-14", "2025-11-17",
    "2025-11-18", "2025-11-19", "2025-11-20", "2025-11-21", "2025-11-24",
]


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> int:
    log(f"OOS 期間 {len(DAYS)} 天：{DAYS[0]} ~ {DAYS[-1]} · 標的 {len(STOCK_FUTURES)} 檔")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    summary = []
    for i, (sid, fid) in enumerate(STOCK_FUTURES.items(), 1):
        log(f"[{i}/{len(STOCK_FUTURES)}] {sid} ({fid}) 抓取中...")
        all_ticks = []
        contract_by_day = {}
        for day in DAYS:
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
            log(f"  {sid}: 無資料，跳過")
            summary.append((sid, fid, 0, ""))
            continue
        out_path = OUT_DIR / f"{sid}_{fid}_tick_{DAYS[0]}_{DAYS[-1]}.csv"
        with out_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["date", "futures_id", "contract_date", "price", "volume"])
            w.writeheader()
            w.writerows(all_ticks)
        contracts = "/".join(sorted(set(contract_by_day.values())))
        log(f"  {sid}: {len(all_ticks)} 筆 -> {out_path.name}（合約：{contracts}）")
        summary.append((sid, fid, len(all_ticks), contracts))

    log("=== 總結 ===")
    total = 0
    for sid, fid, n, contracts in summary:
        total += n
        log(f"  {sid} {fid}: {n} 筆 · {contracts}")
    log(f"合計 {total} 筆逐筆成交 · {sum(1 for _, _, n, _ in summary if n > 0)}/{len(summary)} 檔有資料")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
