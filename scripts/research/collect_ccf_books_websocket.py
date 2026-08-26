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
import threading
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
# 2026-08-20 加入 momentum-rotation 的 12 檔個股期貨：該 sleeve 的訊號完全沒有用到
# 委託簿（無五檔、無掛單量、無買賣力道失衡、無牆壁偵測——2026-08-20 逐行核對確認，
# 見 src/order/momentum_rotation_signal.py），而 2026-08-13 唯一一天實彈 7 筆 0 勝 7 敗
# 的失敗形態正是「買在小尖頂／賣在小尖底」，也就是委託簿才看得見的那一類資訊。
# **五檔無法回補**（歷史 tick CSV 只有 price/volume），所以在還沒決定要不要重做這條
# 策略線之前就先開始累積——今天沒收的，之後任何研究都拿不回來。這是唯讀 collector
# （job_registry `order_capable: false`），加 root 不影響任何送單路徑；新 root 若解析
# 失敗會被 _run_once 的 per-root try/except 跳過，不會拖累既有 8 個 root。
_MOMENTUM_ROTATION_STOCK_FUTURES = [
    "RA",  # 3017 奇鋐
    "KB",  # 6213 聯茂
    "OP",  # 2345 智邦
    "PJ",  # 2383 台光電
    "QD",  # 3532 台勝科
    "GU",  # 2455 全新
    "QL",  # 3374 精材
    "OW",  # 6488 環球晶
    "HB",  # 2492 華新科
    "GH",  # 2376 技嘉
    "IP",  # 3035 智原
    "FF",  # 2049 上銀
]
# 2026-08-20（同日稍晚修正）：上面那 12 檔是 momentum-rotation 的**舊** universe，而
# 同一天做的全市場微結構篩選證明它們全都不該留——345 檔逐 tick 算「跳動單位/價格」
# 這個成本地板，對照 broad-universe holdout 實測的損平 10.9bps，那 12 檔沒有一檔進得了
# 前 20：最好的 6213 聯茂排 26/89，6488 環球晶 48.7bps 排 89/89（中位價 1026 元剛好
# 跨過 1000 元的跳動級距、一跳從 1 元變 5 元），另有 5 檔連 500 tick/日 都過不了。
# 下面這 7 檔才是篩出來的白名單（保守成本 = max(tick 成本, Roll 有效價差) < 10.9bps）。
# 舊 12 檔**不移除**：收集成本很低，而它們是唯一有實彈成交紀錄（2026-08-13）的標的，
# 保留著可以做「訊號 vs 真實成交」的對照。
_TRADABLE_WHITELIST_20260820 = [
    "PWF",  # 小型緯穎   5920 元 · 8.4bps · 1986 tick/日
    "SFF",  # 小型台光電 5155 元 · 9.7bps · 1316 tick/日
    "DQF",  # 群創         49 元 · 10.1bps · 16098 tick/日（全市場最密）
    "CKF",  # 國泰金       98 元 · 10.2bps · 572 tick/日
    "LUF",  # 臻鼎-KY     484 元 · 10.3bps · 3672 tick/日
    "RWF",  # 小型創意   4610 元 · 10.8bps · 2216 tick/日
    "IRF",  # 欣興        920 元 · 10.9bps · 1992 tick/日
]
#: 指數期貨核心組——TMF/MXF/TXF 是成本線與牆研究的標的，其餘四檔是既有對照。
_INDEX_CORE = ["CCF", "TMF", "MXF", "TXF", "EXF", "SOF", "SXF", "SPF"]

# 2026-08-21 暫時縮回核心 8 檔（原本是 8 + 12 + 7 = 27）。
# 起因：今日 11:08 出現「Maximum number of connections reached → authentication
# timeout」，8 roots 的舊行程已連續乾淨跑了 21 小時，所以**不是 root 數造成的原始
# 失敗**；但重啟後載入 27 roots 的新版之後，復原花了 2 小時 48 分、48 個重啟循環才
# 成功（11:11 → 13:59），而同一段時間正好是日盤，實測掉了 11:08–13:45 共 157 分鐘
# ＝ 300 分鐘日盤的 52%。27 roots × 4 訂閱（books/trades × 日盤/夜盤）＝ 108 個訂閱，
# 是復原困難最合理的解釋。
#
# 這是**暫時**措施，不是否定那 19 檔的價值——白名單本身有完整篩選理據（345 檔逐 tick
# 算跳動成本地板、對照 broad-universe holdout 的 10.9bps 損平），兩個清單都原樣留著。
# 要恢復請連同解決訂閱數上限一起做，例如：分成兩個 launchd job 各自持有一條連線、
# 或只訂 trades 不訂 books、或分時段輪流訂閱。直接把它們加回 ROOTS 會重現今天的事故。
_STOCK_FUTURES_PAUSED_20260821 = [
    *_MOMENTUM_ROTATION_STOCK_FUTURES,
    *_TRADABLE_WHITELIST_20260820,
]
ROOTS = list(_INDEX_CORE)
RECONNECT_SLEEP_SEC = 5.0
# 比照 tmf_channel.session_pool.get_fubon_session 的 max_age_sec=3500 ——realtime
# token 沒有官方文件寫明存活多久，這個閾值是既有 production 程式碼驗證過的安全值，
# 沿用同一個數字，主動重連而不是隻靠 disconnect 事件（token 可能悄悄過期不觸發斷線）。
SESSION_MAX_AGE_SEC = 3500.0

