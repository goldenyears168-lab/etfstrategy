#!/usr/bin/env python3
"""Stage 1 · branch-footprint-screen 擴大版 master builder（買賣雙側 · 全 universe · 股數版）.

盲點修正（相對舊 study_whale_branch_l1h7_universe.py 與本檔第一版）：
  * 資料源 = REPLICA（data/replica/stocks.db）完整快照：2024-07→2026-06 ~548 家、
    2026-07 起 1010 家、finmind。本地 data/stocks.db 正在回填、2024–25 只有一半分點。
  * 不 join 價格：同一 (trade_date, stock_id) 內所有分點乘同一收盤價，排名與各占比
    （top1_share/top12_share/top3_share）皆 price-invariant，故 Top1/2/3 與集中度用
    「股數」直接算即可，涵蓋全部 2300+ 檔、不被價格覆蓋（~370 檔）截斷。
    絕對金額門檻與 forward return 需要價格，延到 screen 階段處理。
  * 買賣雙側；月度分塊、可斷點續跑；快照鉤子寫進 manifest。

輸出：reports/research/branch-footprint-screen/expanded/master_parts/YYYY-MM.parquet
  每列 = 一個 (trade_date, stock_id, side)：total_shares / n_branches / top1..3 id+shares。

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/build_branch_footprint_master_expanded.py
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
REPLICA_DB = ROOT / "data/replica/stocks.db"
OUT_DIR = ROOT / "reports/research/branch-footprint-screen/expanded"
PARTS_DIR = OUT_DIR / "master_parts"
DATE_START = "2024-07-01"
DATE_END = "2026-07-22"


def month_ranges(start: str, end: str) -> list[tuple[str, str, str]]:
    out = []
    y, m = int(start[:4]), int(start[5:7])
    while True:
        lo = f"{y:04d}-{m:02d}-01"
        if lo > end:
            break
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        out.append((f"{y:04d}-{m:02d}", lo, f"{ny:04d}-{nm:02d}-01"))
        y, m = ny, nm
    return out


def build_side(df: pd.DataFrame, sh_col: str, side: str) -> pd.DataFrame:
    """向量化 top1/2/3 + total + n_branches，依股數（比照 scan_whale_*_concentration 的排名邏輯）。"""
    sub = df[df[sh_col] > 0][["trade_date", "stock_id", "securities_trader_id", sh_col]].copy()
    if sub.empty:
        return pd.DataFrame()
    gcols = ["trade_date", "stock_id"]
    sub["rank"] = sub.groupby(gcols)[sh_col].rank(method="first", ascending=False)
    meta = sub.groupby(gcols)[sh_col].agg(total_shares="sum", n_branches="size").reset_index()
    top = sub[sub["rank"] <= 3].pivot_table(
        index=gcols, columns="rank", values=[sh_col, "securities_trader_id"], aggfunc="first"
    )
    top.columns = [f"{a}_{int(b)}" for a, b in top.columns]
    top = top.reset_index()
    out = meta.merge(top, on=gcols, how="left")
    ren = {
        f"{sh_col}_1": "top1_sh", f"{sh_col}_2": "top2_sh", f"{sh_col}_3": "top3_sh",
        "securities_trader_id_1": "top1_id", "securities_trader_id_2": "top2_id",
        "securities_trader_id_3": "top3_id",
    }
    out = out.rename(columns=ren)
    for c in ("top1_sh", "top2_sh", "top3_sh"):
        if c not in out:
            out[c] = 0.0
        out[c] = out[c].fillna(0.0)
    for c in ("top1_id", "top2_id", "top3_id"):
        if c not in out:
            out[c] = None
    out["side"] = side
    return out[["trade_date", "stock_id", "side", "total_shares", "n_branches",
                "top1_id", "top1_sh", "top2_id", "top2_sh", "top3_id", "top3_sh"]]


def main() -> int:
    PARTS_DIR.mkdir(parents=True, exist_ok=True)
    assert REPLICA_DB.exists(), f"missing {REPLICA_DB}"
    conn = sqlite3.connect(f"file:{REPLICA_DB}?mode=ro", uri=True)
    snap = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(REPLICA_DB.stat().st_mtime))
    data_max = conn.execute("SELECT MAX(trade_date) FROM stock_broker_branch_daily").fetchone()[0]
    manifest = {"source_db": str(REPLICA_DB), "snapshot_file_mtime": snap,
                "data_max_trade_date": data_max, "window": [DATE_START, DATE_END],
                "basis": "shares (price-invariant ranking)", "built_parts": []}
    print(f"[SNAPSHOT] replica mtime={snap} data_max={data_max}")

    for tag, lo, hi in month_ranges(DATE_START, DATE_END):
        part = PARTS_DIR / f"{tag}.parquet"
        if part.exists():
            print(f"[SKIP] {tag}"); manifest["built_parts"].append(tag); continue
        t0 = time.time()
        br = pd.read_sql_query(
            """SELECT trade_date, stock_id, securities_trader_id, buy, sell
               FROM stock_broker_branch_daily
               WHERE source='finmind' AND trade_date>=? AND trade_date<? AND trade_date<=?
                 AND (buy>0 OR sell>0)""",
            conn, params=(lo, hi, DATE_END))
        if br.empty:
            print(f"[EMPTY] {tag}"); continue
        parts = [build_side(br, "buy", "buy"), build_side(br, "sell", "sell")]
        out = pd.concat([p for p in parts if not p.empty], ignore_index=True)
        out["stock_id"] = out["stock_id"].astype(str)
        out.to_parquet(part, index=False)
        manifest["built_parts"].append(tag)
        print(f"[OK] {tag} br_rows={len(br):,} out_rows={len(out):,} ({time.time()-t0:.0f}s)")

    (OUT_DIR / "master_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    conn.close()
    print(f"[DONE] parts={len(manifest['built_parts'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
