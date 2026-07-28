#!/usr/bin/env python3
"""補回本機 stock_daily_bars 缺漏的股票（含已下市股票，修正 survivorship bias）。

背景（2026-07-28）：本機 stock_daily_bars 目前只有 1331 檔股票，但 FinMind
TaiwanStockInfo 列出約 3600 檔 4位數字代碼的股票（twse+tpex+emerging）。抽樣 30
檔缺漏股票測試，87% 是「目前仍在交易、只是從沒被本地排程抓過」，10% 是「資料
在某天之後就停了，判斷為已下市」（如 2384 勝華 2014-11-18 停、1107 建台 2012-06-06
停）——確認 FinMind TaiwanStockPrice 對已下市股票仍保留完整歷史資料，本地缺漏
純粹是「從沒抓過」，不是資料源本身沒有。

不分是否已下市，全部一起補（兩者都能擴大未來 cross-sectional 回測的宇宙，
已下市那部分直接修正 survivorship bias；仍在交易但沒抓過的那部分是額外的
宇宙覆蓋率提升）。

單一 stock_id 拿全部歷史只需 1 次 API 呼叫（不像分點資料要逐日抓），額度友善。

用法：
  PYTHONPATH=src .venv/bin/python \\
    scripts/research/backfill_delisted_stock_universe.py [--max-stocks N] [--delay 0.35]
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from finmind_client import fetch_finmind_dataset, finmind_token  # noqa: E402
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect, upsert_stock_daily_bars  # noqa: E402

SOURCE = "finmind"
EARLIEST = date(2010, 1, 1)


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


def normalize_price_rows(sid: str, rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        d = str(r.get("date") or "")[:10]
        close = r.get("close")
        if not d or close is None:
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
    ap.add_argument("--max-stocks", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.35)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = ap.parse_args()

    load_project_dotenv()
    if not finmind_token():
        print("ERROR: FINMIND_TOKEN unset", file=sys.stderr)
        return 2

    conn = connect(args.db)
    conn.execute("PRAGMA busy_timeout = 60000")

    print("fetching TaiwanStockInfo full universe list...", flush=True)
    info_rows = fetch_finmind_dataset("TaiwanStockInfo")
    import re

    four_digit = [
        r for r in info_rows
        if re.fullmatch(r"[0-9]{4}", r.get("stock_id", "")) and not r["stock_id"].startswith("00")
    ]
    local_ids = {
        r[0] for r in conn.execute(
            f"SELECT DISTINCT stock_id FROM stock_daily_bars WHERE source=?", (SOURCE,)
        ).fetchall()
    }
    missing = sorted({r["stock_id"] for r in four_digit} - local_ids)
    print(f"FinMind 4-digit universe={len(four_digit)}  local={len(local_ids)}  missing={len(missing)}", flush=True)

    if args.max_stocks > 0:
        missing = missing[: args.max_stocks]

    n_ok = 0
    n_err = 0
    n_nodata = 0
    n_rows_total = 0
    t0 = time.time()

    for i, sid in enumerate(missing, 1):
        try:
            rows = fetch_finmind_dataset(
                "TaiwanStockPrice", data_id=sid, start=EARLIEST, end=date.today()
            )
        except Exception as exc:  # noqa: BLE001
            reason = _hard_stop_reason(exc)
            if reason is not None:
                print(f"STOP {reason} at [{i}/{len(missing)}] {sid}: {exc}", file=sys.stderr, flush=True)
                conn.close()
                return 3 if reason == "ip_banned" else 4
            n_err += 1
            print(f"  WARN {sid}: {exc}", file=sys.stderr)
            time.sleep(max(1.0, args.delay))
            continue

        time.sleep(args.delay)

        if not rows:
            n_nodata += 1
            continue

        norm = normalize_price_rows(sid, rows)
        n_written = upsert_stock_daily_bars(conn, norm)
        n_rows_total += n_written
        n_ok += 1

        if i % 50 == 0 or i == len(missing):
            elapsed = time.time() - t0
            rate = i / max(elapsed, 1)
            eta_min = (len(missing) - i) / max(rate, 1e-9) / 60
            last_date = max((r["trade_date"] for r in norm), default="")
            print(
                f"[{i}/{len(missing)}] ok={n_ok} nodata={n_nodata} err={n_err} "
                f"rows={n_rows_total:,} last={sid}(thru {last_date}) "
                f"elapsed={elapsed/60:.1f}m eta={eta_min:.1f}m",
                flush=True,
            )

    conn.close()
    print(f"DONE ok={n_ok} nodata={n_nodata} err={n_err} rows_written={n_rows_total:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
