#!/usr/bin/env python3
"""盤前試撮委買委賣 snapshot 收集器（研究用 · 從今天起累積自建歷史庫供未來回測）。

背景：FinMind／Yahoo／現有 TEJ 訂閱皆確認沒有「盤前試撮委買委賣」歷史封存資料可回測。
唯一可行路徑是每個交易日盤前自動收集 snapshot，累積屬於自己的歷史庫。

實測結論（2026-07-21 週二晚間收盤後執行，非盤前試撮時段 08:30-09:00，token 為既有付費 FINMIND_TOKEN）：

  1. TaiwanStockStatisticsOfOrderBookAndTrade（FinMind v4 /data，Free tier）：
     HTTP 200 可打通，但是「全市場彙總」——不接受 data_id（帶 data_id 會回
     HTTP 400 "parameter data_id don't provide on ... dataset"），且首筆
     Time 固定 09:00:00，不含 08:30-09:00 盤前試撮區段，欄位也只有
     Time/TotalBuyOrder/TotalBuyVolume/TotalSellOrder/TotalSellVolume/
     TotalDealOrder/TotalDealVolume/TotalDealMoney/date，沒有五檔委買委賣明細。
     → 無法滿足「個股」盤前試撮研究需求，但仍收集存檔（stock_id='MARKET'）供對照，
       可用 --skip-market-stats 跳過。

  2. TaiwanStockPriceMinuteBidAsk（使用者原先設想的即時最佳五檔資料集）：
     只存在於已停用的 FinMind v3 API（https://api.finmindtrade.com/api/v3/data
     現在直接回 HTTP 404 Not Found），v4 /data 的合法 dataset enum 清單裡完全
     沒有這個名稱（帶這個 dataset 名稱呼叫 v4 /data 會回 HTTP 422 "Input should
     be ... 'TaiwanStockPrice' ... 'TaiwanStockPriceTick' ..."，enum 清單裡沒有
     BidAsk 這個字）。
     → 這個 dataset 在目前 FinMind API 已經打不通，是端點本身消失，不是我們的
       token 權限不足。每次執行仍會探測一次（見 --skip-bidask-probe），以便
       之後 FinMind 若恢復可以立即發現。

  3. 個股層級的實際可用替代來源：既有 taiwan_stock_tick_snapshot
     （src/finmind_client.fetch_tick_snapshots，Sponsor tier，本專案 token 已可用，
     HTTP 200 成功取到 2330 資料），但只有「最佳一檔」委買/委賣
     （buy_price/buy_volume, sell_price/sell_volume），不是五檔。
     08:30-09:00 盤前這個端點是否真的會回試撮報價、還是要等 09:00 才有值，
     現在收盤後測試無法驗證（只拿得到最後一筆快取行情），需要明天盤前實測確認。

  4.（2026-07-21 晚間追加）taiwan_stock_tick_snapshot 支援 data_id 留空取得
     「全市場單次回應」（fetch_all_tick_snapshots），實測收盤後單次呼叫拿到
     2,852 檔股票（921ms）。用 --all-market 可一次覆蓋全市場，取代逐檔迴圈
     （逐檔對上千檔股票會太慢也太耗 API 額度），是免費方案下「足夠量大」的
     最佳解。

用法（手動執行，測試用）：
    PYTHONPATH=src .venv/bin/python scripts/research/collect_pre_market_auction_snapshot.py \\
        --symbols 2330,2317

    # 全市場（推薦，一次 API 呼叫覆蓋全部股票）：
    PYTHONPATH=src .venv/bin/python scripts/research/collect_pre_market_auction_snapshot.py \\
        --all-market --skip-market-stats --skip-bidask-probe

    # 未指定 --symbols 且未加 --all-market 時，改讀 order_holdings_snapshot 最新持股清單。

正式排程（Mac mini · 每個交易日 08:29-09:01 每分鐘一次）：
    見 launchd/com.jackm4.goldenstocks.pre-market-auction-collect.plist.template +
    scripts/launchd/pre-market-auction-collect.command。此收集器是研究層
    （scripts/research/），不屬於下單層，因此刻意不併入
    scripts/install-launchd.sh 的下單層陣列，改用獨立
    `launchctl load ~/Library/LaunchAgents/com.jackm4.goldenstocks.pre-market-auction-collect.plist`
    手動安裝於 mini（一次性，見上述 command 檔內註解）。

不新增依賴：只用 requests（既有）+ 專案既有 stock_db / finmind_client / project_dotenv。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from finmind_client import (
    fetch_all_tick_snapshots,
    fetch_finmind_json,
    fetch_taiwan_stock_statistics_of_order_book_and_trade,
    fetch_tick_snapshots,
)
from project_dotenv import finmind_token_from_env, load_project_dotenv
from stock_db import (
    DEFAULT_DB_PATH,
    PreMarketAuctionSnapshotRow,
    connect,
    load_latest_order_holdings_snapshot,
    upsert_pre_market_auction_snapshot,
    utc_now_iso,
)

_TZ = ZoneInfo("Asia/Taipei")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="盤前試撮委買委賣 snapshot 收集器（FinMind · 研究用，累積歷史庫）",
    )
    p.add_argument(
        "--symbols",
        help="逗號分隔股票代碼（例：2330,2317）。未指定且未加 --all-market 則讀取最新"
        " order_holdings_snapshot 持股清單。",
    )
    p.add_argument(
        "--all-market",
        action="store_true",
        help="單次 API 呼叫覆蓋全市場（taiwan_stock_tick_snapshot data_id 留空），"
        "忽略 --symbols，適合日常排程（免費方案下最大覆蓋量，且只耗 1 次額度）。",
    )
    p.add_argument("--date", metavar="YYYY-MM-DD", help="交易日（預設：今天，Asia/Taipei）")
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument(
        "--skip-market-stats",
        action="store_true",
        help="略過 TaiwanStockStatisticsOfOrderBookAndTrade 全市場彙總（盤前密集輪詢建議加此旗標省一次 API call）。",
    )
    p.add_argument(
        "--skip-bidask-probe",
        action="store_true",
        help="略過 TaiwanStockPriceMinuteBidAsk 存活探測（已確認 2026-07-21 打不通，可跳過）。",
    )
    return p.parse_args()


def _resolve_symbols(conn, raw: str | None) -> list[str]:
    if raw:
        symbols = [s.strip() for s in raw.split(",") if s.strip()]
        if symbols:
            return symbols
    holdings = load_latest_order_holdings_snapshot(conn)
    if holdings:
        symbols = sorted(holdings)
        print(f"[INFO] --symbols 未指定，改用最新 order_holdings_snapshot 持股清單：{symbols}")
        return symbols
    return []


def _connect_with_retry(db_path: Path, *, attempts: int = 4, backoff_sec: float = 2.0):
    """mini 上其他長跑任務（如分點回填）常佔用 sqlite 寫鎖；短暫重試比整次落空好。

    本收集器每 60 秒自我重跑，這裡的重試只是加速自我修復，不是取代排程重試。
    """
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
    raise last_exc  # pragma: no cover — 迴圈保證前面已 return 或 raise


def _is_locked(now_hms: str) -> bool:
    """TWSE 開盤試撮鎖單區段（08:59:55-09:00:00，此時仍可能更新試撮但無法再取消委託）。"""
    return bool(now_hms) and "08:59:55" <= now_hms < "09:00:00"


def _probe_legacy_bidask(trade_date: str) -> None:
    """探測 TaiwanStockPriceMinuteBidAsk 是否恢復（預期打不通，只印訊息，不寫入 DB）。"""
    try:
        fetch_finmind_json(
            {
                "dataset": "TaiwanStockPriceMinuteBidAsk",
                "data_id": "2330",
                "start_date": trade_date,
                "end_date": trade_date,
            }
        )
        print(
            "[WARN] TaiwanStockPriceMinuteBidAsk 意外回應成功！"
            "FinMind 可能已恢復此 dataset，建議重新檢查回傳欄位並補上收集邏輯。"
        )
    except Exception as exc:  # noqa: BLE001 — 探測用，任何失敗都算「打不通」
        print(f"[INFO] TaiwanStockPriceMinuteBidAsk 存活探測：打不通（預期內）— {exc}")


def _collect_market_stats(trade_date: str, synced_at: str) -> PreMarketAuctionSnapshotRow | None:
    day = datetime.strptime(trade_date, "%Y-%m-%d").date()
    try:
        rows = fetch_taiwan_stock_statistics_of_order_book_and_trade(day)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] TaiwanStockStatisticsOfOrderBookAndTrade 呼叫失敗：{exc}")
        return None
    if not rows:
        print(
            "[INFO] TaiwanStockStatisticsOfOrderBookAndTrade 回傳空資料"
            "（盤前 08:30-09:00 這是預期行為：此 dataset 首筆固定 09:00:00，不含試撮區段）。"
        )
        return None
    latest = rows[-1]
    return PreMarketAuctionSnapshotRow(
        stock_id="MARKET",
        trade_date=trade_date,
        snapshot_ts=f"{latest.get('date', trade_date)} {latest.get('Time', '')}".strip(),
        bid_prices=None,
        bid_volumes=[latest.get("TotalBuyVolume")],
        ask_prices=None,
        ask_volumes=[latest.get("TotalSellVolume")],
        match_price=None,
        match_volume=latest.get("TotalDealVolume"),
        is_locked=False,
        source="finmind_stat_orderbook_market",
        synced_at=synced_at,
    )


def _row_to_snapshot(
    row: dict, trade_date: str, synced_at: str, source: str
) -> PreMarketAuctionSnapshotRow | None:
    stock_id = str(row.get("stock_id") or "")
    if not stock_id:
        return None
    snapshot_dt = str(row.get("date") or "")
    snapshot_ts = snapshot_dt[:19] if len(snapshot_dt) >= 19 else f"{trade_date} unknown"
    now_hms = snapshot_ts[11:19] if len(snapshot_ts) >= 19 else ""
    buy_price = row.get("buy_price")
    buy_volume = row.get("buy_volume")
    sell_price = row.get("sell_price")
    sell_volume = row.get("sell_volume")
    return PreMarketAuctionSnapshotRow(
        stock_id=stock_id,
        trade_date=trade_date,
        snapshot_ts=snapshot_ts,
        bid_prices=[buy_price] if buy_price is not None else None,
        bid_volumes=[buy_volume] if buy_volume is not None else None,
        ask_prices=[sell_price] if sell_price is not None else None,
        ask_volumes=[sell_volume] if sell_volume is not None else None,
        match_price=row.get("close"),
        match_volume=row.get("volume"),
        is_locked=_is_locked(now_hms),
        source=source,
        synced_at=synced_at,
    )


def _collect_tick_snapshots(
    symbols: list[str], trade_date: str, synced_at: str
) -> list[PreMarketAuctionSnapshotRow]:
    rows, err = fetch_tick_snapshots(symbols)
    if err and not rows:
        print(f"[ERROR] taiwan_stock_tick_snapshot 呼叫失敗：{err}")
        return []
    if err:
        print(f"[WARN] taiwan_stock_tick_snapshot 部分股票失敗：{err}")
    if not rows:
        print("[INFO] taiwan_stock_tick_snapshot 回傳空資料。")
        return []
    out = [_row_to_snapshot(r, trade_date, synced_at, "finmind_tick_snapshot") for r in rows]
    return [r for r in out if r is not None]


def _collect_all_market_tick_snapshots(
    trade_date: str, synced_at: str
) -> list[PreMarketAuctionSnapshotRow]:
    rows, err = fetch_all_tick_snapshots()
    if err and not rows:
        print(f"[ERROR] taiwan_stock_tick_snapshot（全市場）呼叫失敗：{err}")
        return []
    if err:
        print(f"[WARN] taiwan_stock_tick_snapshot（全市場）部分失敗：{err}")
    if not rows:
        print("[INFO] taiwan_stock_tick_snapshot（全市場）回傳空資料。")
        return []
    out = [
        _row_to_snapshot(r, trade_date, synced_at, "finmind_tick_snapshot_all_market")
        for r in rows
    ]
    return [r for r in out if r is not None]


def main() -> int:
    load_project_dotenv()
    args = _parse_args()

    token = finmind_token_from_env()
    if not token:
        print("[ERROR] 未設定 FINMIND_TOKEN（檢查 .env）。", file=sys.stderr)
        return 1

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
    synced_at = utc_now_iso()

    try:
        conn = _connect_with_retry(args.db)
    except sqlite3.OperationalError as exc:
        print(f"[ERROR] 資料庫持續鎖定，放棄本次輪詢：{exc}", file=sys.stderr)
        return 1
    try:
        symbols: list[str] = []
        if not args.all_market:
            symbols = _resolve_symbols(conn, args.symbols)
            if not symbols:
                print(
                    "[ERROR] 沒有可收集的股票代碼，請用 --symbols 2330,2317 或 --all-market 指定，"
                    "或先確保 order_holdings_snapshot 有最新持股。",
                    file=sys.stderr,
                )
                return 1

        print(f"[INFO] trade_date={trade_date} all_market={args.all_market} symbols={symbols}")

        if not args.skip_bidask_probe:
            _probe_legacy_bidask(trade_date)

        all_rows: list[PreMarketAuctionSnapshotRow] = []

        if not args.skip_market_stats:
            market_row = _collect_market_stats(trade_date, synced_at)
            if market_row is not None:
                all_rows.append(market_row)

        if args.all_market:
            all_rows.extend(_collect_all_market_tick_snapshots(trade_date, synced_at))
        else:
            all_rows.extend(_collect_tick_snapshots(symbols, trade_date, synced_at))

        if not all_rows:
            print("[WARN] 本次沒有任何可寫入的 snapshot（可能是盤前/收盤後資料本來就是空的）。")
            return 0

        n = None
        for attempt in range(1, 5):
            try:
                n = upsert_pre_market_auction_snapshot(conn, all_rows)
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 4:
                    raise
                print(f"[WARN] upsert 時 database locked，重試 {attempt}/4：{exc}")
                time.sleep(2.0 * attempt)
        print(f"[OK] 寫入/更新 {n} 筆 pre_market_auction_snapshot。")
        for r in all_rows:
            print(f"  - {r.source} · {r.stock_id} · {r.snapshot_ts} · is_locked={r.is_locked}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
