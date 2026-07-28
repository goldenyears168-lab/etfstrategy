#!/usr/bin/env python3
"""回補下市股票歷史日K線，做真正的存活者偏誤 (survivorship bias) 驗證用。

背景（2026-07-28）：先前的 Brown-Goetzmann-Ibbotson-Ross bound 分析結果是「0筆被
強制加回」，原因是 stock_daily_bars 裡 339 檔已知下市股票中，338 檔在本地完全沒有
價格歷史（只抓過目前還在市場上交易的股票）。FinMind 的 TaiwanStockPrice 對下市股票
依然保留完整的下市前歷史 K 線（已驗證 911609 揚子江、9915 億豐等案例）。

本腳本只做「回補」這一件事：
  1. 讀取下市清單 JSON（date/stock_id/stock_name）。
  2. 篩選 4 位數股票代碼（排除 00 開頭的 ETF/債券基金）且下市日期 >= 2015-01-01
     （跟 momentum 訊號評估窗最相關的子集）。
  3. 對每檔呼叫 FinMind TaiwanStockPrice 拿全歷史（含下市前最後交易日），
     寫入 stock_daily_bars，但 source 欄位固定為 'finmind_delisted'
     （**不是** 'finmind'）—— 這樣不會被任何用 WHERE source='finmind' 的正式查詢
     動到，完全獨立、安全、可回收。

用法：
  cd "股票研究目錄" && PYTHONPATH=src .venv/bin/python \\
      scripts/backfill_delisted_stock_prices.py \\
      [--delisting-json PATH] [--max-stocks N] [--delay 0.35] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from finmind_client import fetch_finmind_dataset, finmind_token  # noqa: E402
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect, upsert_stock_daily_bars  # noqa: E402

SOURCE = "finmind_delisted"
DEFAULT_DELISTING_JSON = Path(
    "/private/tmp/claude-501/-Users-jackm4-Documents-ETF-----/"
    "23d1e4ad-e00c-4bea-98bd-c10e716d0856/scratchpad/delisting_list.json"
)
# 抓到比 2015-01-01 更早一點的資料，讓 12 個月動能回看窗在事件早期也有足夠歷史。
FETCH_START = date(2013, 1, 1)
CUTOFF_DATE = "2015-01-01"


class FinMindHardStop(RuntimeError):
    pass


def _hard_stop_reason(exc: BaseException) -> str | None:
    text = str(exc).lower()
    if "ip banned" in text or "ip ban" in text:
        return "ip_banned"
    if "upper limit" in text or "payment required" in text:
        return "quota"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        code = int(exc.response.status_code)
        body = (exc.response.text or "").lower()
        if code == 403:
            return "ip_banned"
        if code == 402 or "upper limit" in body:
            return "quota"
    return None


def load_target_universe(delisting_json: Path) -> list[dict]:
    records = json.loads(delisting_json.read_text(encoding="utf-8"))
    out = []
    for r in records:
        sid = str(r.get("stock_id") or "").strip()
        d = str(r.get("date") or "")[:10]
        if not re.fullmatch(r"[0-9]{4}", sid):
            continue
        if sid.startswith("00"):
            continue
        if not d or d < CUTOFF_DATE:
            continue
        out.append({"stock_id": sid, "delisting_date": d, "stock_name": r.get("stock_name")})
    # de-dup by stock_id (keep first occurrence)
    seen = set()
    dedup = []
    for r in out:
        if r["stock_id"] in seen:
            continue
        seen.add(r["stock_id"])
        dedup.append(r)
    return sorted(dedup, key=lambda r: r["stock_id"])


def normalize_price_rows(sid: str, rows: list[dict], max_trade_date: str) -> list[dict]:
    """正規化並過濾出下市日以前(含)的資料列。

    重要：台股股票代碼下市後會被回收再利用給新公司（實測驗證：1262 綠悅-KY
    2019-10 下市，同代碼 2026-06 起變成完全不同的公司在交易，收盤價從 39.85
    直接跳到 75.0，中間缺口 6 年多）。FinMind TaiwanStockPrice 對同一代碼會
    回傳「連續」的完整歷史（含被回收後的新公司資料），如果不過濾，會把不相關
    的新公司價格污染進舊公司的下市前歷史，後續動能回測會被完全污染。
    因此這裡強制只保留 trade_date <= 下市清單日期(max_trade_date) 的資料列。
    """
    out = []
    for r in rows:
        d = str(r.get("date") or "")[:10]
        close = r.get("close")
        if not d or close is None:
            continue
        if d > max_trade_date:
            continue
        out.append(
            {
                "stock_id": sid,
                "trade_date": d,
                "open": r.get("open"),
                "high": r.get("max"),
                "low": r.get("min"),
                "close": close,
                "adj_close": None,
                "volume": r.get("Trading_Volume"),
                "amount": r.get("Trading_money"),
                "source": SOURCE,
            }
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--delisting-json", type=Path, default=DEFAULT_DELISTING_JSON)
    ap.add_argument("--max-stocks", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.35)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--dry-run", action="store_true", help="只列出目標清單，不打 API、不寫 DB")
    args = ap.parse_args()

    targets = load_target_universe(args.delisting_json)
    print(f"target universe (4-digit, non-00, delisted>={CUTOFF_DATE}): {len(targets)} 檔", flush=True)

    if args.max_stocks > 0:
        targets = targets[: args.max_stocks]

    if args.dry_run:
        for r in targets:
            print(f"  {r['stock_id']}  {r['stock_name']}  delisted={r['delisting_date']}")
        return 0

    load_project_dotenv()
    if not finmind_token():
        print("ERROR: FINMIND_TOKEN unset", file=sys.stderr)
        return 2

    conn = connect(args.db)
    conn.execute("PRAGMA busy_timeout = 60000")

    already = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT stock_id FROM stock_daily_bars WHERE source=?", (SOURCE,)
        ).fetchall()
    }
    todo = [r for r in targets if r["stock_id"] not in already]
    print(
        f"already backfilled (source={SOURCE}): {len(already)}  remaining: {len(todo)}",
        flush=True,
    )

    n_ok = 0
    n_err = 0
    n_nodata = 0
    n_rows_total = 0
    t0 = time.time()

    for i, rec in enumerate(todo, 1):
        sid = rec["stock_id"]
        delisting_date = rec["delisting_date"]
        # 加 30 天緩衝，避免下市清單日期跟最後交易日之間有一兩天落差時漏抓；
        # normalize_price_rows() 仍會用清單上的確切日期做二次過濾。
        fetch_end = date.fromisoformat(delisting_date)
        fetch_end = date.fromordinal(min(fetch_end.toordinal() + 30, date.today().toordinal()))
        try:
            rows = fetch_finmind_dataset(
                "TaiwanStockPrice", data_id=sid, start=FETCH_START, end=fetch_end
            )
        except Exception as exc:  # noqa: BLE001
            reason = _hard_stop_reason(exc)
            if reason is not None:
                print(f"STOP {reason} at [{i}/{len(todo)}] {sid}: {exc}", file=sys.stderr, flush=True)
                conn.close()
                return 3 if reason == "ip_banned" else 4
            n_err += 1
            print(f"  WARN {sid}: {exc}", file=sys.stderr)
            time.sleep(max(1.0, args.delay))
            continue

        time.sleep(args.delay)

        if not rows:
            n_nodata += 1
            print(f"  NODATA {sid} ({rec['stock_name']}) delisted={rec['delisting_date']}")
            continue

        norm = normalize_price_rows(sid, rows, max_trade_date=delisting_date)
        n_written = upsert_stock_daily_bars(conn, norm)
        n_rows_total += n_written
        n_ok += 1

        last_date = max((r["trade_date"] for r in norm), default="")
        print(
            f"[{i}/{len(todo)}] {sid} ({rec['stock_name']}) rows={n_written} "
            f"last_trade={last_date} delisted(list)={rec['delisting_date']}",
            flush=True,
        )

    elapsed = time.time() - t0
    conn.close()
    print(
        f"DONE ok={n_ok} nodata={n_nodata} err={n_err} rows_written={n_rows_total:,} "
        f"elapsed={elapsed/60:.1f}m",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