# 2026-08-20：這支曾經「行程活著但 33 小時沒收到任何一筆資料」（08-19 04:46 → 08-20
# 13:57）。launchd KeepAlive 沒救它，因為 KeepAlive 只在行程*結束*時重啟，而它沒死——
# 它卡在重連迴圈裡燒掉 2,002 分鐘 CPU、連 log 都不再寫。launcher 的 PID 鎖同樣沒救，
# 因為那個鎖只驗「行程在不在」，不驗「行程有沒有在做事」。三個修法：
#   (a) ws.connect() 之後一律 try/finally 收掉連線 —— 原本 "auth failed" 那條 raise
#       走在 ws.disconnect() 之前，每失敗一次就洩漏一條連線，洩到上限就變成
#       「Maximum number of connections reached → 認證逾時 → 再洩一條」的死亡螺旋，
#       正是當天 log 的樣子。而且洩漏的連線各自帶著執行緒，CPU 就是這樣被燒掉的。
#   (b) 連續失敗到上限就**主動退出行程**，讓 launchd 給一個乾淨的新行程 —— 洩漏的
#       是原生連線與執行緒，同一個行程內清不乾淨，換行程才是可靠的釋放方式。
#   (c) 看門狗執行緒盯「上次有進展是多久以前」，卡住就 os._exit —— (b) 的計數器只在
#       迴圈跑得動時才前進，擋不住阻塞在某個沒有 timeout 的呼叫裡的情況，而那正是
#       這次的實際症狀。
MAX_CONSECUTIVE_FAILURES = 6
RECONNECT_SLEEP_MAX_SEC = 120.0
#: 超過這麼久沒有任何進展（沒寫到資料、也沒寫出 log）就視為卡死。
#: 存活迴圈每 300 秒會寫一行 "alive."，所以正常的無行情時段（夜盤收盤 05:00 到
#: 日盤開盤 08:45 之間沒有任何 book）不會誤觸發——看門狗盯的是**進展**不是**成交量**。
WATCHDOG_STALL_SEC = 900.0

#: 最近一次有進展的 monotonic 時間（寫資料或寫 log 都算）。看門狗讀它。
_LAST_PROGRESS = {"mono": time.monotonic()}

# 2026-08-25：08-21 我把故障歸因於「27 roots × 4 訂閱 = 108 個訂閱撞上限」，並把
# 個股期貨拆成第二條連線。**那個診斷是錯的**——今天兩支收集器**同時**失敗
# （指數組 297 次、個股組 472 次「Maximum number of connections reached」），
# 拆開並沒有隔離任何東西，反而多佔一條連線。
#
# 真正的限制是**帳號層級的同時連線數**。mini 上同時持有 Fubon realtime 連線的行程
# 至少有：tmf-channel worker、momentum-rotation worker、dayflip-short worker、
# dayflip-post-dump-long worker、futopt-fill-listener、tmf/unf second-quote collector、
# websocket-candle-shadow，再加這兩支收集器 —— 八到十條。
#
# 而 order.fubon_session 沒有 logout()，連線只能靠**行程結束**釋放。這使得原本
# 「連續失敗 6 次就退出讓 launchd 重啟」的設計在這個錯誤上**幫倒忙**：退出→30 秒後
# 重啟→再試 6 次，今天兩支合計嘗試了 769 次連線，每一次都在跟**實彈 order worker**
# 搶同一個帳號的連線額度。研究層收集器沒有理由跟送單路徑搶資源。
#
# 所以這個錯誤要跟其他錯誤分開處理：認出它 → 長時間退讓 → 不要快速重啟。
_CONN_LIMIT_MARK = "Maximum number of connections reached"
#: 撞到帳號連線上限時退讓多久（秒）。刻意設長：等別的行程自然釋放，而不是硬搶。
CONN_LIMIT_BACKOFF_SEC = 600.0
#: 撞上限的次數要累積到這麼多才考慮退出行程（一般錯誤是 6 次）
CONN_LIMIT_MAX_FAILURES = 30
#: 由 ws error callback 設定，讓 main() 知道這一輪的失敗屬於「額度被別人佔滿」
_HIT_CONN_LIMIT = {"flag": False}


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
    _LAST_PROGRESS["mono"] = time.monotonic()
    print(f"[{datetime.now(tz=TZ).isoformat(timespec='seconds')}] {msg}", flush=True)


