"""Fetch ETF 受益人數(holders) + 在外單位數(SHOUT) 週頻面板 → parquet.

單一 dataset TaiwanStockHoldingSharesPer 的 `total` 列同時給:
  people = 總受益人數 (S3 holders)
  unit   = 總在外受益權單位(股)數 = SHOUT (S1 flow 的分母/分子)
=> S1(申贖 flow)與 S3(受益人數)皆可從此週頻資料建, 無需 SITCA/投信爬蟲。
S2(折溢價)需官方 NAV, FinMind 無 dataset(已查證 422 enum), 留待補。

主要 ETF 宇宙(市值型 + 高股息型, 涵蓋 2023-26 散戶擁擠故事)。
每檔 1 次 API 呼叫(全區間), 迴圈 sleep 0.3s。
"""
from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from finmind_client import fetch_finmind  # noqa: E402

OUT = ROOT / "data" / "research" / "dashboard" / "etf_flows_data.parquet"
OUT.parent.mkdir(parents=True, exist_ok=True)

UNIVERSE = {
    "0050": "元大台灣50 (市值)",
    "006208": "富邦台50 (市值)",
    "0056": "元大高股息 (高息)",
    "00878": "國泰永續高股息 (高息)",
    "00713": "元大高息低波 (高息)",
    "00919": "群益台灣精選高息 (高息)",
    "00929": "復華台灣科技優息 (高息月配)",
    "00940": "元大台灣價值高息 (2024 mega-IPO)",
}
START = date(2018, 1, 1)
END = date(2026, 7, 29)


def main() -> None:
    frames = []
    for code in UNIVERSE:
        raw = fetch_finmind("TaiwanStockHoldingSharesPer", code, START, END, timeout=120)
        df = pd.DataFrame(raw)
        if df.empty:
            print(f"  {code}: EMPTY")
            continue
        tot = df[df["HoldingSharesLevel"] == "total"][["date", "people", "unit"]].copy()
        tot["etf_code"] = code
        tot = tot.rename(columns={"people": "holders", "unit": "shout"})
        frames.append(tot)
        print(f"  {code} {UNIVERSE[code]}: {len(tot)} wk  {tot['date'].min()}..{tot['date'].max()}")
        time.sleep(0.3)
    panel = pd.concat(frames, ignore_index=True)
    panel["date"] = panel["date"].astype(str).str[:10]
    panel["holders"] = pd.to_numeric(panel["holders"], errors="coerce")
    panel["shout"] = pd.to_numeric(panel["shout"], errors="coerce")
    panel = panel.sort_values(["etf_code", "date"]).reset_index(drop=True)
    panel.to_parquet(OUT, index=False)
    print(f"\nSAVED {OUT}  rows={len(panel)}  etfs={panel['etf_code'].nunique()}")


if __name__ == "__main__":
    main()
