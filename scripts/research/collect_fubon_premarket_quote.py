#!/usr/bin/env python3
"""富邦 Neo API 盤前試撮 quote 收集器（研究用 · 累積歷史庫供未來回測）。

背景：``collect_pre_market_auction_snapshot.py``（FinMind 來源）已在 2026-07-22
實測證實無法用——``taiwan_stock_tick_snapshot`` 對一般個股在 08:30-08:59 完全沒有
回應（0 筆），僅少數非典型代碼回應同一個凍結時間戳，直到 09:00:00 才開始有逐秒
更新的真實資料。已停用該收集器的 mini 排程。

2026-07-22 晚間實測發現：富邦 Neo API 的即時報價底層其實是 Fugle marketdata feed
（``sdk.init_realtime()`` → ``sdk.marketdata.rest_client.stock.intraday.quote()``），
回應內容包含：

  - 真正的五檔委買委賣（``bids``/``asks``，各 5 檔 price+size）
  - **明確區分「一般成交」跟「試撮」的兩個獨立欄位**：``lastTrade`` vs ``lastTrial``
    （各自有獨立的 price/size/serial/time），這正是盤前 08:30-09:00 集合競價
    試撮成交會落地的欄位。
  - ``isOpen``/``isOpenDelayed``/``isClose``/``isCloseDelayed`` 市場狀態旗標。

2026-07-23 週四盤前實測結果（已驗證，見 ``analyze_fubon_premarket_quote.py``）：08:30-09:00
的 ``lastTrial`` 真的逐秒更新，且最後一筆試撮價跟實際開盤價非常接近（2330 誤差 5 元、
0050 誤差 0.05 元）——這條路對「預測開盤價」有效；但試撮走勢本身看不出「開盤後會不會
走低（開高走低）」，那是開盤後才形成的，需要另外的訊號。當天 09:00 開盤瞬間曾因
mini 其他排程搶 sqlite 寫鎖讓收集器整個中斷掛掉，已修成「單輪失敗併入下一輪重試，
不中斷主迴圈」（見 ``_flush``），2026-07-23 晚間起生效。

用法（手動測試，任何時間皆可跑，market 關閉時只會拿到最後快取值）：
    PYTHONPATH=src .venv-fubon/bin/python scripts/research/collect_fubon_premarket_quote.py \\
        --once --symbols 2330,0050

正式盤前收集（Mac mini，週一至五 08:28 由 launchd 自動觸發，見
launchd/com.jackm4.goldenstocks.fubon-premarket-quote-collect.plist.template）：
    PYTHONPATH=src .venv-fubon/bin/python scripts/research/collect_fubon_premarket_quote.py
（預設 --start-time 08:29:00 --end-time 09:01:00 --interval-sec 5，
 若啟動時已在窗口內立即開始，若早於窗口會先等到 08:29:00，若晚於窗口直接結束。
 未指定 --symbols 時，預設＝目前持股 + 固定核心觀察清單，見 ``_DEFAULT_EXTRA_SYMBOLS``。）

收盤後看當天結果：
    PYTHONPATH=src .venv/bin/python scripts/research/analyze_fubon_premarket_quote.py \\
        --date 2026-07-23

注意：必須用 ``.venv-fubon``（Python 3.8-3.13 限制，富邦 Neo SDK 規定），跟
專案其他研究腳本慣用的 ``.venv`` 不同。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from order.fubon_account import inventories  # noqa: E402
from order.fubon_session import FubonSession, check_python_version, connect_fubon  # noqa: E402
from stock_db import (  # noqa: E402
    DEFAULT_DB_PATH,
    FubonPremarketQuoteRow,
    connect,
    upsert_fubon_premarket_quote,
    utc_now_iso,
)

_TZ = ZoneInfo("Asia/Taipei")
# 固定核心觀察清單（不隨持股變動），確保跨交易日可比較同一批股票的試撮準度：
# 0050=大盤代表 ETF、2330=權值龍頭、2317=電子代工、2454=IC 設計，涵蓋不同流動性/類股。
_DEFAULT_EXTRA_SYMBOLS = ["0050", "2317", "2330", "2454"]
_SOURCE = "fubon_fugle_quote"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="富邦 Neo API（Fugle marketdata）盤前試撮 quote 收集器（研究用）",
    )
    p.add_argument(
        "--symbols",
        help="逗號分隔股票代碼（例：2330,0050）。未指定則用目前持股 + 2330 + 0050。",
    )
    p.add_argument("--date", metavar="YYYY-MM-DD", help="交易日（預設：今天，Asia/Taipei）")
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument("--start-time", default="08:29:00", metavar="HH:MM:SS")
    p.add_argument("--end-time", default="09:01:00", metavar="HH:MM:SS")
    p.add_argument("--interval-sec", type=float, default=5.0, help="每輪輪詢間隔秒數")
    p.add_argument(
        "--once",
        action="store_true",
        help="忽略時間窗，立即輪詢一次所有 symbols 後結束（手動測試用）。",
    )
    return p.parse_args()


def _resolve_symbols(session: FubonSession, raw: str | None) -> list[str]:
    if raw:
        symbols = [s.strip() for s in raw.split(",") if s.strip()]
        if symbols:
            return symbols
    held: list[str] = []
    try:
        for row in inventories(session):
            today = int(row.get("today_qty", 0) or 0)
            tradable = int(row.get("tradable_qty", 0) or 0)
            if today > 0 or tradable > 0:
                held.append(row["stock_no"])
    except Exception as exc:  # noqa: BLE001 — 查持股失敗不應擋住收集
        print(f"[WARN] 查詢持股失敗，改用預設清單：{exc}")
    symbols = sorted(set(held) | set(_DEFAULT_EXTRA_SYMBOLS))
    print(f"[INFO] --symbols 未指定，改用持股 + 預設清單：{symbols}")
    return symbols


def _connect_db_with_retry(db_path: Path, *, attempts: int = 4, backoff_sec: float = 2.0):
    last_exc: sqlite3.OperationalError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return connect(db_path)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == attempts:
                raise
            last_exc = exc
            print(f"[WARN] database locked，重試 {attempt}/{attempts}：{exc}")
            time.sleep(backoff_sec * attempt)
    raise last_exc  # pragma: no cover


def _us_epoch_to_iso(us: Any) -> str | None:
    if us is None:
        return None
    try:
        dt = datetime.fromtimestamp(int(us) / 1_000_000, tz=_TZ)
    except (ValueError, OSError, OverflowError):
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


def _levels(items: list[dict] | None, key: str) -> list[float] | None:
    if not items:
        return None
    return [lv.get(key) for lv in items]


def _parse_quote(
    stock_id: str, quote: dict, trade_date: str, poll_ts: str, synced_at: str
) -> FubonPremarketQuoteRow:
    last_trade = quote.get("lastTrade") or {}
    last_trial = quote.get("lastTrial") or {}
    return FubonPremarketQuoteRow(
        stock_id=stock_id,
        trade_date=trade_date,
        poll_ts=poll_ts,
        bid_prices=_levels(quote.get("bids"), "price"),
        bid_sizes=_levels(quote.get("bids"), "size"),
        ask_prices=_levels(quote.get("asks"), "price"),
        ask_sizes=_levels(quote.get("asks"), "size"),
        last_trade_price=last_trade.get("price"),
        last_trade_size=last_trade.get("size"),
        last_trade_serial=last_trade.get("serial"),
        last_trade_ts=_us_epoch_to_iso(last_trade.get("time")),
        last_trial_price=last_trial.get("price"),
        last_trial_size=last_trial.get("size"),
        last_trial_serial=last_trial.get("serial"),
        last_trial_ts=_us_epoch_to_iso(last_trial.get("time")),
        is_open=quote.get("isOpen"),
        is_open_delayed=quote.get("isOpenDelayed"),
        is_close=quote.get("isClose"),
        is_close_delayed=quote.get("isCloseDelayed"),
        raw_json=json.dumps(quote, ensure_ascii=False),
        source=_SOURCE,
        synced_at=synced_at,
    )


def _poll_once(
    session: FubonSession, symbols: list[str], trade_date: str
) -> list[FubonPremarketQuoteRow]:
    rest_stock = session.sdk.marketdata.rest_client.stock
    poll_ts = datetime.now(tz=_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")
    synced_at = utc_now_iso()
    rows: list[FubonPremarketQuoteRow] = []
    for symbol in symbols:
        try:
            quote = rest_stock.intraday.quote(symbol=symbol)
        except Exception as exc:  # noqa: BLE001 — 單一股票失敗不應擋其他股票
            print(f"[ERROR] quote({symbol}) 失敗：{exc}")
            continue
        rows.append(_parse_quote(symbol, quote, trade_date, poll_ts, synced_at))
    return rows


def _flush(conn: sqlite3.Connection, rows: list[FubonPremarketQuoteRow], *, attempts: int = 6) -> bool:
    """寫入一輪 snapshot；即使重試後仍失敗，也只回報 False，不拋例外中斷主迴圈。

    2026-07-23 實測：09:00 開盤瞬間其他 mini 排程（order 層）會搶 sqlite 寫鎖，
    若讓例外往外拋，會讓整個收集器在最關鍵的開盤時刻直接掛掉、後面 40 秒全部拿不到。
    """
    if not rows:
        return True
    for attempt in range(1, attempts + 1):
        try:
            n = upsert_fubon_premarket_quote(conn, rows)
            print(f"[OK] 寫入/更新 {n} 筆 fubon_premarket_quote_snapshot。")
            for r in rows:
                trial = (
                    f"trial={r.last_trial_price}x{r.last_trial_size}@{r.last_trial_ts}"
                    if r.last_trial_price is not None
                    else "trial=None"
                )
                print(f"  - {r.stock_id} · {r.poll_ts} · {trial}")
            return True
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                print(f"[ERROR] upsert 失敗（非 lock 問題，放棄本輪）：{exc}")
                return False
            if attempt == attempts:
                print(f"[WARN] database locked，重試 {attempts} 次後仍失敗，放棄本輪（下一輪會併入重試）：{exc}")
                return False
            print(f"[WARN] upsert 時 database locked，重試 {attempt}/{attempts}：{exc}")
            time.sleep(min(2.0 * attempt, 8.0))
    return False  # pragma: no cover


def main() -> int:
    try:
        check_python_version()
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    args = _parse_args()

    if not args.db.exists():
        print(f"[ERROR] database not found: {args.db}", file=sys.stderr)
        return 1

    now = datetime.now(tz=_TZ)
    trade_date = args.date or now.strftime("%Y-%m-%d")
    try:
        datetime.strptime(trade_date, "%Y-%m-%d")
    except ValueError:
        print(f"[ERROR] --date 格式錯誤（需 YYYY-MM-DD）：{trade_date}", file=sys.stderr)
        return 1

    try:
        start_dt = datetime.strptime(f"{trade_date} {args.start_time}", "%Y-%m-%d %H:%M:%S")
        end_dt = datetime.strptime(f"{trade_date} {args.end_time}", "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        print(f"[ERROR] --start-time/--end-time 格式錯誤：{exc}", file=sys.stderr)
        return 1
    start_dt = start_dt.replace(tzinfo=_TZ)
    end_dt = end_dt.replace(tzinfo=_TZ)

    if not args.once:
        now = datetime.now(tz=_TZ)
        if now > end_dt:
            print(f"[INFO] 現在（{now:%H:%M:%S}）已超過收集窗口結束時間（{args.end_time}），結束。")
            return 0
        if now < start_dt:
            wait_sec = (start_dt - now).total_seconds()
            print(f"[INFO] 現在（{now:%H:%M:%S}）早於窗口開始時間（{args.start_time}），等待 {wait_sec:.0f} 秒。")
            time.sleep(wait_sec)

    try:
        print("[INFO] 登入富邦 Neo API 並初始化 realtime marketdata...")
        session = connect_fubon(realtime=True)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"[ERROR] 富邦登入失敗：{exc}", file=sys.stderr)
        return 1

    symbols = _resolve_symbols(session, args.symbols)
    if not symbols:
        print("[ERROR] 沒有可收集的股票代碼。", file=sys.stderr)
        return 1
    print(f"[INFO] trade_date={trade_date} symbols={symbols} interval={args.interval_sec}s")

    try:
        conn = _connect_db_with_retry(args.db)
    except sqlite3.OperationalError as exc:
        print(f"[ERROR] 資料庫持續鎖定：{exc}", file=sys.stderr)
        return 1

    try:
        if args.once:
            rows = _poll_once(session, symbols, trade_date)
            _flush(conn, rows)
            return 0

        n_rounds = 0
        pending: list[FubonPremarketQuoteRow] = []
        while datetime.now(tz=_TZ) <= end_dt:
            round_start = time.monotonic()
            rows = _poll_once(session, symbols, trade_date)
            to_write = pending + rows
            if _flush(conn, to_write):
                pending = []
            else:
                pending = to_write  # 併入下一輪重試，避免鎖表期間永久漏資料
            n_rounds += 1
            elapsed = time.monotonic() - round_start
            sleep_left = max(0.0, args.interval_sec - elapsed)
            time.sleep(sleep_left)
        if pending:
            print(f"[WARN] 收集窗口結束時仍有 {len(pending)} 筆未寫入，最後嘗試一次。")
            _flush(conn, pending)
        print(f"[INFO] 收集窗口結束，共輪詢 {n_rounds} 輪。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
