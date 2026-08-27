#!/usr/bin/env python3
"""補抓 9227(凱基-城中)／9661(富邦-新店) 2022-07~2024-06 分點資料（優先版）.

`backfill_dayflip_seats_history.py` 涵蓋 24 席 dayflip 候選席位、進度緩慢
（本次檢查時只完成 2023-11-01~07 共7天 x 23席）。本腳本只鎖定 branch-backward-
holdout-reverify 任務實際需要的兩個席位（9227/9661），寫入**同一張**獨立研究快取
（$GOLDENSTOCKS_DATA_DIR/data/research/branch_tape_hist.db，schema 相同、
INSERT OR REPLACE by (trader,date) 主鍵），可與原腳本安全共存、互相續跑不衝突。

用途：branch-backward-holdout-reverify（9227/9661 用 2022-07~2024-06 完全獨立於
forward-sample 研究的資料做 walk-forward 之外的第二次獨立驗證）。

  PYTHONPATH=src .venv/bin/python scripts/research/backfill_9227_9661_backward_priority.py \
      --start 2022-07-01 --end 2024-06-30 --max-seconds 1500
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

import stock_db
from finmind_client import fetch_taiwan_stock_trading_daily_report

SEATS = ["9227", "9661"]

CACHE_DIR = stock_db.DATA_DIR / "data" / "research"
CACHE = CACHE_DIR / "branch_tape_hist.db"
SLEEP = 0.35
MAX_RETRY = 3


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def init(con: sqlite3.Connection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS branch_tape (
        trade_date TEXT, securities_trader_id TEXT, stock_id TEXT,
        buy REAL, sell REAL,
        PRIMARY KEY (securities_trader_id, trade_date, stock_id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS progress (
        securities_trader_id TEXT, trade_date TEXT, n_rows INTEGER,
        fetched_at TEXT, PRIMARY KEY (securities_trader_id, trade_date))""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_bt_date ON branch_tape(trade_date)")
    con.commit()


def calendar(start: str, end: str) -> list[str]:
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    ds = [str(r[0]) for r in con.execute(
        "SELECT trade_date FROM stock_daily_bars WHERE source='finmind' "
        "AND stock_id='2330' AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        (start, end))]
    con.close()
    return ds


def is_ban(exc: BaseException) -> bool:
    s = str(exc).lower()
    return any(k in s for k in ("402", "429", "ban", "too many", "limit reached",
                                "forbidden", "403"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2022-07-01")
    ap.add_argument("--end", default="2024-06-30")
    ap.add_argument("--sleep", type=float, default=SLEEP)
    ap.add_argument("--max-seconds", type=float, default=1_800.0,
                     help="wall-clock budget; exits cleanly (resumable) when exceeded")
    args = ap.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(CACHE, timeout=30)
    init(con)

    cal = calendar(args.start, args.end)
    done = {(r[0], r[1]) for r in con.execute(
        "SELECT securities_trader_id, trade_date FROM progress WHERE securities_trader_id IN (?,?)",
        SEATS)}
    todo = [(t, d) for d in cal for t in SEATS if (t, d) not in done]
    log(f"交易日 {len(cal)} · 席位 {len(SEATS)} · 待抓 {len(todo)} / 已完成 {len(done)}")
    log(f"快取 → {CACHE}")
    if not todo:
        log("已全部完成")
        return 0

    t0 = time.time()
    ok = fail = rows_total = 0
    for i, (tid, d) in enumerate(todo, 1):
        if time.time() - t0 > args.max_seconds:
            log(f"達到時間預算 {args.max_seconds}s，停止（已完成 {ok}，重跑本腳本可續抓）")
            con.commit()
            break
        payload = None
        for attempt in range(MAX_RETRY):
            try:
                payload = fetch_taiwan_stock_trading_daily_report(
                    trade_date=date.fromisoformat(d), securities_trader_id=tid,
                    timeout=120)
                break
            except Exception as ex:  # noqa: BLE001
                if is_ban(ex):
                    con.commit()
                    log(f"‼️ 疑似被限流/封鎖，停止：{str(ex)[:120]}")
                    log(f"已完成 {ok} 筆，重跑本腳本可續抓")
                    return 2
                if attempt == MAX_RETRY - 1:
                    fail += 1
                    log(f"  {tid} {d} 放棄：{str(ex)[:70]}")
                else:
                    time.sleep(2 ** attempt)
        if payload is None:
            time.sleep(args.sleep)
            continue
        recs = []
        for r in payload:
            b = float(r.get("buy") or 0)
            s = float(r.get("sell") or 0)
            if b <= 0 and s <= 0:
                continue
            recs.append((d, tid, str(r.get("stock_id")), b, s))
        con.executemany(
            "INSERT OR REPLACE INTO branch_tape "
            "(trade_date, securities_trader_id, stock_id, buy, sell) VALUES (?,?,?,?,?)",
            recs)
        con.execute(
            "INSERT OR REPLACE INTO progress VALUES (?,?,?,?)",
            (tid, d, len(recs), datetime.now().isoformat(timespec="seconds")))
        ok += 1
        rows_total += len(recs)
        if i % 100 == 0:
            con.commit()
            el = time.time() - t0
            eta = el / i * (len(todo) - i) / 3600
            log(f"  {i}/{len(todo)} · 累計 {rows_total:,} 列 · 失敗 {fail} · "
                f"已跑 {el/3600:.2f}h · ETA {eta:.2f}h")
        time.sleep(args.sleep)
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM branch_tape WHERE securities_trader_id IN (?,?)", SEATS).fetchone()[0]
    days = con.execute(
        "SELECT COUNT(DISTINCT trade_date) FROM progress WHERE securities_trader_id IN (?,?)", SEATS
    ).fetchone()[0]
    log(f"目前累計：{n:,} 列 / {days} 交易日（雙席合計）/ 失敗 {fail} · 耗時 {(time.time()-t0)/3600:.2f}h")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
