#!/usr/bin/env python3
"""2026-08-13: standalone, read-only shadow listener for Fubon's futopt
market-data WEBSOCKET 'trades' channel (fubon_neo.adapter.build_websocket_client)
-- momentum-rotation目前用REST intraday.trades()輪詢(每1秒)當逐筆成交來源，
這裡評估websocket的'trades'頻道能不能真正取代輪詢、達到更即時的效果。

跟tmf_websocket_candle_shadow.py（另一個session為了TMF的candles頻道寫的）
不同：'trades'頻道**沒有**被fubon_neo.adapter.py的Speed模式限制擋掉（只有
'aggregates'/'candles'被擋，見該檔案WebSocketFutOptClientWrapper.subscribe）
——理論上momentum-rotation live worker現在用的Speed模式session應該就能直接訂閱
'trades'，不需要像TMF那樣另開Normal模式session。這裡用獨立session驗證，
不碰任何正式下單流程或momentum_rotation_order.py的live控制邏輯。

'trades'頻道的真實推送訊息格式未知（沒有文件、沒有.pyi、fugle_marketdata
套件原始碼裡也找不到範例）——比照tmf_websocket_candle_shadow.py同一個原則：
記錄看到的每一筆真實訊息，不要假設格式，讓真實流量告訴我們欄位長什麼樣。

Phase: evaluation only。不修改momentum_rotation_tick_marketdata.py或任何
live控制流程。真的要把REST trades()輪詢換成websocket，是另一個更大的步驟，
要等這支腳本確認真的能收到可用資料後才考慮。

只跑2個標的(3017/2049，個股期貨代碼RAFH6/FFFH6)當代表，不是全部12檔——
先確認頻道本身能不能用，不需要一次訂閱全部。

輸出：${GOLDENSTOCKS_DATA_DIR}/cache/momentum_rotation/websocket_trades_shadow_{date}.jsonl
執行：PYTHONPATH=src .venv/bin/python scripts/research/momentum_rotation_websocket_trades_shadow.py
停止：找PID kill，或Ctrl-C（有訊號處理，會乾淨結束）
"""
from __future__ import annotations

import json
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")

from order.fubon_session import connect_fubon  # noqa: E402
from stock_db import DATA_DIR  # noqa: E402

_TZ = ZoneInfo("Asia/Taipei")
OUT_DIR = DATA_DIR.parent / "cache" / "momentum_rotation"
SYMBOLS = ["RAFH6", "FFFH6"]  # 3017奇鋐, 2049上銀 —— 只拿兩檔代表先驗證
_STOP = False


def _on_signal(signum, _frame):
    global _STOP
    _STOP = True
    print(f"signal {signum} -> stopping after current tick", flush=True)


def _out_path(now: datetime) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR / f"websocket_trades_shadow_{now.strftime('%Y-%m-%d')}.jsonl"


def _log(kind: str, payload) -> None:
    now = datetime.now(tz=_TZ)
    rec = {"ts": now.isoformat(timespec="milliseconds"), "kind": kind, "payload": payload}
    try:
        with _out_path(now).open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 -- logging must never crash the listener
        print(f"[{now.isoformat(timespec='seconds')}] log write error: {exc}", flush=True)
    print(f"[{now.isoformat(timespec='seconds')}] {kind}: {json.dumps(payload, ensure_ascii=False, default=str)[:400]}", flush=True)


def main() -> int:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    session = connect_fubon(realtime=True)  # 預設Speed模式，跟live worker一致
    print(f"session realtime init done (mode=Speed, 跟live worker同一種), auth={getattr(session,'_tmf_realtime_ok', None)}", flush=True)

    ws = session.sdk.marketdata.websocket_client.futopt

    ws.on("connect", lambda: _log("connect", None))
    ws.on("disconnect", lambda code, msg: _log("disconnect", {"code": code, "msg": msg}))
    ws.on("error", lambda err: _log("error", str(err)))
    ws.on("authenticated", lambda msg: _log("authenticated", msg))
    ws.on("message", lambda raw: _log("message", json.loads(raw)))

    print("connecting...", flush=True)
    ws.connect()
    print(f"connected. auth_status={ws.auth_status} error={ws.error}", flush=True)

    for symbol in SYMBOLS:
        try:
            ws.subscribe({"channel": "trades", "symbol": symbol})
            _log("subscribe_sent", {"channel": "trades", "symbol": symbol})
        except Exception as exc:  # noqa: BLE001 -- Speed/Normal channel restrictions raise here
            _log("subscribe_error", {"channel": "trades", "symbol": symbol, "error": str(exc)})

    print("listening (observe-only, no orders, no live control flow touched)...", flush=True)
    n_report = 0
    last_report = time.monotonic()
    while not _STOP:
        time.sleep(1.0)
        if time.monotonic() - last_report > 60:
            print(f"[{datetime.now(tz=_TZ).isoformat(timespec='seconds')}] still listening, {n_report} 1-min reports so far", flush=True)
            last_report = time.monotonic()
            n_report += 1
    print("stopped cleanly", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
