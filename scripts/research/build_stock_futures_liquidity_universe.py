#!/usr/bin/env python3
"""建立個股期貨流動性宇宙 + 抓取歷史日線（research only）.

兩段式，避免 381 次全史呼叫：
  1. 先用「不指定 data_id」的 TaiwanFuturesDaily 抓近 N 個交易日的全契約列，
     算出每個 futures_id 的近期日均量 → 粗篩（門檻放寬到 2,000 口，避免倖存者偏誤）
  2. 只對通過粗篩者抓 2024-07~2026-07 全史，寫入快取供後續 PIT 過濾使用

  PYTHONPATH=src .venv/bin/python scripts/research/build_stock_futures_liquidity_universe.py
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import mean

from finmind_client import fetch_finmind, fetch_finmind_dataset

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short"
UNIVERSE = OUT / "stock_futures_universe.json"
CACHE = OUT / "futures_daily_cache.json"
SCREEN = OUT / "futures_liquidity_screen.json"

RECENT_DATES = ["2026-07-08", "2026-07-07", "2026-07-06", "2026-07-03", "2026-07-02",
                "2026-07-01", "2026-06-30", "2026-06-27", "2026-06-26", "2026-06-25"]
PRESCREEN_LOTS = 500


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> None:
    futmap = json.loads(UNIVERSE.read_text())["map"]  # stock_id -> 2碼商品代號
    fid_of = {sid: (c + "F" if len(c) == 2 else c) for sid, c in futmap.items()}
    inv = {v: k for k, v in fid_of.items()}

    log("步驟1：近期全契約成交量 ...")
    vol: dict[str, list[float]] = defaultdict(list)
    for d in RECENT_DATES:
        try:
            rows = fetch_finmind_dataset("TaiwanFuturesDaily",
                                         start=date.fromisoformat(d),
                                         end=date.fromisoformat(d), timeout=150)
        except Exception as ex:  # noqa: BLE001
            log(f"  {d} ERR {str(ex)[:60]}")
            continue
        agg: dict[str, float] = defaultdict(float)
        for r in rows:
            cd = str(r.get("contract_date", ""))
            if "/" in cd or r.get("trading_session") != "position":
                continue
            agg[str(r["futures_id"])] += float(r.get("volume") or 0)
        for k, v in agg.items():
            vol[k].append(v)
        log(f"  {d}: {len(agg)} 契約")
        time.sleep(0.3)

    recent = {k: mean(v) for k, v in vol.items() if v}
    cands = sorted(
        [(inv[k], k, v) for k, v in recent.items() if k in inv and v >= PRESCREEN_LOTS],
        key=lambda x: -x[2])
    log(f"粗篩 >= {PRESCREEN_LOTS} 口：{len(cands)} 檔 / 對應標的 {len(inv)} 檔")
    SCREEN.write_text(json.dumps(
        {"as_of": RECENT_DATES[0], "prescreen_lots": PRESCREEN_LOTS,
         "recent_avg_lots": {inv[k]: round(v) for k, v in recent.items() if k in inv},
         "candidates": [{"stock_id": s, "futures_id": f, "recent_avg_lots": round(v)}
                        for s, f, v in cands]},
        ensure_ascii=False, indent=1), encoding="utf-8")

    log("步驟2：抓候選標的全史 ...")
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    done = 0
    for sid, fid, _ in cands:
        if sid in cache and cache[sid]:
            continue
        try:
            rows = fetch_finmind("TaiwanFuturesDaily", fid,
                                 date(2024, 7, 1), date(2026, 7, 20), timeout=180)
        except Exception as ex:  # noqa: BLE001
            log(f"  {sid} {fid} ERR {str(ex)[:60]}")
            cache[sid] = {}
            CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
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
            tot = sum(float(x.get("volume") or 0) for x in rs)
            out[d] = [float(near["open"]), float(near["close"]),
                      float(near.get("max") or 0), float(near.get("min") or 0), tot]
        cache[sid] = out
        done += 1
        if done % 10 == 0:
            CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            log(f"  已抓 {done}/{len(cands)}")
        time.sleep(0.3)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    log(f"完成：快取 {len(cache)} 檔 → {CACHE}")


if __name__ == "__main__":
    main()
