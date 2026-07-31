#!/usr/bin/env python3
"""全市場個股融資餘額回填 (FinMind → stock_margin_daily)。

- Universe: TaiwanStockInfo 的 twse+tpex 4 位數個股 (~3000)。
- 增量: 依 stock_margin_daily 現有 MAX(trade_date) 只補缺口; 已到最新則跳過。
- 配額韌性: 遇 FinMind 上限(402/limit)自動退避重試; 可隨時重跑續傳(upsert 冪等)。
- 只回融資 (省 3x 請求); 借券/當沖另案。

用法: PYTHONPATH=src python scripts/backfill_full_stock_margin.py [--floor 2018-01-01] [--limit N]
"""
from __future__ import annotations
import sys, time, sqlite3, argparse
from datetime import date, timedelta
sys.path.insert(0, "src")

import requests
from finmind_client import fetch_finmind
from chip_data import parse_margin_rows
from stock_db.chip import upsert_stock_margin_daily
from project_dotenv import finmind_token_from_env

DB = "data/stocks.db"
LOG = "/private/tmp/claude-501/-Users-jackm4-Documents-ETF-----/f1e96bff-be81-4322-817e-1255c9069fac/scratchpad/backfill_margin.log"
TARGET_MAX = "2026-07-29"   # 已到此日者跳過


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def get_universe() -> list[str]:
    tok = finmind_token_from_env()
    r = requests.get("https://api.finmindtrade.com/api/v4/data",
                     params={"dataset": "TaiwanStockInfo", "token": tok}, timeout=60).json()
    seen, out = set(), []
    for x in r.get("data", []):
        sid = str(x.get("stock_id", ""))
        if sid.isdigit() and len(sid) == 4 and x.get("type") in ("twse", "tpex") and sid not in seen:
            seen.add(sid); out.append(sid)
    return sorted(out)


def fetch_with_backoff(sid: str, start: date, end: date, max_wait_cycles: int = 40) -> list[dict]:
    """遇配額上限退避重試; 其他錯誤丟出。"""
    for attempt in range(max_wait_cycles):
        try:
            return fetch_finmind("TaiwanStockMarginPurchaseShortSale", sid, start, end)
        except Exception as e:
            msg = str(e).lower()
            if "402" in msg or "limit" in msg or "upper" in msg or "429" in msg:
                log(f"  quota hit on {sid}, 退避 90s (cycle {attempt+1})")
                time.sleep(90)
                continue
            raise
    raise RuntimeError(f"{sid}: 配額退避超過上限")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--floor", default="2018-01-01")
    ap.add_argument("--limit", type=int, default=0, help="只處理前 N 檔(測試用)")
    ap.add_argument("--delay", type=float, default=0.35)
    ap.add_argument("--stocks", default="", help="逗號分隔的指定股號(覆蓋全市場 universe)")
    args = ap.parse_args()
    floor = date.fromisoformat(args.floor)

    conn = sqlite3.connect(DB, timeout=60)
    maxmap = {r[0]: r[1] for r in conn.execute(
        "SELECT stock_id, MAX(trade_date) FROM stock_margin_daily GROUP BY stock_id")}
    universe = [s.strip() for s in args.stocks.split(",") if s.strip()] if args.stocks else get_universe()
    todo = [s for s in universe if (maxmap.get(s) or "") < TARGET_MAX]
    if args.limit:
        todo = todo[:args.limit]
    log(f"=== 全市場融資回填啟動 === universe={len(universe)} 現有覆蓋={len(maxmap)} 待處理={len(todo)} floor={floor}")

    done = rows_total = warn = 0
    for i, sid in enumerate(todo):
        prev = maxmap.get(sid)
        start = (date.fromisoformat(prev) + timedelta(days=1)) if prev else floor
        end = date.today()
        try:
            raw = fetch_with_backoff(sid, start, end)
            rows = parse_margin_rows(sid, raw)
            n = upsert_stock_margin_daily(conn, rows) if rows else 0
            rows_total += n
            done += 1
        except Exception as e:
            warn += 1
            log(f"  WARN {sid}: {e}")
        if (i + 1) % 25 == 0:
            log(f"進度 {i+1}/{len(todo)} | 完成={done} 寫入列={rows_total} 警告={warn} | 最近={sid}")
        time.sleep(args.delay)

    conn.close()
    log(f"=== 完成 === 處理={done}/{len(todo)} 寫入列={rows_total} 警告={warn}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
