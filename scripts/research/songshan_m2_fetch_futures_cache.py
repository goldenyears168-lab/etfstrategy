#!/usr/bin/env python3
"""songshan_m2 · 抓 9217 母體標的的個股期貨日線（FinMind TaiwanFuturesDaily）.

沿用 run_dayflip_futures_0845_entry.load_futures 的口徑：
  - 只取 trading_session == 'position'（日盤），排除價差單（contract_date 含 '/'）
  - 每日「近月」= 該日成交量最大的契約（與 dayflip 線一致）
  - 額外記錄：該日所有非價差契約的總量（近月+次月+… 的 upper bound）
    以及依 contract_date 排序後的前兩個契約量合計（近月+次月，對齊 FROZEN_SPEC）

不覆蓋 dayflip 線的 futures_daily_cache.json，寫自己的快取。
輸出：reports/research/branch-footprint-screen/songshan_m2/futures_daily_cache.json
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from finmind_client import fetch_finmind  # noqa: E402

BASE = ROOT / "reports" / "research" / "branch-footprint-screen"
OUT_DIR = BASE / "songshan_m2"
CACHE = OUT_DIR / "futures_daily_cache.json"
FUTMAP = BASE / "dayflip_gapup_short" / "stock_futures_universe.json"
TRADES = OUT_DIR / "mother_set_trades.csv"

START = date(2024, 5, 1)
END = date(2026, 8, 17)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    futmap = json.loads(FUTMAP.read_text())["map"]
    sids = sorted(pd.read_csv(TRADES, dtype={"stock_id": str})["stock_id"].unique())
    targets = [s for s in sids if s in futmap]
    print(f"[INFO] 母體 {len(sids)} 檔，其中在期貨宇宙 {len(targets)} 檔")

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    for sid in targets:
        if sid in cache:
            continue
        code = futmap[sid]
        fid = code + "F" if len(code) == 2 else code
        try:
            rows = fetch_finmind("TaiwanFuturesDaily", fid, START, END, timeout=240)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sid} {fid} ERR {str(exc)[:80]}")
            cache[sid] = {}
            continue
        byd: dict[str, list] = defaultdict(list)
        for r in rows:
            cd = str(r.get("contract_date", ""))
            if "/" in cd or r.get("trading_session") != "position":
                continue
            if float(r.get("open") or 0) <= 0:
                continue
            byd[str(r["date"])].append(r)
        out = {}
        for d, rs in byd.items():
            near = max(rs, key=lambda x: float(x.get("volume") or 0))
            by_cd = sorted(rs, key=lambda x: str(x.get("contract_date")))
            front2 = sum(float(x.get("volume") or 0) for x in by_cd[:2])
            total = sum(float(x.get("volume") or 0) for x in rs)
            out[d] = {
                "o": float(near["open"]),
                "c": float(near["close"]),
                "h": float(near.get("max") or 0),
                "l": float(near.get("min") or 0),
                "v_near": float(near.get("volume") or 0),
                "v_front2": front2,
                "v_all": total,
                "cd": str(near.get("contract_date")),
                "n_contracts": len(rs),
            }
        cache[sid] = out
        days = sorted(out)
        print(f"  {sid} {fid}: {len(out)} 日 {days[0] if days else '-'}~{days[-1] if days else '-'}")
        CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        time.sleep(0.4)

    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    print(f"[OK] {CACHE} · {len(cache)} 檔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
