#!/usr/bin/env python3
"""富邦 Neo API TMF 盤前試撮 quote 觀察器（研究用 · 唯讀 · 不下單）。

背景：``collect_fubon_premarket_quote.py`` 已證實股票 ``rest_client.stock.intraday.quote()``
在 08:30-09:00 會回傳明確區分「一般成交」跟「試撮」的兩個獨立欄位
（``lastTrade`` vs ``lastTrial``），且試撮價逐秒更新、最後一筆跟實際開盤價很接近
（2330 誤差 5 元、0050 誤差 0.05 元，見 2026-07-23 實測）。

**尚未驗證**：期貨 ``rest_client.futopt.intraday.quote()`` 在 TMF 日盤 08:30-08:45／
夜盤 14:50-15:00 試撮窗是否也回傳同樣的 lastTrade/lastTrial 區分欄位——這是本腳本
唯一要回答的問題。純讀取 marketdata REST，不呼叫任何 order／place_order 相關函式，
不連 order 層、不寫 production DB，只把逐輪原始 JSON 記錄到本機檔案供事後分析。

用法（手動測試，任何時間皆可跑，市場關閉時只會拿到最後快取值或錯誤）：
    PYTHONPATH=src .venv-fubon/bin/python scripts/research/collect_tmf_premarket_quote.py --once

正式觀察（人工或排程觸發，預設涵蓋日盤試撮窗 08:29-08:46）：
    PYTHONPATH=src .venv-fubon/bin/python scripts/research/collect_tmf_premarket_quote.py
    PYTHONPATH=src .venv-fubon/bin/python scripts/research/collect_tmf_premarket_quote.py \\
        --start-time 14:49:00 --end-time 15:01:00   # 夜盤試撮窗

輸出：reports/research/channel_lab/tmf_premarket_quote_log/<date>.jsonl（每輪一行原始 JSON）。

注意：必須用 ``.venv-fubon``（Python 3.8-3.13 限制，富邦 Neo SDK 規定）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from order.fubon_session import check_python_version, connect_fubon  # noqa: E402
from order.tmf_channel_marketdata import resolve_front_symbol  # noqa: E402

_TZ = ZoneInfo("Asia/Taipei")
OUT_DIR = ROOT / "reports" / "research" / "channel_lab" / "tmf_premarket_quote_log"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TMF 盤前試撮 quote 觀察器（研究用 · 唯讀）")
    p.add_argument("--date", metavar="YYYY-MM-DD", help="交易日（預設：今天，Asia/Taipei）")
    p.add_argument("--start-time", default="08:29:00", metavar="HH:MM:SS")
    p.add_argument("--end-time", default="08:46:00", metavar="HH:MM:SS")
    p.add_argument("--interval-sec", type=float, default=5.0, help="每輪輪詢間隔秒數")
    p.add_argument(
        "--once", action="store_true", help="忽略時間窗，立即輪詢一次後結束（手動測試用）。"
    )
    return p.parse_args()


def _poll_once(fut, symbol: str) -> dict:
    poll_ts = datetime.now(tz=_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")
    try:
        quote = fut.intraday.quote(symbol=symbol)
    except Exception as exc:  # noqa: BLE001 — 單輪失敗不應中斷觀察窗
        return {"poll_ts": poll_ts, "symbol": symbol, "error": str(exc)}
    return {"poll_ts": poll_ts, "symbol": symbol, "quote": quote}


def main() -> int:
    try:
        check_python_version()
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    args = _parse_args()
    now = datetime.now(tz=_TZ)
    trade_date = args.date or now.strftime("%Y-%m-%d")

    if not args.once:
        try:
            start_dt = datetime.strptime(f"{trade_date} {args.start_time}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=_TZ)
            end_dt = datetime.strptime(f"{trade_date} {args.end_time}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=_TZ)
        except ValueError as exc:
            print(f"[ERROR] --start-time/--end-time 格式錯誤：{exc}", file=sys.stderr)
            return 1
        now = datetime.now(tz=_TZ)
        if now > end_dt:
            print(f"[INFO] 現在（{now:%H:%M:%S}）已超過觀察窗結束時間（{args.end_time}），結束。")
            return 0
        if now < start_dt:
            wait_sec = (start_dt - now).total_seconds()
            print(f"[INFO] 現在（{now:%H:%M:%S}）早於觀察窗開始時間（{args.start_time}），等待 {wait_sec:.0f} 秒。")
            time.sleep(wait_sec)

    try:
        print("[INFO] 登入富邦 Neo API 並初始化 realtime marketdata（唯讀）...")
        session = connect_fubon(realtime=True)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"[ERROR] 富邦登入失敗：{exc}", file=sys.stderr)
        return 1

    try:
        symbol, name, end = resolve_front_symbol(session)
    except RuntimeError as exc:
        print(f"[ERROR] 解析 TMF 近月合約失敗：{exc}", file=sys.stderr)
        return 1
    print(f"[INFO] 近月合約 symbol={symbol} name={name} end={end}")

    fut = session.sdk.marketdata.rest_client.futopt

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{trade_date}.jsonl"

    n_rounds = 0
    with out_path.open("a", encoding="utf-8") as f:
        if args.once:
            row = _poll_once(fut, symbol)
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            f.flush()
            print(json.dumps(row, ensure_ascii=False, indent=2, default=str))
            return 0

        while datetime.now(tz=_TZ) <= end_dt:
            round_start = time.monotonic()
            row = _poll_once(fut, symbol)
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            f.flush()
            n_rounds += 1
            if "error" in row:
                print(f"[{row['poll_ts']}] error={row['error']}")
            else:
                q = row["quote"] or {}
                last_trade = q.get("lastTrade") if isinstance(q, dict) else None
                last_trial = q.get("lastTrial") if isinstance(q, dict) else None
                print(
                    f"[{row['poll_ts']}] lastTrade={last_trade} lastTrial={last_trial} "
                    f"isOpen={q.get('isOpen') if isinstance(q, dict) else None}"
                )
            elapsed = time.monotonic() - round_start
            time.sleep(max(0.0, args.interval_sec - elapsed))

    print(f"[INFO] 觀察窗結束，共輪詢 {n_rounds} 輪。輸出：{out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