def _write_row(root: str, row: dict) -> None:
    _LAST_PROGRESS["mono"] = time.monotonic()
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


def _run_once() -> int:
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
    # 2026-08-20：捕獲率自動監測。trades 訊息帶交易所端的累計成交量
    # （data.total.tradeVolume，單調遞增），拿它跟「我們實際收到的 size 合計」相比，
    # 就能算出這條連線到底收到了市場成交的百分之幾——不必事後考古。
    # 動機：2026-08-18 的 CCF 只收到 archive 28% 的成交量，事後才查出是
    # 「Maximum number of connections reached」造成的反覆重連＋當天 11:32 才啟動。
    # 破碎的資料比沒有資料更危險（看起來像完整資料），所以讓它自己叫。
    # ⚠️ tradeVolume 是「該 session 內」的累計量，日盤→夜盤交替時會歸零重算。
    # 第一版寫成 last−first，遇到歸零就變負數、被 `delta <= 0` 靜默跳過，結果 CCF/TMF
    # 明明在成交卻整個從報告裡消失——跟這支收集器要監測的問題同一類錯（靜默丟棄）。
    # 改成偵測到 cum 下降就當作 session 重置、把上一段的量結算進累加器再重新起算。
    # key = (root, quote_type)；日盤與夜盤各自一條流，見 trades handler 的說明。
    cap: dict[tuple[str, str], dict] = {}

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
            arr = data.get("trades") or []
            cum_vol = (data.get("total") or {}).get("tradeVolume")
            serial = data.get("serial")
            # 日盤(FUTURE)與夜盤(FUTURE_AH)是**兩條獨立的流**，各自有自己的
            # tradeVolume 累計與 serial 序號空間。2026-08-20 實測 SXF 在同一秒
            # 收到 (cum=12,serial=382190) 與 (cum=17,serial=140724) 兩列——把兩者
            # 混進同一個累加器，計數器會來回跳、一直觸發「session 重置」分支，
            # 算出 SOF 4% / SXF 20% 這種假的低捕獲率。必須按 session 分開記。
            qtype = data.get("type") or "?"
            key = (root, qtype)
            c = cap.setdefault(
                key, {"prev_cv": None, "base_cv": None, "exch_total": 0,
                      "recv_total": 0, "prev_serial": None}
            )
            # 收盤側的凍結重送：books 那邊已知每次 session 續約（約 58 分）會把
            # 最後一筆原樣重送（見下方 books 分支的說明），trades 有**同樣**的
            # 問題但先前沒處理——2026-08-20 觀測到 SXF/SOF 在 21:55:57 與
            # 22:02:33 送出 serial/cum 完全相同的列。沿用 books 的原則
            # 「標記而不丟棄」：寫 stale=True 讓下游能過濾，但不吃掉資料。
            is_dup = (
                serial is not None
                and c["prev_serial"] == serial
                and c["prev_cv"] == cum_vol
            )
            if cum_vol is not None and not is_dup:
                if c["base_cv"] is None:
                    c["base_cv"] = cum_vol       # 第一則只當基準，它的 size 不計入分子
                elif cum_vol < c["prev_cv"]:
                    # session 重置：把上一段結算進累加器，從這一則重新起算
                    c["exch_total"] += c["prev_cv"] - c["base_cv"]
                    c["base_cv"] = cum_vol
                else:
                    c["recv_total"] += sum(int(t.get("size") or 0) for t in arr)
                c["prev_cv"] = cum_vol
            c["prev_serial"] = serial
            for t in arr:
                trow = {
                    "ts": now_iso,
                    "root": root,
                    "symbol": symbol,
                    "price": t.get("price"),
                    "size": t.get("size"),
                    "bid": t.get("bid"),
                    "ask": t.get("ask"),
                    "trade_time": data.get("time"),
                    # 下面兩個是 2026-08-20 新增的稽核欄位：cum_volume 讓下游能自己
                    # 重算任一時段的捕獲率；serial 供偵測訊息序列不連續。
                    "cum_volume": cum_vol,
                    "serial": serial,
                    "quote_type": qtype,   # FUTURE=日盤 / FUTURE_AH=夜盤，兩條獨立的流
                    "stale": is_dup,       # 收盤側凍結重送，標記而不丟棄
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
        if _CONN_LIMIT_MARK in str(err):
            _HIT_CONN_LIMIT["flag"] = True
        _log(f"ws error: {err}")

    ws.on("message", on_message)
    ws.on("disconnect", on_disconnect)
    ws.on("error", on_error)

    ws.connect()
    # 從這裡開始，無論怎麼離開都必須把連線收掉：券商端對同時連線數有上限，
    # 洩漏一條就少一條，洩滿就再也連不上（而且每條洩漏的連線都還帶著執行緒）。
    try:
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
                # 捕獲率報告：只報有成交在跑的 root（交易所累計量有推進的）。
                # 低於 95% 就明確標 LOW，讓覆蓋率不足變成看得見的告警而不是靜默劣化。
                parts, quiet = [], []
                for (r_, q_), c_ in cap.items():
                    exch = c_["exch_total"]
                    if c_["base_cv"] is not None and c_["prev_cv"] is not None:
                        exch += c_["prev_cv"] - c_["base_cv"]
                    tag = f"{r_}{'/AH' if q_.endswith('_AH') else ''}"
                    if exch <= 0:
                        # 明確列出來，不要靜默省略——「沒成交」跟「訂閱死掉」
                        # 在報告上必須長得不一樣，否則等於沒監測。
                        quiet.append(tag)
                        continue
                    pct = c_["recv_total"] / exch * 100.0
                    parts.append(f"{tag} {pct:.0f}%{' LOW' if pct < 95 else ''}")
                if parts:
                    _log("capture rate (received/exchange cumulative): " + "  ".join(parts))
                if quiet:
                    _log(f"capture rate: no exchange volume seen for {' '.join(quiet)}")
                last_report = time.monotonic()
    finally:
        try:
            ws.disconnect()
        except Exception as exc:  # noqa: BLE001 -- 收線失敗不能蓋掉真正的錯誤
            _log(f"disconnect during cleanup failed (ignored): {exc!r}")

    # 這一輪有沒有真的收到資料，決定 main() 要不要把連續失敗計數歸零：
    # 「連上了但一筆都沒收到」和「根本沒連上」對資料而言是同一件事。
    return sum(n_rows.values()) + sum(n_trades.values())


def _start_watchdog() -> None:
    """卡住就整個行程自殺，交給 launchd KeepAlive 重來。

    用 ``os._exit`` 而不是 ``sys.exit``：卡死的情況下主執行緒可能永遠不會收到
    例外（它正阻塞在某個沒有 timeout 的原生呼叫裡），只有硬退出保證會離開。
    """

    def _loop() -> None:
        while True:
            time.sleep(60.0)
            stalled = time.monotonic() - _LAST_PROGRESS["mono"]
            if stalled > WATCHDOG_STALL_SEC:
                _log(
                    f"WATCHDOG: no progress for {stalled:.0f}s "
                    f"(> {WATCHDOG_STALL_SEC:.0f}s) -- exiting so launchd restarts clean"
                )
                os._exit(3)

    threading.Thread(target=_loop, name="stall-watchdog", daemon=True).start()


def main() -> int:
    _log(f"starting books+trades collector for {ROOTS} (Ctrl-C / kill to stop)")
    _start_watchdog()
    consecutive_failures = 0
    while True:
        try:
            n_collected = _run_once()
        except KeyboardInterrupt:
            _log("stopped by user")
            return 0
        except Exception as exc:  # noqa: BLE001 -- must survive to reconnect
            n_collected = 0
            consecutive_failures += 1
            hit_limit = _HIT_CONN_LIMIT["flag"]
            _HIT_CONN_LIMIT["flag"] = False
            if hit_limit:
                # 帳號連線額度被佔滿：退出行程不會生出額度，只會多送一輪連線請求去跟
                # 實彈 order worker 搶。改成長時間退讓，並用獨立的、寬鬆得多的上限。
                if consecutive_failures >= CONN_LIMIT_MAX_FAILURES:
                    _log(f"connection limit persisted {consecutive_failures} rounds -- "
                         f"exiting so launchd restarts clean")
                    return 1
                _log(f"error: {exc!r} -- ACCOUNT CONNECTION LIMIT "
                     f"({consecutive_failures}/{CONN_LIMIT_MAX_FAILURES}), "
                     f"yielding to order workers for {CONN_LIMIT_BACKOFF_SEC:.0f}s")
                time.sleep(CONN_LIMIT_BACKOFF_SEC)
                continue
            backoff = min(RECONNECT_SLEEP_MAX_SEC, RECONNECT_SLEEP_SEC * (2 ** (consecutive_failures - 1)))
            _log(
                f"error: {exc!r} -- failure {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}, "
                f"reconnecting in {backoff:.0f}s"
            )
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                _log("too many consecutive failures -- exiting so launchd restarts clean")
                return 1
            time.sleep(backoff)
            continue
        if n_collected > 0:
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            _log(
                f"session ended having collected 0 rows -- treating as failure "
                f"{consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}"
            )
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                _log("too many consecutive empty sessions -- exiting so launchd restarts clean")
                return 1
        time.sleep(RECONNECT_SLEEP_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
