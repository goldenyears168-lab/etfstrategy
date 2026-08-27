#!/usr/bin/env python3
"""dayflip-futures-short v1 · 增量更新 futures_daily_cache.json（20日ADV流動性濾網用）.

2026-08-08 code review 發現：這份快取最新資料停在 2026-07-20，且
`dayflip_short_signal.build_candidates()` 對「當天不在快取裡」是 fail-open
（直接跳過ADV檢查讓候選通過），代表流動性濾網已經靜默失效近3週。原本的
build_stock_futures_liquidity_universe.py 是一次性建置腳本（`if sid in cache
and cache[sid]: continue` 會跳過已有資料的標的，抓取範圍也寫死到 2026-07-20），
不適合當日常增量更新用；這支腳本只做「從每檔標的目前快取的最後一天，補到
today」的增量抓取，維持原本的資料格式（[open, close, max, min, volume]）。

⚠️ 2026-08-12修正：原本補到「today-1」，是為了某個「隔天一早跑、當天資料
還沒公布」的排程情境設計的，但2026-08-10把這支腳本併進daily-sync job
（週一至五16:35執行，已收盤超過2小時，FinMind當天資料早就有了）之後，
「today-1」變成永遠補不進「今天自己」——2026-08-11 16:35那次自動執行親眼
證實：跑完回報「0檔更新、83檔已是最新」，但實際查快取最新只到08-10，
08-11完全沒補進去，隔天(08-12)08:45候選判斷會重演跟08-10同一晚一樣的
fail-closed排除全部候選的問題。改成補到「today」而非「today-1」，因為
現在的排程時間點(16:35，收盤後)已經確保today當天的FinMind資料存在。

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/refresh_dayflip_futures_daily_cache.py

排程：daily-sync job（週一至五16:35，見scripts/launchd/daily-sync.command）。
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from finmind_client import fetch_finmind  # noqa: E402

OUT_DIR = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short"
CACHE = OUT_DIR / "futures_daily_cache.json"
UNIVERSE = OUT_DIR / "stock_futures_universe.json"


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> None:
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    futmap = json.loads(UNIVERSE.read_text())["map"]
    today = date.today()
    # 2026-08-12修正：改補到today（不是today-1）——見上方docstring，這支腳本
    # 現在跑在收盤後2小時的16:35排程，today當天FinMind資料已經有了。
    end = today

    updated = 0
    skipped_no_gap = 0
    for sid, code in sorted(futmap.items()):
        if sid not in cache or not cache[sid]:
            continue  # 這支只補現有快取，不新建標的（新建走 build_stock_futures_liquidity_universe.py）
        existing = cache[sid]
        last_date = max(existing) if existing else None
        start = (date.fromisoformat(last_date) + timedelta(days=1)) if last_date else date(2024, 7, 1)
        if start > end:
            skipped_no_gap += 1
            continue

        fid = code if len(code) == 2 else code
        fid = fid + "F" if len(fid) == 2 else fid
        tried = [fid]
        if len(code) > 2 and not code.endswith("F"):
            tried.append(code[:-1] + "F")

        rows = None
        for cand_fid in tried:
            try:
                rows = fetch_finmind("TaiwanFuturesDaily", cand_fid, start, end, timeout=120)
            except Exception as ex:  # noqa: BLE001
                log(f"  {sid} {cand_fid} ERR {str(ex)[:60]}")
                continue
            if rows:
                break
        if not rows:
            continue

        byd: dict[str, list] = defaultdict(list)
        for r in rows:
            cd = str(r.get("contract_date", ""))
            if "/" in cd or r.get("trading_session") != "position":
                continue
            if float(r.get("open") or 0) <= 0:
                continue
            byd[str(r["date"])].append(r)
        new_days = 0
        for d, rs in byd.items():
            if d in existing:
                continue
            near = max(rs, key=lambda x: float(x.get("volume") or 0))
            tot = sum(float(x.get("volume") or 0) for x in rs)
            existing[d] = [float(near["open"]), float(near["close"]),
                          float(near.get("max") or 0), float(near.get("min") or 0), tot]
            new_days += 1
        if new_days:
            updated += 1
            log(f"  {sid}: +{new_days} 天 (last={max(existing)})")
        time.sleep(0.2)

    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    log(f"完成：{updated} 檔更新、{skipped_no_gap} 檔已是最新 → {CACHE}")


if __name__ == "__main__":
    main()
