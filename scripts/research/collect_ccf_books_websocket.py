#!/usr/bin/env python3
"""聯電個股期（CCF）研究主軸＋五個配套指數期貨五檔即時收集器 · Fugle
marketdata websocket ``books`` channel，同一條連線同時訂閱六個商品，供之後
分層回測用（見 memory「五檔兩條真來源」2026-08-14 補充）：

  CCF  聯電期貨       研究主軸（目標個股，純半導體代工）
  SOF  半導體30期貨    產業層·半導體（跟 CCF 同族群,比 EXF 更精準）
  EXF  電子期貨        產業層·電子（較寬的電子業 beta，跟 SOF 做交叉比較）
  TMF  微型臺指期貨     大盤層（台股整體）
  SXF  美國費城半導體期貨（新台幣計價） 跨市場橋接·美股半導體情緒
  SPF  美國標普500期貨（新台幣計價）   跨市場橋接·美股大盤情緒

SXF／SPF 是直接掛在同一個富邦 TAIFEX API 上的美股連結商品，不需要
Databento／TouChance 就能間接摸到美股情緒。商品代碼皆由
``fut.intraday.products(type="FUTURE", exchange="TAIFEX")`` 現場查證
（2026-08-15，這支端點是靜態參考清單、週末休市也查得到，不像
``tickers()`` 要盤中才有資料）。

背景：2026-08-14 現場實測（見 memory「五檔兩條真來源」）發現
``session.sdk.marketdata.websocket_client.futopt`` 訂閱
``{"channel": "books", "symbol": ...}`` 會真的推播五檔（``bids``/``asks``
各 5 檔 price+size）——這推翻了 ``config/job_registry.yaml`` 舊記載的
「WebSocket 也沒有 Level-2 深度」（那句話只查了 REST）。

⚠️ 關鍵 gotcha：``subscribe`` 一定要帶 ``"afterHours": true/false``（布林值，
見富邦官方 websocket 文件 `fbs.com.tw/TradeAPI` books channel 說明）才會拿到
對應時段的推播；不帶這個欄位（或誤傳成 REST 那邊用的字串
``"session": "afterhours"``）會讓 SDK 靜默退回 ``afterHours=false``，訂閱後只
拿到日盤收盤當下凍結的單一 snapshot、之後永遠不會再推——現場踩過這個坑
（2026-08-14 20:09-20:22 卡了 13 分鐘查錯方向），帶對參數後 60 秒內收到
12 筆 ``event="data"``、五檔量真的在變動，確認推播頻率足夠密（數秒一筆）。
這支收集器同時訂閱日盤／夜盤兩路（見下方 ``ws.subscribe`` 呼叫兩次），
不用依賴時窗判斷手動切換。

安全設計（比照既有 ``tmf_websocket_candle_shadow.py`` 的模式）：
  - ``connect_fubon(realtime=False)`` 開一個獨立 session，**不觸碰**
    ``FubonSession`` 給 live TMF worker用的那個 session／``init_realtime()``
    預設 Speed mode——這支用 ``Mode.Normal``，兩者互不影響。
  - 純讀取市場資料，不建倉、不下單、不碰任何 order layer 程式碼路徑。
  - 逐筆 append 寫檔（不是攢批），單筆寫入失敗只印錯誤、不中斷主迴圈。
  - 外層 while True 重連：websocket 斷線或例外時，不嘗試複用舊的
    ws client（fubon_neo 底層物件重連行為未驗證過，風險未知），而是整組
    重新來——重新 login 一個新 session、重新 ``init_realtime``、重新訂閱，
    這跟 ``WebsocketCandleFeed`` 遇到 disconnect 就整個重建的作法一致。

輸出（每個 root 各自一個目錄／檔案，方便之後照時間戳 join）：
``${GOLDENSTOCKS_DATA_DIR}/cache/{root}_books/{root}_books_{YYYY-MM-DD}.jsonl``
每行一筆 JSON：
``{"ts", "event", "root", "symbol", "bids", "asks", "book_time", "quote_type"}``
（``quote_type`` 直接存 SDK 回傳的 ``FUTURE``／``FUTURE_AH``，不用自己猜日夜盤）。

2026-08-15 補收 ``trades`` channel（同一條連線，訂閱成本幾乎為零）：
``${GOLDENSTOCKS_DATA_DIR}/cache/{root}_trades/{root}_trades_{YYYY-MM-DD}.jsonl``
每行 ``{"ts", "root", "symbol", "price", "size", "bid", "ask", "trade_time"}``——
``bid``/``ask`` 是成交當下的最佳買賣價，可直接判斷主動買（price==ask）／主動賣
（price==bid），這是回測「限價單排隊能不能被吃到」的必要資料——之前只收
``books`` 沒有逐筆成交，沒辦法嚴謹模擬被動單成交機率，只能做粗略估計。

用法：
    PYTHONPATH=src .venv-fubon/bin/python scripts/research/collect_ccf_books_websocket.py
停止：找 process 直接 kill（``pgrep -f collect_ccf_books_websocket``）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")

from order.dayflip_short_order import resolve_live_futures_symbol  # noqa: E402
from order.fubon_session import connect_fubon  # noqa: E402
from stock_db import DATA_DIR  # noqa: E402

TZ = ZoneInfo("Asia/Taipei")
CACHE_DIR = DATA_DIR.parent / "cache"
SYMBOL_CACHE_PATH = CACHE_DIR / "futures_symbol_cache.json"
# 2026-08-19 加入 MXF（小型臺指）與 TXF（大台）：三段 walk-forward 顯示，同一個
# 訊號在掛單距離 ×2.0 下每筆毛額是 2.86 點（三段最小），而成本結構完全取決於
# 契約選擇——手續費是「每口 NT$X」，換算成點數要除以每點價值：
#   微台 TMF  NT$10/點 → NT$15/邊 = 1.50 點/邊 → 來回成本 4.05 點 → 每筆 −1.19
#   小台 MXF  NT$50/點 → NT$15/邊 = 0.30 點/邊 → 來回成本 1.65 點 → 每筆 +1.21
# 交易稅在點數上是尺度不變的（0.924/邊），所以手續費是唯一能靠換工具大幅壓縮的
# 項目。但上面那個 1.65 沿用了微台量到的價差與滑價，而 MXF 流動性遠優於 TMF、
# 價差很可能更窄——在真的搬過去之前必須用**實測**取代假設，這就是把它們加進
# 收集器的理由。TXF 一併收，作為同一指數第三個成本點的對照。
ROOTS = ["CCF", "TMF", "MXF", "TXF", "EXF", "SOF", "SXF", "SPF"]
RECONNECT_SLEEP_SEC = 5.0
# 比照 tmf_channel.session_pool.get_fubon_session 的 max_age_sec=3500 ——realtime
# token 沒有官方文件寫明存活多久，這個閾值是既有 production 程式碼驗證過的安全值，
# 沿用同一個數字，主動重連而不是隻靠 disconnect 事件（token 可能悄悄過期不觸發斷線）。
SESSION_MAX_AGE_SEC = 3500.0


#: book_time 落後 wall-clock 超過這個秒數就視為收盤側的凍結重送
STALE_BOOK_SEC = float(os.environ.get("BOOKS_STALE_SEC", "5"))


def _classify_book(book_time: object) -> tuple[str | None, float | None]:
    """(session, book_age_sec) — 由交易所端時間判定，不靠訂閱來源。

    ``book_time`` 是微秒 epoch。台指類日盤 08:45-13:45，其餘為夜盤。
    """
    try:
        bt = datetime.fromtimestamp(float(book_time) / 1e6, tz=TZ)
    except (TypeError, ValueError, OSError, OverflowError):
        return None, None
    hm = bt.strftime("%H:%M")
    sess = "day" if "08:45" <= hm <= "13:45" else "night"
    age = (datetime.now(tz=TZ) - bt).total_seconds()
    return sess, round(age, 3)


def _out_path(root: str, now: datetime) -> Path:
    out_dir = CACHE_DIR / f"{root.lower()}_books"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{root.lower()}_books_{now.strftime('%Y-%m-%d')}.jsonl"


def _trades_out_path(root: str, now: datetime) -> Path:
    out_dir = CACHE_DIR / f"{root.lower()}_trades"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{root.lower()}_trades_{now.strftime('%Y-%m-%d')}.jsonl"


def _log(msg: str) -> None:
    print(f"[{datetime.now(tz=TZ).isoformat(timespec='seconds')}] {msg}", flush=True)


def _write_row(root: str, row: dict) -> None:
    now = datetime.now(tz=TZ)
    try:
        with _out_path(root, now).open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 -- logging must never crash the collector
        _log(f"write error: {exc}")


def _write_trade_row(root: str, row: dict) -> None:
    now = datetime.now(tz=TZ)
    try:
        with _trades_out_path(root, now).open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 -- logging must never crash the collector
        _log(f"trade write error: {exc}")


def _load_symbol_cache() -> dict[str, str]:
    try:
        return json.loads(SYMBOL_CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_symbol_cache(cache: dict[str, str]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        SYMBOL_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 -- caching is best-effort
        _log(f"symbol cache write error: {exc}")


def _resolve_symbol_with_fallback(session, root: str, cache: dict[str, str]) -> tuple[str, str]:
    """``resolve_live_futures_symbol`` 走 Fubon ``tickers()`` 合約清單 REST，
    2026-08-15 凌晨現場實測發現這支 API 在凌晨時段會回傳空陣列（CCF/TXF/TMF
    全部查空），但同時間 TAIFEX MIS 直接查證明市場其實還在正常交易——問題在
    這支清單 API 本身不穩，不是市場休市。近月合約代碼一個晚上內不會變，
    所以查不到時直接沿用上次成功解析到的代碼，比因為這支 API 不穩就整晚斷線
    安全。"""
    resolved = resolve_live_futures_symbol(session, root)
    if resolved:
        symbol, name = resolved
        cache[root] = symbol
        _save_symbol_cache(cache)
        return symbol, name
    cached_symbol = cache.get(root)
    if cached_symbol:
        _log(f"resolve_live_futures_symbol({root!r}) empty — falling back to cached {cached_symbol}")
        return cached_symbol, cached_symbol
    raise RuntimeError(f"resolve_live_futures_symbol({root!r}) returned None and no cache available")


def _run_once() -> None:
    from fubon_neo.adapter import Mode

    session = connect_fubon(realtime=False)
    symbol_cache = _load_symbol_cache()
    symbol_to_root: dict[str, str] = {}
    for root in ROOTS:
        # per-root failure must not block the other roots (e.g. a brand-new
        # root with no cache entry yet, hit during a tickers() blackout
        # window) -- isolate so CCF/TMF etc. keep collecting regardless.
        try:
            symbol, name = _resolve_symbol_with_fallback(session, root, symbol_cache)
        except RuntimeError as exc:
            _log(f"skipping {root}: {exc}")
            continue
        symbol_to_root[symbol] = root
        _log(f"resolved live symbol: {root} -> {symbol} ({name})")
    if not symbol_to_root:
        raise RuntimeError("no roots resolved (all failed) -- nothing to subscribe")

    session.sdk.init_realtime(mode=Mode.Normal)
    ws = session.sdk.marketdata.websocket_client.futopt

    disconnected = {"flag": False}
    n_rows = {root: 0 for root in ROOTS}
    n_trades = {root: 0 for root in ROOTS}
    n_stale = {root: 0 for root in ROOTS}

    def on_message(raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        event = msg.get("event")
        data = msg.get("data")
        if event not in ("snapshot", "data") or not isinstance(data, dict):
            return
        channel = msg.get("channel") or data.get("channel")
        symbol = data.get("symbol")
        root = symbol_to_root.get(symbol)
        if root is None:
            return
        now_iso = datetime.now(tz=TZ).isoformat(timespec="milliseconds")

        if channel == "trades" or "trades" in data:
            for t in data.get("trades") or []:
                trow = {
                    "ts": now_iso,
                    "root": root,
                    "symbol": symbol,
                    "price": t.get("price"),
                    "size": t.get("size"),
                    "bid": t.get("bid"),
                    "ask": t.get("ask"),
                    "trade_time": data.get("time"),
                }
                _write_trade_row(root, trow)
                n_trades[root] += 1
                if n_trades[root] % 50 == 1:
                    _log(f"{root} trades rows so far: {n_trades[root]} (latest price={trow['price']} size={trow['size']})")
            return

        if channel != "books" and not ("bids" in data and "asks" in data):
            return
        # 日盤與夜盤兩路訂閱都送到這個 callback，訊息本身沒有任何欄位能分辨。
        # 收盤那一側會凍結在最後一筆，而且每次 session 續約（約 58 分）就把
        # 同一筆原樣重送一次：2026-08-17 00:10~03:06 寫進檔案的列全部是
        # 08-14 13:45 與 08-15 05:00 的收盤簿在鬼打牆。下游若不知情就會把
        # 殭屍當即時資料用（實測 336,953 列中有 492 列）。
        # 這裡用交易所端時間（book_time）補上兩個欄位，讓下游用欄位過濾，
        # 而不是自己逆推：
        #   session — 由交易所時間判定 day / night，不靠訂閱來源
        #   stale   — book_time 落後 wall-clock 超過門檻＝收盤側的凍結重送
        # 刻意「標記而不丟棄」：收盤簿本身有資訊價值，丟掉不可逆。
        book_time = data.get("time")
        sess, age_sec = _classify_book(book_time)
        row = {
            "ts": now_iso,
            "event": event,
            "root": root,
            "symbol": symbol,
            "bids": data.get("bids"),
            "asks": data.get("asks"),
            "book_time": book_time,
            "quote_type": data.get("type"),
            "session": sess,
            "book_age_sec": age_sec,
            "stale": (age_sec is not None and age_sec > STALE_BOOK_SEC),
        }
        if row["stale"]:
            n_stale[root] += 1
            if n_stale[root] % 20 == 1:
                _log(f"{root} stale book re-send #{n_stale[root]} "
                     f"(book_time 落後 {age_sec:.0f}s · session={sess}) — 已標記 stale=true")
        _write_row(root, row)
        n_rows[root] += 1
        if n_rows[root] % 20 == 1:
            _log(f"{root} books rows so far: {n_rows[root]} (latest bid1={row['bids'][0] if row['bids'] else None} ask1={row['asks'][0] if row['asks'] else None})")

    def on_disconnect(code, msg) -> None:
        _log(f"disconnect code={code} msg={msg}")
        disconnected["flag"] = True

    def on_error(err) -> None:
        _log(f"ws error: {err}")

    ws.on("message", on_message)
    ws.on("disconnect", on_disconnect)
    ws.on("error", on_error)

    ws.connect()
    _log(f"connected auth_status={ws.auth_status} error={ws.error}")
    if ws.error is not None:
        raise RuntimeError(f"auth failed: {ws.error}")

    for symbol, root in symbol_to_root.items():
        ws.subscribe({"channel": "books", "symbol": symbol, "afterHours": False})
        ws.subscribe({"channel": "books", "symbol": symbol, "afterHours": True})
        ws.subscribe({"channel": "trades", "symbol": symbol, "afterHours": False})
        ws.subscribe({"channel": "trades", "symbol": symbol, "afterHours": True})
        _log(f"subscribed books+trades (day + afterHours) for {root} {symbol}")

    started_mono = time.monotonic()
    last_report = time.monotonic()
    while not disconnected["flag"]:
        time.sleep(1.0)
        if time.monotonic() - started_mono > SESSION_MAX_AGE_SEC:
            _log("proactive session refresh (age limit reached)")
            break
        if time.monotonic() - last_report > 300:
            _log(f"alive. books rows so far: {n_rows}")
            last_report = time.monotonic()

    ws.disconnect()
    raise RuntimeError("reconnect (disconnect event or proactive age refresh)")


def main() -> int:
    _log(f"starting books+trades collector for {ROOTS} (Ctrl-C / kill to stop)")
    while True:
        try:
            _run_once()
        except KeyboardInterrupt:
            _log("stopped by user")
            return 0
        except Exception as exc:  # noqa: BLE001 -- must survive to reconnect
            _log(f"error: {exc!r} -- reconnecting in {RECONNECT_SLEEP_SEC}s")
            time.sleep(RECONNECT_SLEEP_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
