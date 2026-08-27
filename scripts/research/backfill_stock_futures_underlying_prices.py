#!/usr/bin/env python3
"""補齊個股期貨標的的日線（stock_daily_bars）.

背景：stock_daily_bars 的 finmind 來源只涵蓋約 676 檔、每日 174~375 檔，
355 檔非權值股期貨標的中只有 229 檔有價格，2024H2 更只有 106 檔。
訊號第一步「買進金額 = 買進股數 × 收盤價」會把沒有價格的股票靜默丟棄，
造成研究宇宙被砍掉約 2/3、且 2024 vs 2026 的覆蓋率不可比。

寫入生產 stock_daily_bars（INSERT OR IGNORE，不覆蓋既有列）。
量級約 355 檔 × 500 日 ≈ 18 萬列，遠小於該表既有規模。

  PYTHONPATH=src .venv/bin/python \
    scripts/research/backfill_stock_futures_underlying_prices.py \
    --start 2024-06-01 --end 2026-08-06
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

import stock_db
from finmind_client import fetch_finmind

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "reports/research/branch-footprint-screen"
OUT = BASE / "dayflip_gapup_short"
SLEEP = 0.3


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-06-01")
    ap.add_argument("--end", default="2026-08-06")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--scope", choices=("necessary", "all"), default="necessary",
                    help="necessary=daily_collect_universe.json（73 檔，日常增量用）；"
                         "all=全部個股期貨標的（355 檔，僅補歷史時用）")
    args = ap.parse_args()

    if args.scope == "necessary":
        targets = json.loads((OUT / "daily_collect_universe.json").read_text())["stocks"]
    else:
        futmap = json.loads((OUT / "stock_futures_universe.json").read_text())["map"]
        mega = set(json.loads((BASE / "ab58_xMega_copytrade/mega_blacklist_v1.json")
                              .read_text())["symbols"])
        targets = sorted(s for s in futmap if s not in mega and not s.startswith("00"))
    log(f"scope={args.scope} · 目標標的 {len(targets)} 檔 · 期間 {args.start}~{args.end}")

    con = sqlite3.connect(stock_db.DEFAULT_DB_PATH, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    before = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT stock_id) FROM stock_daily_bars "
        "WHERE source='finmind' AND trade_date BETWEEN ? AND ?",
        (args.start, args.end)).fetchone()
    log(f"補之前：{before[0]:,} 列 / {before[1]} 檔")

    now = datetime.now().isoformat(timespec="seconds")
    ins = skipped = delisted = 0
    t0 = time.time()
    for i, sid in enumerate(targets, 1):
        try:
            rows = fetch_finmind("TaiwanStockPrice", sid,
                                 date.fromisoformat(args.start),
                                 date.fromisoformat(args.end), timeout=150)
        except Exception as ex:  # noqa: BLE001
            log(f"  {sid} ERR {str(ex)[:70]}")
            skipped += 1
            time.sleep(SLEEP)
            continue
        if not rows:
            delisted += 1
            time.sleep(SLEEP)
            continue
        recs = []
        for r in rows:
            c = float(r.get("close") or 0)
            if c <= 0:
                continue
            recs.append((sid, str(r.get("date")), float(r.get("open") or 0),
                         float(r.get("max") or 0), float(r.get("min") or 0), c,
                         int(r.get("Trading_Volume") or 0), "finmind", now,
                         None, float(r.get("Trading_money") or 0) or None))
        if not args.dry_run and recs:
            cur = con.executemany(
                "INSERT OR IGNORE INTO stock_daily_bars "
                "(stock_id, trade_date, open, high, low, close, volume, source, "
                " synced_at, adj_close, amount) VALUES (?,?,?,?,?,?,?,?,?,?,?)", recs)
            ins += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        if i % 25 == 0:
            if not args.dry_run:
                con.commit()
            el = time.time() - t0
            log(f"  {i}/{len(targets)} · 新增 {ins:,} 列 · 無資料 {delisted} · "
                f"錯誤 {skipped} · ETA {el/i*(len(targets)-i)/60:.1f} 分")
        time.sleep(SLEEP)
    if not args.dry_run:
        con.commit()
    after = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT stock_id) FROM stock_daily_bars "
        "WHERE source='finmind' AND trade_date BETWEEN ? AND ?",
        (args.start, args.end)).fetchone()
    log(f"補之後：{after[0]:,} 列 / {after[1]} 檔 "
        f"(+{after[0]-before[0]:,} 列 / +{after[1]-before[1]} 檔)")
    log(f"無資料(疑似下市) {delisted} 檔 · 錯誤 {skipped} 檔 · "
        f"耗時 {(time.time()-t0)/60:.1f} 分")
    print("\n=== 補後每半年覆蓋（目標標的中有價格者）===")
    q = ",".join("?" * len(targets))
    for nm, lo, hi in (("2024H2", "2024-07-01", "2024-12-31"),
                       ("2025H1", "2025-01-01", "2025-06-30"),
                       ("2025H2", "2025-07-01", "2025-12-31"),
                       ("2026H1", "2026-01-01", "2026-06-30"),
                       ("2026-07~08", "2026-07-01", "2026-08-06")):
        n = con.execute(
            f"SELECT COUNT(DISTINCT stock_id) FROM stock_daily_bars "
            f"WHERE source='finmind' AND stock_id IN ({q}) AND trade_date BETWEEN ? AND ?",
            (*targets, lo, hi)).fetchone()[0]
        print(f"  {nm}: {n} / {len(targets)} 檔")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
