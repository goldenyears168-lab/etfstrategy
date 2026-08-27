#!/usr/bin/env python3
"""下載分點專家池全部個股期貨近一個月逐筆成交（每檔一個 CSV）。

沿用 fetch_ix_futures_tick_recent_month.py 的邏輯：FinMind TaiwanFuturesTick
一次只能抓一天，逐日抓取後只留當天成交量最大的「近月」合約（排除跨月
價差單），原始逐筆直接寫出。

STOCK_FUTURES 對照表是 2026-08-11 用 order.dayflip_short_signal.resolve_futures_symbol()
逐檔驗證過『當天確實有資料』的結果（44 檔 POOLS 中有 14 檔查無現行有效個股期貨，
不在此表：1815/2351/3090/3167/3491/4966/5289/6442/6446/6515/6531/6805/8033/8210）。

用法：
  PYTHONPATH=src .venv/bin/python \
    scripts/research/fetch_expert_pool_futures_tick_recent_month.py
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

# stock_id -> FinMind futures_id（2026-08-11 驗證）
STOCK_FUTURES: dict[str, str] = {
    "1802": "KUF",
    "2059": "FGF",
    "2303": "CCF",
    "2308": "FRF",
    "2317": "DHF",
    "2327": "LXF",
    "2337": "DIF",
    "2344": "FZF",
    "2367": "VBF",
    "2383": "PJF",
    "2408": "CYF",
    "3037": "IRF",
    "3081": "OTF",
    "3105": "NAF",
    "3189": "IXF",
    "3443": "JBF",
    "3481": "DQF",
    "3653": "JMF",
    "3665": "VEF",
    "3711": "OZF",
    "4958": "LUF",
    "6147": "NSF",
    "6213": "KBF",
    "6223": "UVF",
    "6239": "KCF",
    "6274": "OVF",
    "6510": "OXF",
    "6669": "PVF",
    "8046": "LYF",
    "8358": "PQF",
}

N_DAYS = 22
SLEEP = 0.3
OUT_DIR = Path(__file__).resolve().parents[2] / "reports/research/expert_pool_futures_tick"


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


def fetch_day_ticks(futures_id: str, day: str) -> list[dict]:
    payload = fetch_finmind_json(
        {"dataset": "TaiwanFuturesTick", "data_id": futures_id, "start_date": day},
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


def fetch_stock_ticks(futures_id: str, days: list[str]) -> tuple[list[dict], dict[str, str]]:
    all_ticks: list[dict] = []
    contract_by_day: dict[str, str] = {}
    for day in days:
        try:
            rows = fetch_day_ticks(futures_id, day)
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
    return all_ticks, contract_by_day


def main() -> int:
    days = trading_days(N_DAYS)
    log(f"目標交易日 {len(days)} 天：{days[0]} ~ {days[-1]} · 標的 {len(STOCK_FUTURES)} 檔")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    summary = []
    for i, (sid, fid) in enumerate(STOCK_FUTURES.items(), 1):
        log(f"[{i}/{len(STOCK_FUTURES)}] {sid} ({fid}) 抓取中...")
        ticks, contract_by_day = fetch_stock_ticks(fid, days)
        if not ticks:
            log(f"  {sid}: 無資料，跳過")
            summary.append((sid, fid, 0, ""))
            continue
        out_path = OUT_DIR / f"{sid}_{fid}_tick_{days[0]}_{days[-1]}.csv"
        with out_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["date", "futures_id", "contract_date", "price", "volume"])
            w.writeheader()
            w.writerows(ticks)
        contracts = "/".join(sorted(set(contract_by_day.values())))
        log(f"  {sid}: {len(ticks)} 筆 -> {out_path.name}（合約：{contracts}）")
        summary.append((sid, fid, len(ticks), contracts))

    log("=== 總結 ===")
    total = 0
    for sid, fid, n, contracts in summary:
        total += n
        log(f"  {sid} {fid}: {n} 筆 · {contracts}")
    log(f"合計 {total} 筆逐筆成交 · {sum(1 for _, _, n, _ in summary if n > 0)}/{len(summary)} 檔有資料")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
