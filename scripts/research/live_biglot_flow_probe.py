#!/usr/bin/env python3
"""盤中大戶主動買賣流即時探針 · 唯讀 · 無任何送單路徑.

用 Fubon 行情 websocket 的 trades 頻道重建「單筆金額 >= 500 萬的主動買 − 主動賣」，
即 pit_universe_tick 研究線 big500 訊號的 live 版本。

與離線版的差異（重要）：
  - 離線版用 FinMind TickType；FinMind 當日盤中不供 tick（實測回 0 筆），只能盤後拿。
  - 本探針用 Lee-Ready：成交價 >= ask 判主動買、<= bid 判主動賣，兩者皆非則用 tick rule。
  - **只能從訂閱當下起算**。要拿到 09:00 起的完整累積，必須 09:00 前啟動。

連線紀律（沿 chip_intraday_lot_probe.py，2026-08-21 教訓：108 訂閱打掉 52% 資料）：
獨立 session、獨立 websocket、訂閱上限 45×1 頻道、一次性行程自動退出。

用法：
    PYTHONPATH=src .venv-fubon/bin/python scripts/research/live_biglot_flow_probe.py
    PYTHONPATH=src .venv-fubon/bin/python scripts/research/live_biglot_flow_probe.py --smoke
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")

from order.fubon_session import connect_fubon  # noqa: E402
from stock_db import DATA_DIR  # noqa: E402

_TZ = ZoneInfo("Asia/Taipei")
OUT_DIR = DATA_DIR.parent / "cache" / "live_biglot_flow"
MAX_SYMBOLS = 45
BIG_AMT = 5_000_000       # 單筆 >= 500 萬 = 大戶，與離線版 big500 一致
END_HHMM = (13, 32)
_STOP = False
_LOCK = threading.Lock()
_RAW_FH = None
_LAST_PX: dict[str, float] = {}
_ACC: dict[str, dict] = {}


def _now() -> datetime:
    return datetime.now(_TZ)


def _on_signal(signum, _frame):
    global _STOP
    _STOP = True
    print(f"signal {signum} -> stopping", flush=True)


def _raw_write(kind, payload):
    global _RAW_FH
    if _RAW_FH is None:
        return
    with _LOCK:
        _RAW_FH.write(json.dumps({"ts": _now().isoformat(), "kind": kind,
                                  "payload": payload}, ensure_ascii=False) + "\n")
        _RAW_FH.flush()


def _on_message(raw):
    """容錯解析：不假設 schema，欄位一律 .get。"""
    try:
        msg = json.loads(raw)
    except Exception:  # noqa: BLE001
        return
    _raw_write("message", msg)
    if msg.get("channel") != "trades" or msg.get("event") not in (None, "data"):
        return
    d = msg.get("data") or {}
    sid = str(d.get("symbol", ""))
    px, size = d.get("price"), d.get("size")
    if not sid or px is None or size is None:
        return
    px, size = float(px), float(size)
    amt = px * size * 1000
    bid, ask = d.get("bid"), d.get("ask")
    side = 0
    if ask is not None and px >= float(ask):
        side = 1
    elif bid is not None and px <= float(bid):
        side = -1
    else:                                   # tick rule 退路
        prev = _LAST_PX.get(sid)
        if prev is not None:
            side = 1 if px > prev else (-1 if px < prev else 0)
    _LAST_PX[sid] = px
    a = _ACC.setdefault(sid, {"n": 0, "vol": 0.0, "big_buy": 0.0, "big_sell": 0.0,
                              "buy": 0.0, "sell": 0.0, "first_px": px, "last_px": px})
    a["n"] += 1
    a["vol"] += size
    a["last_px"] = px
    if side > 0:
        a["buy"] += size
        if amt >= BIG_AMT:
            a["big_buy"] += size
    elif side < 0:
        a["sell"] += size
        if amt >= BIG_AMT:
            a["big_sell"] += size


def scan_universe(session) -> list[dict]:
    """全市場 REST snapshot → 依成交值取前 MAX_SYMBOLS 檔（大戶單活躍的地方）。"""
    rest = session.sdk.marketdata.rest_client.stock
    quotes = []
    for market in ("TSE", "OTC"):
        try:
            resp = rest.snapshot.quotes(market=market)
            data = resp.get("data", resp) if isinstance(resp, dict) else resp
            quotes.extend(data or [])
            print(f"snapshot {market}: {len(data or [])} 檔", flush=True)
        except Exception as exc:  # noqa: BLE001
            _raw_write("snapshot_error", {"market": market, "error": str(exc)})
    out = []
    for q in quotes:
        sid = str(q.get("symbol", ""))
        if len(sid) != 4 or not sid.isdigit():
            continue
        out.append({"symbol": sid, "value": q.get("tradeValue") or 0,
                    "vol": q.get("tradeVolume") or 0,
                    "chg": q.get("changePercent"),
                    "px": q.get("closePrice") or q.get("lastPrice")})
    out.sort(key=lambda x: -x["value"])
    return out[:MAX_SYMBOLS]


def report(cands):
    meta = {c["symbol"]: c for c in cands}
    rows = []
    for sid, a in _ACC.items():
        net = a["big_buy"] - a["big_sell"]
        rows.append((net / a["vol"] * 100 if a["vol"] else 0, net, sid, a, meta.get(sid, {})))
    rows.sort(reverse=True)
    hdr = f"{'#':<3}{'代號':<7}{'佔窗口量%':>10}{'大戶淨買(張)':>13}{'窗口量(張)':>11}{'筆數':>8}{'窗口內%':>9}"
    print("\n【大戶(單筆>=500萬)主動買 — 僅涵蓋本次訂閱窗口】")
    print(hdr)
    for i, (norm, net, sid, a, m) in enumerate(rows[:15], 1):
        wr = (a["last_px"] / a["first_px"] - 1) * 100 if a["first_px"] else 0
        print(f"{i:<3}{sid:<7}{norm:>+10.1f}{net:>13,.0f}{a['vol']:>11,.0f}{a['n']:>8,}{wr:>+8.2f}%")
    print("\n【大戶主動賣最多】")
    print(hdr)
    for i, (norm, net, sid, a, m) in enumerate(rows[-8:][::-1], 1):
        wr = (a["last_px"] / a["first_px"] - 1) * 100 if a["first_px"] else 0
        print(f"{i:<3}{sid:<7}{norm:>+10.1f}{net:>13,.0f}{a['vol']:>11,.0f}{a['n']:>8,}{wr:>+8.2f}%")


def main() -> int:
    global _RAW_FH
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="登入+掃描+訂閱2檔，60秒後退出")
    ap.add_argument("--minutes", type=float, default=None, help="最多跑幾分鐘（預設跑到 13:32）")
    a = ap.parse_args()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    day = _now().strftime("%Y-%m-%d")
    _RAW_FH = (OUT_DIR / f"raw_{day}{'_smoke' if a.smoke else ''}.jsonl").open("a", encoding="utf-8")

    session = connect_fubon(realtime=True)
    print("fubon session ready（獨立連線 · 唯讀 · 無送單路徑）", flush=True)
    cands = scan_universe(session)
    _raw_write("candidates", cands)
    print(f"訂閱 {len(cands)} 檔（全市場成交值前 {MAX_SYMBOLS}）: {[c['symbol'] for c in cands]}", flush=True)
    if a.smoke:
        cands = cands[:2] or [{"symbol": "2330"}, {"symbol": "2317"}]

    ws = session.sdk.marketdata.websocket_client.stock
    ws.on("connect", lambda: _raw_write("connect", None))
    ws.on("disconnect", lambda code, msg: _raw_write("disconnect", {"code": code, "msg": msg}))
    ws.on("error", lambda err: _raw_write("error", str(err)))
    ws.on("authenticated", lambda msg: _raw_write("authenticated", msg))
    ws.on("message", _on_message)
    ws.connect()
    print(f"ws connected auth={getattr(ws, 'auth_status', '?')}", flush=True)
    for c in cands:
        try:
            ws.subscribe({"channel": "trades", "symbol": c["symbol"]})
        except Exception as exc:  # noqa: BLE001
            _raw_write("subscribe_error", {"symbol": c["symbol"], "error": str(exc)})
    t0 = _now()
    print(f"開始累積 {t0:%H:%M:%S}（窗口只涵蓋此刻之後的成交）", flush=True)

    dl = _now().replace(hour=END_HHMM[0], minute=END_HHMM[1], second=0)
    end = time.monotonic() + (60 if a.smoke else
                              (a.minutes * 60 if a.minutes else max(30, (dl - _now()).total_seconds())))
    last = 0.0
    while not _STOP and time.monotonic() < end:
        time.sleep(1)
        if time.monotonic() - last > 300:
            last = time.monotonic()
            print(f"  {_now():%H:%M:%S} 已收 {sum(v['n'] for v in _ACC.values()):,} 筆 / {len(_ACC)} 檔", flush=True)
    try:
        ws.disconnect()
    except Exception:  # noqa: BLE001
        pass
    print(f"\n窗口 {t0:%H:%M:%S} → {_now():%H:%M:%S}", flush=True)
    report(cands)
    _RAW_FH.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
