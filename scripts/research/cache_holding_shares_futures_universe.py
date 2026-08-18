#!/usr/bin/env python3
"""抓個股期貨宇宙（251 檔）的集保股權分散表，並保留多個大戶級距門檻。

為什麼要多門檻：既有快取 `holding_shares_per_expert.csv` 把「大戶」定義成 100 張以上
（HoldingSharesLevel >= 100,001 股），但看盤軟體常用的是 200 張／400 張／1000 張。
門檻選哪個本身就是待驗證的問題，所以一次抓齊、之後才不用重抓。

PIT 註記：集保是**週資料**（每週最後營業日），下週一才可得。任何前瞻報酬必須從
資料可得日之後起算——本腳本只負責落檔，PIT 由消費端負責。

Out: reports/research/chip-overlays/cache/holding_shares_per_futures_universe.csv
     欄位 sid,d,pct_100,pct_200,pct_400,pct_1000,pct_retail_10,pct_retail_50
"""
from __future__ import annotations
import sys, time, json
from collections import defaultdict
from datetime import date
from pathlib import Path
import pandas as pd
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT / "src"))
from finmind_client import fetch_finmind, finmind_token  # noqa: E402

OUT = ROOT / "reports/research/chip-overlays/cache"
FUT = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/stock_futures_universe.json"
START, END = date(2024, 6, 1), date(2026, 8, 18)
DELAY = 0.35

# FinMind HoldingSharesLevel 字串 → 該級距下界（張）
LEVEL_LOT = {
    "1-999": 0, "1,000-5,000": 1, "5,001-10,000": 5, "10,001-15,000": 10,
    "15,001-20,000": 15, "20,001-30,000": 20, "30,001-40,000": 30,
    "40,001-50,000": 40, "50,001-100,000": 50, "100,001-200,000": 100,
    "200,001-400,000": 200, "400,001-600,000": 400, "600,001-800,000": 600,
    "800,001-1,000,000": 800, "more than 1,000,001": 1000,
}

def main() -> int:
    if not finmind_token():
        print("ERROR: FINMIND_TOKEN unset", file=sys.stderr); return 2
    OUT.mkdir(parents=True, exist_ok=True)
    ids = sorted(json.loads(FUT.read_text(encoding="utf-8"))["map"].keys())
    print(f"目標 {len(ids)} 檔（個股期貨宇宙）· {START} ~ {END}")
    rows, unknown = [], set()
    for i, sid in enumerate(ids, 1):
        try:
            raw = fetch_finmind("TaiwanStockHoldingSharesPer", sid, START, END, timeout=120)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i}/{len(ids)}] {sid} FAIL {exc}"); time.sleep(DELAY); continue
        by_d = defaultdict(list)
        for it in raw:
            d = str(it.get("date") or "")[:10]
            if d: by_d[d].append(it)
        for d in sorted(by_d):
            acc = dict(pct_100=0.0, pct_200=0.0, pct_400=0.0, pct_1000=0.0,
                       pct_retail_10=0.0, pct_retail_50=0.0)
            for it in by_d[d]:
                lvl = str(it.get("HoldingSharesLevel") or "")
                if lvl in ("total", "合計"): continue
                lot = LEVEL_LOT.get(lvl)
                if lot is None: unknown.add(lvl); continue
                try: pct = float(it.get("percent") or 0)
                except (TypeError, ValueError): continue
                if lot >= 100: acc["pct_100"] += pct
                if lot >= 200: acc["pct_200"] += pct
                if lot >= 400: acc["pct_400"] += pct
                if lot >= 1000: acc["pct_1000"] += pct
                if lot < 10: acc["pct_retail_10"] += pct
                if lot < 50: acc["pct_retail_50"] += pct
            rows.append(dict(sid=sid, d=d, **{k: round(v, 4) for k, v in acc.items()}))
        if i % 25 == 0: print(f"  [{i}/{len(ids)}] 累積 {len(rows):,} 列")
        time.sleep(DELAY)
    if unknown: print("⚠ 未知級距字串（已略過）:", sorted(unknown))
    df = pd.DataFrame(rows).sort_values(["sid", "d"])
    for col in ("pct_100", "pct_200", "pct_400", "pct_1000", "pct_retail_10"):
        df[f"{col}_chg"] = df.groupby("sid")[col].diff()
    p = OUT / "holding_shares_per_futures_universe.csv"
    df.to_csv(p, index=False)
    print(f"\n完成：{len(df):,} 列 · {df.sid.nunique()} 檔 · {df.d.min()} ~ {df.d.max()}\n→ {p}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
