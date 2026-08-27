#!/usr/bin/env python3
"""抓 227 檔候選期貨標的的日線（近月），供 branch_dayflip_reclassify 的跳空回測用。

沿用 run_dayflip_forward_test.py 的近月挑選邏輯（session='position'、非跨月、
open>0、同日多檔取成交量最大者）。輸出：
  reports/research/branch_dayflip_reclassify/futures_panel.json
  格式：{stock_id: {date: [open, close, min, max, volume]}}
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, "/Users/jackm4/goldenstocks/src")
from finmind_client import fetch_finmind  # noqa: E402

OUT_DIR = Path("/Users/jackm4/goldenstocks/reports/research/branch_dayflip_reclassify")
PANEL_PATH = OUT_DIR / "futures_panel.json"
UNIVERSE_PATH = Path(
    "/Users/jackm4/goldenstocks/reports/research/branch-footprint-screen/"
    "dayflip_gapup_short/stock_futures_universe.json"
)
MEGA_PATH = Path(
    "/Users/jackm4/goldenstocks/reports/research/branch-footprint-screen/"
    "ab58_xMega_copytrade/mega_blacklist_v1.json"
)
NEED_STOCKS_PATH = Path("/tmp/need_futures_stocks.json")

START = date(2024, 7, 1)
END = date(2026, 8, 7)


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> None:
    futmap = json.loads(UNIVERSE_PATH.read_text())["map"]
    need = json.loads(NEED_STOCKS_PATH.read_text())
    log(f"stocks to fetch: {len(need)}")

    panel: dict[str, dict[str, list]] = {}
    if PANEL_PATH.exists():
        panel = json.loads(PANEL_PATH.read_text())
        log(f"resuming, already have {len(panel)} stocks cached")

    for i, sid in enumerate(need):
        if sid in panel:
            continue
        code = futmap.get(sid)
        if not code:
            continue
        fid = code + "F" if len(code) == 2 else code
        try:
            rows = fetch_finmind("TaiwanFuturesDaily", fid, START, END, timeout=120)
        except Exception as ex:  # noqa: BLE001
            log(f"  {sid} ({fid}) ERR {str(ex)[:60]}")
            continue
        byd = defaultdict(list)
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
            out[d] = [
                float(near["open"]), float(near["close"]),
                float(near.get("min") or 0), float(near.get("max") or 0),
                sum(float(x.get("volume") or 0) for x in rs),
            ]
        panel[sid] = out
        if i % 20 == 0:
            log(f"  [{i+1}/{len(need)}] {sid} ({fid}) -> {len(out)} days")
            PANEL_PATH.write_text(json.dumps(panel))
        time.sleep(0.2)

    PANEL_PATH.write_text(json.dumps(panel))
    log(f"DONE: {len(panel)} stocks cached -> {PANEL_PATH}")


if __name__ == "__main__":
    main()
