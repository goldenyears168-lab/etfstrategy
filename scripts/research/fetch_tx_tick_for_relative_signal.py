#!/usr/bin/env python3
"""下載台指期(TX)逐筆成交（IS+OOS共47天），供個股相對大盤同步超額動能訊號用.

用法：
  PYTHONPATH=src .venv/bin/python \
    scripts/research/fetch_tx_tick_for_relative_signal.py
"""

from __future__ import annotations

import csv
import json
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from finmind_client import fetch_finmind_json

SLEEP = 0.3
OUT_DIR = Path(__file__).resolve().parents[2] / "reports/research/expert_pool_futures_tick"


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


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
    days = json.loads(Path("/tmp/all_days.json").read_text())
    log(f"TX 逐筆下載 {len(days)} 天")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for d in days:
        payload = fetch_finmind_json(
            {"dataset": "TaiwanFuturesTick", "data_id": "TX", "start_date": d}, timeout=60
        )
        rows = payload.get("data") or []
        contract = near_month_contract(rows)
        kept = [r for r in rows if str(r.get("contract_date", "")) == contract] if contract else []
        kept.sort(key=lambda r: r["date"])
        out_path = OUT_DIR / f"tx_market_TX_tick_{d}.csv"
        with out_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["date", "futures_id", "contract_date", "price", "volume"])
            w.writeheader()
            w.writerows(kept)
        log(f"  {d}: {len(kept)} 筆（合約 {contract}）")
        time.sleep(SLEEP)

    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
