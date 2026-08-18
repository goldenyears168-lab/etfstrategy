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
  * **優先序**：近期 tape 買進**金額**（股數 × 最近已知收盤價）大的先補——用股數排序會把
    高價股系統性排到後面（7610 一股 2,425 元、84,577 股＝2.05 億，用股數排卻進不了前 250）。
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


def find_targets_for_branch(conn, branch_id: str, since: str, limit: int) -> list[dict]:
    """單一分點在 `since` 之後碰過、但價格缺漏的標的（依 tape 買進股數排序）。

    用途：研究母體修復。`scan_5d_net95` 這類訊號是 `stock_broker_branch_daily INNER JOIN
    stock_daily_bars`，缺價標的會被靜默丟掉——2026-08-17 實測 9217 在 2024-07 起的 tape
    有 50.0% 的列沒有對應收盤價、2,236 檔標的中 1,183 檔在 DB 完全沒有任何價格。
    補其中 40 檔（3.4%）就讓母體從 n=53／mean +2.26% 變成 n=71／mean −0.27%。

    走 (securities_trader_id, trade_date) 索引，單分點約 31 萬列、秒級完成；
    不要用全市場版做長窗口聚合（2.23 億列會跑好幾分鐘）。
    """
    rows = conn.execute(
        """
        SELECT b.stock_id, SUM(b.buy) AS tape_buy_shares,
               COUNT(DISTINCT b.trade_date) AS tape_days
        FROM stock_broker_branch_daily b
        WHERE b.source = ? AND b.securities_trader_id = ? AND b.trade_date >= ?
          AND length(b.stock_id) = 4 AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND b.stock_id NOT GLOB '00*'
        GROUP BY b.stock_id
        """,
        (SOURCE, branch_id, since),
    ).fetchall()
    have = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT stock_id, MIN(trade_date) FROM stock_daily_bars WHERE source=? GROUP BY stock_id",
            (SOURCE,),
        )
    }
    out = [
        {
            "stock_id": r["stock_id"],
            "tape_buy_shares": r["tape_buy_shares"] or 0,
            "tape_days": r["tape_days"],
            "last_bar": None,  # 強制從 since 起補完整歷史
            "est_buy_ntd": 0.0,
            "price_known": False,
            "first_bar": have.get(r["stock_id"]),
        }
        for r in rows
        # 完全沒有價格，或最早的 bar 晚於研究窗起點（歷史不完整）
        if have.get(r["stock_id"]) is None or have[r["stock_id"]] > since
    ]
    out.sort(key=lambda d: d["tape_buy_shares"], reverse=True)
    return out[:limit]


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

    # ⚠️ 2026-08-17 修 bug：原本直接用「買進股數」排序，結果高價股被系統性排到後面——
    # 7610 近 3 日買 84,577 股看似很少，但它一股 2,425 元＝2.05 億，是本清單最該先補的之一，
    # 卻因為股數小而落在 250 檔預算之外，補了一輪還在漏（它正是先前被靜默吃掉的真實訊號，
    # 9217 於 2026-07-30 在該股五日買超 1.456 億／淨比 1.000，live watch 完全沒看到）。
    # 改用「股數 × 最近已知收盤價」＝金額排序。從未有價者無從估價，退回用全體收盤價中位數
    # 當保守估計，排在有價可估者之後（它們多為小型股，金額門檻通常碰不到）。
    med_close = conn.execute(
        "SELECT close FROM stock_daily_bars WHERE source=? AND trade_date=? AND close>0 "
        "ORDER BY close LIMIT 1 OFFSET (SELECT COUNT(*)/2 FROM stock_daily_bars "
        "WHERE source=? AND trade_date=? AND close>0)",
        (SOURCE, latest, SOURCE, latest),
    ).fetchone()
    fallback_close = float(med_close[0]) if med_close and med_close[0] else 50.0

    px = {
        r[0]: float(r[1])
        for r in conn.execute(
            "SELECT b.stock_id, b.close FROM stock_daily_bars b "
            "JOIN (SELECT stock_id, MAX(trade_date) md FROM stock_daily_bars "
            "      WHERE source=? GROUP BY stock_id) m "
            "  ON m.stock_id=b.stock_id AND m.md=b.trade_date "
            "WHERE b.source=? AND b.close>0",
            (SOURCE, SOURCE),
        )
    }

    out = []
    for r in tape:
        sid = r["stock_id"]
        lb = last_bar.get(sid)
        if lb is not None and lb >= latest:
            continue
        shares = r["tape_buy_shares"] or 0
        known = px.get(sid)
        out.append(
            {
                "stock_id": sid,
                "tape_buy_shares": shares,
                "tape_days": r["tape_days"],
                "last_bar": lb,
                "est_buy_ntd": shares * (known if known else fallback_close),
                "price_known": known is not None,
            }
        )
    # 有價可估者優先（估值可信），再依估計金額由大到小
    out.sort(key=lambda d: (d["price_known"], d["est_buy_ntd"]), reverse=True)
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
    ap.add_argument("--branch-id", default="", help="研究母體修復模式：補該分點碰過但缺價的標的")
    ap.add_argument("--tape-since", default="2024-07-01", help="--branch-id 模式的 tape 起點")
    args = ap.parse_args()

    load_project_dotenv()  # FINMIND_TOKEN 在 ${GOLDENSTOCKS_DATA_DIR}/.env
    conn = connect(DEFAULT_DB_PATH)
    conn.execute("PRAGMA busy_timeout = 60000")
    try:
        if args.branch_id:
            targets = find_targets_for_branch(
                conn, args.branch_id, args.tape_since, args.max_stocks
            )
        else:
            targets = find_targets(conn, args.tape_days, args.max_stocks)
        if not targets:
            print("無待補標的（tape 活躍者皆已有最新價格）")
            return 0

        print(f"待補 {len(targets)} 檔（預算上限 {args.max_stocks}）· 依近 {args.tape_days} 交易日 tape 買進**金額**排序")
        for t in targets[:15]:
            est = t["est_buy_ntd"] / 1e8
            mark = "" if t["price_known"] else "（估·從未有價）"
            print(f"  {t['stock_id']}  est_buy={est:>7.2f}億{mark}  "
                  f"shares={int(t['tape_buy_shares']):>10,}  "
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
