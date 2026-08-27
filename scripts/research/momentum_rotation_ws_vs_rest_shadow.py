#!/usr/bin/env python3
"""2026-08-13 使用者要求驗證「websocket連上後，是否真的更接近回測」——具體
驗證的是REST intraday.trades()輪詢的已知殘留風險（fetch_ticks_as_bars檔頭
docstring：真實回傳筆數上限未知，兩次poll之間成交超過上限會漏掉中間tick，
回測則是逐筆全量重播、理論零遺漏）。

比較法：REST路徑(fetch_ticks_as_bars)跟websocket路徑(fetch_ticks_as_bars_ws
——production同一份程式碼，這裡只是手動把
ORDER_MOMENTUM_ROTATION_WS_MARKETDATA_ENABLED設1去啟用)同時對production的
全部UNIVERSE symbol輪詢，各自累積「這個session看過的所有tick時間戳」聯集，
定期比對兩邊差集——如果REST真的有輪詢窗口截斷問題，該遺漏的tick理論上會
出現在ws_only、不會出現在rest_only（websocket是push、REST是有上限的輪詢
快照，不會比websocket多看到東西）。

Phase: 唯讀觀察，不寫ledger/不下單/不碰momentum_rotation_order.py任何live
路徑，跟momentum_rotation_websocket_trades_shadow.py同一個安全等級。

輸出：${GOLDENSTOCKS_DATA_DIR}/cache/momentum_rotation/ws_vs_rest_shadow_{date}.jsonl
執行：PYTHONPATH=src .venv/bin/python scripts/research/momentum_rotation_ws_vs_rest_shadow.py
停止：找PID kill，或Ctrl-C；也會在收盤(13:41)後自動停止並印出最終比較報告。
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")

os.environ["ORDER_MOMENTUM_ROTATION_WS_MARKETDATA_ENABLED"] = "1"

from order.dayflip_short_order import resolve_live_futures_symbol  # noqa: E402
from order.fubon_session import connect_fubon  # noqa: E402
from order.momentum_rotation_order import in_momentum_rotation_trade_window  # noqa: E402
from order.momentum_rotation_signal import FUTURES_ROOT, UNIVERSE  # noqa: E402
from order.momentum_rotation_tick_marketdata import fetch_ticks_as_bars  # noqa: E402
from order.momentum_rotation_websocket_feed import fetch_ticks_as_bars_ws  # noqa: E402
from stock_db import DATA_DIR  # noqa: E402

_TZ = ZoneInfo("Asia/Taipei")
OUT_DIR = DATA_DIR.parent / "cache" / "momentum_rotation"
_STOP = False


def _on_signal(signum, _frame) -> None:
    global _STOP
    _STOP = True
    print(f"signal {signum} -> stopping after current poll", flush=True)


def _out_path(now: datetime) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR / f"ws_vs_rest_shadow_{now.strftime('%Y-%m-%d')}.jsonl"


def _log(rec: dict) -> None:
    now = datetime.now(tz=_TZ)
    rec = {"ts": now.isoformat(timespec="milliseconds"), **rec}
    try:
        with _out_path(now).open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 -- logging must never crash the comparison
        print(f"log write error: {exc}", flush=True)


def _summarize(rest_seen: dict, ws_seen: dict) -> dict:
    out = {}
    for sym in rest_seen:
        r, w = rest_seen[sym], ws_seen[sym]
        out[sym] = {
            "rest_n": len(r), "ws_n": len(w),
            "rest_only_n": len(r - w), "ws_only_n": len(w - r),
        }
    return out


def main() -> int:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    session = connect_fubon()
    print("session ready, resolving live symbols for all UNIVERSE sids...", flush=True)

    live_symbols: dict[str, str] = {}
    for sid in UNIVERSE:
        root = FUTURES_ROOT.get(sid)
        if not root:
            continue
        resolved = resolve_live_futures_symbol(session, root)
        if resolved is None:
            print(f"  {sid}: resolve failed, skipped", flush=True)
            continue
        live_symbols[sid] = resolved[0]
        print(f"  {sid} -> {resolved[0]}", flush=True)

    all_symbols = list(live_symbols.values())
    rest_seen: dict[str, set] = {s: set() for s in all_symbols}
    ws_seen: dict[str, set] = {s: set() for s in all_symbols}
    n_poll = 0
    last_report = time.monotonic()

    print(f"comparing {len(all_symbols)} symbols, polling ~1s until 13:41 or stop...", flush=True)
    while not _STOP:
        hm = datetime.now(tz=_TZ).strftime("%H:%M")
        if not in_momentum_rotation_trade_window(hm):
            print(f"[{hm}] outside trade window, stopping", flush=True)
            break
        for sid, sym in live_symbols.items():
            try:
                rest_bars = fetch_ticks_as_bars(session, sym)
            except Exception as exc:  # noqa: BLE001
                rest_bars = []
                _log({"kind": "rest_error", "sid": sid, "symbol": sym, "error": str(exc)[:200]})
            try:
                ws_bars = fetch_ticks_as_bars_ws(session, sym, all_symbols=all_symbols)
            except Exception as exc:  # noqa: BLE001
                ws_bars = None
                _log({"kind": "ws_error", "sid": sid, "symbol": sym, "error": str(exc)[:200]})
            rest_seen[sym].update(b["t"] for b in rest_bars)
            if ws_bars:
                ws_seen[sym].update(b["t"] for b in ws_bars)
        n_poll += 1
        if time.monotonic() - last_report > 60:
            summary = _summarize(rest_seen, ws_seen)
            print(
                f"[{datetime.now(tz=_TZ).isoformat(timespec='seconds')}] poll#{n_poll} "
                f"summary: {json.dumps(summary, ensure_ascii=False)}",
                flush=True,
            )
            _log({"kind": "summary", "n_poll": n_poll, "summary": summary})
            last_report = time.monotonic()
        time.sleep(1.0)

    final = _summarize(rest_seen, ws_seen)
    for sym in all_symbols:
        final[sym]["rest_only_sample"] = sorted(rest_seen[sym] - ws_seen[sym])[:5]
        final[sym]["ws_only_sample"] = sorted(ws_seen[sym] - rest_seen[sym])[:5]
    print("=== FINAL ===", flush=True)
    print(json.dumps(final, ensure_ascii=False, indent=2), flush=True)
    _log({"kind": "final", "n_poll": n_poll, "final": final})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
