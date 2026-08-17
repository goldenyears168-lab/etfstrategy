#!/usr/bin/env python3
"""把「分點 tape 活躍但沒有價格」的標的補進 stock_daily_bars（預算制 · 可重複執行）.

問題背景（2026-08-17 查明）：
  `stock_daily_bars` 的每日排程來源**只有 ETF 成分股 watchlist**（約 258 檔，收盤 sync
  每輪回填 7 日滾動窗）。其餘 150~280 檔全部是**偶發手動 backfill 的殘影**——查 synced_at
  可見 2026-06-16、2026-07-14、2026-08-07 三批一次性寫入，跑完就不再更新。所以每日有價
  檔數會從 540 慢慢退潮到 419，而這不是「某個 job 壞了」，是**價格宇宙從來沒有排程來源**。

為什麼要修：大量訊號腳本用 `stock_broker_branch_daily INNER JOIN stock_daily_bars` 算金額，
缺價標的會被**靜默丟掉、不報錯**。已證實吃掉真實訊號：7610 @ 2026-07-30（9217 五日買超
1.456 億、淨比 1.000，live watch 完全沒看到）。同樣的機制也讓研究母體會在有人手動 backfill
時事後長大（第十輪 n=36→48 就是 2026-08-07 那批造成的）。

本腳本＝那個缺席的排程來源。設計重點：
  * **預算制**：每輪只處理 --max-stocks 檔，backlog 分多天排掉，不會一次打爆 FinMind。
  * **優先序**：近期 tape 買進股數大的先補（那些才可能跨過金額門檻），其次才是零星小量。
  * **冪等**：upsert；重跑不會重複、不會壞資料。
  * 取代 `run_songshan_follow_watch.py` 裡 `refresh_missing_ohlc()` 的 `missing[:80]`
    機會性補檔——那個上限對每日 200~350 檔缺價永遠追不上。

  PYTHONPATH=src .venv/bin/python scripts/ops/backfill_active_tape_bars.py --dry-run
  PYTHONPATH=src .venv/bin/python scripts/ops/backfill_active_tape_bars.py --max-stocks 250
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from finmind_client import fetch_finmind, finmind_token  # noqa: E402
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect, upsert_stock_daily_bars  # noqa: E402

SOURCE = "finmind"
DEFAULT_TAPE_DAYS = 3
DEFAULT_MAX_STOCKS = 250
DEFAULT_LOOKBACK_DAYS = 120  # 新標的第一次補多長的歷史
DEFAULT_DELAY = 0.35


def _f(v):
    return None if v is None or v == "" else float(v)


def _i(v):
    return None if v is None or v == "" else int(float(v))


def recent_trade_dates(conn, n: int) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT trade_date FROM stock_broker_branch_daily "
        "WHERE source=? ORDER BY trade_date DESC LIMIT ?",
        (SOURCE, n),
    ).fetchall()
    return [r[0] for r in rows]


def find_targets(conn, tape_days: int, limit: int) -> list[dict]:
    """近期 tape 活躍、但價格停更或從未有價的標的，依近期買進股數排序。

    刻意分兩趟做，而且 tape 窗口刻意開得很短（預設 3 個交易日）：
      * 把 last_bar 寫成相關子查詢會對每個 group 各掃一次 stock_daily_bars → 好幾分鐘。
      * tape 窗口開到 60 個交易日＝約 240 萬列 GROUP BY，實測同樣要好幾分鐘。
        `stock_broker_branch_daily` 有 2.23 億列，窗口每多一天就多約 4 萬列。
    3 個交易日約 12 萬列、秒級完成，而且每天跑一次的聯集本來就會蓋到所有活躍標的。
    """
    dates = recent_trade_dates(conn, tape_days)
    if not dates:
        return []
    latest = dates[0]
    ph = ",".join("?" * len(dates))

    tape = conn.execute(
        f"""
        SELECT b.stock_id, SUM(b.buy) AS tape_buy_shares,
               COUNT(DISTINCT b.trade_date) AS tape_days
        FROM stock_broker_branch_daily b
        WHERE b.source = ? AND b.trade_date IN ({ph})
          AND length(b.stock_id) = 4 AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND b.stock_id NOT GLOB '00*'
        GROUP BY b.stock_id
        """,
        (SOURCE, *dates),
    ).fetchall()

    last_bar = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT stock_id, MAX(trade_date) FROM stock_daily_bars WHERE source=? GROUP BY stock_id",
            (SOURCE,),
        )
    }

    out = [
        {
            "stock_id": r["stock_id"],
            "tape_buy_shares": r["tape_buy_shares"] or 0,
            "tape_days": r["tape_days"],
            "last_bar": last_bar.get(r["stock_id"]),
        }
        for r in tape
        if last_bar.get(r["stock_id"]) is None or last_bar[r["stock_id"]] < latest
    ]
    out.sort(key=lambda d: d["tape_buy_shares"], reverse=True)
    return out[:limit]


def backfill_one(conn, stock_id: str, start: date, end: date, delay: float) -> int:
    price_rows = fetch_finmind("TaiwanStockPrice", stock_id, start, end)
    time.sleep(delay)
    try:
        adj_rows = fetch_finmind("TaiwanStockPriceAdj", stock_id, start, end)
    except requests.HTTPError:
        adj_rows = []
    time.sleep(delay)
    adj_by_date = {str(r["date"])[:10]: float(r["close"]) for r in adj_rows if r.get("close") is not None}

    bars = []
    for row in price_rows:
        trade_date = str(row["date"])[:10]
        close = _f(row.get("close"))
        if close is None:
            continue
        bars.append(
            {
                "stock_id": stock_id,
                "trade_date": trade_date,
                "open": _f(row.get("open")),
                "high": _f(row.get("max")),
                "low": _f(row.get("min")),
                "close": close,
                "adj_close": adj_by_date.get(trade_date),
                "volume": _i(row.get("Trading_Volume") or row.get("volume")),
                "amount": _f(row.get("Trading_money")),
                "source": SOURCE,
            }
        )
    if bars:
        upsert_stock_daily_bars(conn, bars)
    return len(bars)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tape-days", type=int, default=DEFAULT_TAPE_DAYS)
    ap.add_argument("--max-stocks", type=int, default=DEFAULT_MAX_STOCKS)
    ap.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    ap.add_argument("--dry-run", action="store_true", help="只列出目標與優先序，不打 FinMind、不寫 DB")
    args = ap.parse_args()

    load_project_dotenv()  # FINMIND_TOKEN 在 ${GOLDENSTOCKS_DATA_DIR}/.env
    conn = connect(DEFAULT_DB_PATH)
    conn.execute("PRAGMA busy_timeout = 60000")
    try:
        targets = find_targets(conn, args.tape_days, args.max_stocks)
        if not targets:
            print("無待補標的（tape 活躍者皆已有最新價格）")
            return 0

        print(f"待補 {len(targets)} 檔（預算上限 {args.max_stocks}）· 依近 {args.tape_days} 交易日 tape 買進股數排序")
        for t in targets[:15]:
            print(f"  {t['stock_id']}  buy_shares={int(t['tape_buy_shares'] or 0):>10,}  "
                  f"tape_days={t['tape_days']:>3}  last_bar={t['last_bar'] or '(從未有價)'}")
        if len(targets) > 15:
            print(f"  … 其餘 {len(targets) - 15} 檔")

        if args.dry_run:
            print("\n--dry-run：未打 FinMind、未寫 DB")
            return 0

        if not finmind_token():
            print("ERROR: FINMIND_TOKEN unset", file=sys.stderr)
            return 2

        today = date.today()
        ok = failed = written = 0
        for t in targets:
            sid = str(t["stock_id"])
            last_bar = t["last_bar"]
            start = (
                date.fromisoformat(last_bar) + timedelta(days=1)
                if last_bar
                else today - timedelta(days=args.lookback_days)
            )
            if start > today:
                continue
            try:
                n = backfill_one(conn, sid, start, today, args.delay)
                written += n
                ok += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  [ERROR] {sid}: {exc}", file=sys.stderr)

        print(f"\n完成：{ok} 檔補檔成功、{failed} 檔失敗、共寫入 {written} 列")
        return 1 if failed and not ok else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
