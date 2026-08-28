#!/usr/bin/env python3
"""盤中單量結構探針（chip-loud-accum-forward 的儀器層）· 唯讀 · 無任何送單路徑.

目的：高調吸貨訊號的兌現點在 T+1 開盤集合競價（跳空 +0.75%），21:00 拿分點資料
永遠站在跳空之後。本探針在 12:50 掃出「漲>=3% 且放量」的候選（≈今晚會進名單的
超集合），開**獨立的第三條** Fubon 行情 websocket 訂閱逐筆，記錄 12:50~13:35
（含收盤集合競價）的每筆單量 → 均張／大單佔比。之後與當晚 22:15 落庫的
buy/sell 家數差對照，驗證「盤中能否判別集中型」。驗證通過前**不做任何交易用途**。

連線紀律（2026-08-21 教訓：108 個訂閱撞上限打掉指數組 52% 資料）：
獨立 session、獨立 websocket、訂閱數上限 45×1 頻道；一次性行程 13:35 自動退出。

訊息格式未知原則（沿 momentum_rotation_websocket_trades_shadow.py）：
原始訊息一律先落 jsonl，彙總欄位用 .get 容錯，不假設 schema。

輸出：${GOLDENSTOCKS_DATA_DIR}/cache/chip_lot_probe/raw_{date}.jsonl
      ${GOLDENSTOCKS_DATA_DIR}/cache/chip_lot_probe/summary_{date}.csv

用法：
    PYTHONPATH=src .venv-fubon/bin/python scripts/research/chip_intraday_lot_probe.py
    PYTHONPATH=src .venv-fubon/bin/python scripts/research/chip_intraday_lot_probe.py --smoke
"""
from __future__ import annotations

import argparse
import csv
import json
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")

from order.fubon_session import connect_fubon  # noqa: E402  (collector 慣例，見 collect_*_books)
from stock_db import DATA_DIR, DEFAULT_DB_PATH  # noqa: E402

_TZ = ZoneInfo("Asia/Taipei")
OUT_DIR = DATA_DIR.parent / "cache" / "chip_lot_probe"
MAX_SYMBOLS = 45          # 訂閱預算（單頻道），遠低於撞牆的 108
CHG_MIN = 3.0             # 高調門檻，與凍結規格一致
VOLRATIO_MIN = 1.0        # 12:50 累積量已達 20 日均量（放量）
END_HHMM = (13, 35)       # 收盤集合競價 13:30 之後留 5 分鐘收尾
_STOP = False
_LOCK = threading.Lock()
_RAW_FH = None
_MSG_COUNT = 0


def _on_signal(signum, _frame):
    global _STOP
    _STOP = True
    print(f"signal {signum} -> stopping", flush=True)


def _now() -> datetime:
    return datetime.now(tz=_TZ)


def _raw_write(kind: str, payload) -> None:
    global _MSG_COUNT
    rec = {"ts": _now().isoformat(timespec="milliseconds"), "kind": kind, "payload": payload}
    with _LOCK:
        if _RAW_FH is not None:
            _RAW_FH.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        _MSG_COUNT += 1


def _db_universe() -> dict[str, float]:
    """近 20 交易日均量（股），凍結宇宙的流動性濾網。唯讀。"""
    c = sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)
    rows = c.execute(
        """SELECT stock_id, AVG(volume) FROM (
             SELECT stock_id, trade_date, volume, ROW_NUMBER() OVER (
               PARTITION BY stock_id, trade_date ORDER BY CASE source
                 WHEN 'twse_mi_index' THEN 0 WHEN 'tpex_daily' THEN 1
                 WHEN 'finmind' THEN 2 ELSE 3 END) rn
             FROM stock_daily_bars
             WHERE trade_date >= date('now', '-40 day'))
           WHERE rn=1 GROUP BY stock_id""").fetchall()
    c.close()
    return {sid: float(v or 0) for sid, v in rows}


def scan_movers(session) -> list[dict]:
    """Fubon REST snapshot 全市場掃描 → 高調放量候選（<=MAX_SYMBOLS）。"""
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
    av20 = _db_universe()
    cands = []
    for q in quotes:
        sid = str(q.get("symbol", ""))
        if not sid or len(sid) != 4 or not sid.isdigit():
            continue
        chg = q.get("changePercent")
        px = q.get("closePrice") or q.get("lastPrice")
        vol = q.get("tradeVolume") or 0            # 累積成交量（張）
        if chg is None or px is None or px < 10 or chg < CHG_MIN:
            continue
        base = av20.get(sid, 0) / 1000             # 股 → 張
        if base < 300 or vol < base * VOLRATIO_MIN:
            continue
        cands.append({"symbol": sid, "chg": chg, "vol": vol,
                      "value": q.get("tradeValue") or 0})
    cands.sort(key=lambda x: -x["value"])
    return cands[:MAX_SYMBOLS]


def aggregate(raw_path: Path, out_csv: Path) -> None:
    """容錯彙總：每檔 n 筆、總張、均張、大單(>=50張)佔比。"""
    stats: dict[str, dict] = {}
    with raw_path.open(encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if rec.get("kind") != "message":
                continue
            p = rec.get("payload") or {}
            if p.get("channel") != "trades" or p.get("event") not in (None, "data"):
                continue
            d = p.get("data") or {}
            sid, size = str(d.get("symbol", "")), d.get("size")
            if not sid or size is None:
                continue
            s = stats.setdefault(sid, {"n": 0, "vol": 0.0, "big": 0.0})
            s["n"] += 1
            s["vol"] += size
            if size >= 50:
                s["big"] += size
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "n_prints", "total_lots", "avg_lot", "big50_share"])
        for sid, s in sorted(stats.items()):
            w.writerow([sid, s["n"], round(s["vol"], 1),
                        round(s["vol"] / s["n"], 2) if s["n"] else 0,
                        round(s["big"] / s["vol"], 4) if s["vol"] else 0])
    print(f"summary → {out_csv}（{len(stats)} 檔有成交流）", flush=True)


def main() -> int:
    global _RAW_FH
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true",
                    help="安裝驗證：登入+掃描+訂閱2檔，30秒後退出")
    a = ap.parse_args()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    day = _now().strftime("%Y-%m-%d")
    raw_path = OUT_DIR / f"raw_{day}{'_smoke' if a.smoke else ''}.jsonl"
    _RAW_FH = raw_path.open("a", encoding="utf-8")

    session = connect_fubon(realtime=True)
    print("fubon session ready（獨立第三條連線 · 唯讀）", flush=True)

    cands = scan_movers(session)
    _raw_write("candidates", cands)
    print(f"候選 {len(cands)} 檔: {[c['symbol'] for c in cands]}", flush=True)
    if a.smoke:
        cands = cands[:2] or [{"symbol": "2330"}, {"symbol": "2317"}]
    if not cands:
        print("無高調放量候選（休市或安靜日），直接收工", flush=True)
        _RAW_FH.close()
        return 0

    ws = session.sdk.marketdata.websocket_client.stock
    ws.on("connect", lambda: _raw_write("connect", None))
    ws.on("disconnect", lambda code, msg: _raw_write("disconnect", {"code": code, "msg": msg}))
    ws.on("error", lambda err: _raw_write("error", str(err)))
    ws.on("authenticated", lambda msg: _raw_write("authenticated", msg))
    ws.on("message", lambda raw: _raw_write("message", json.loads(raw)))
    ws.connect()
    print(f"ws connected auth={getattr(ws, 'auth_status', '?')}", flush=True)

    for c in cands:
        try:
            ws.subscribe({"channel": "trades", "symbol": c["symbol"]})
        except Exception as exc:  # noqa: BLE001
            _raw_write("subscribe_error", {"symbol": c["symbol"], "error": str(exc)})
    print(f"subscribed {len(cands)} × trades（訂閱預算 {MAX_SYMBOLS}）", flush=True)

    deadline = _now().replace(hour=END_HHMM[0], minute=END_HHMM[1], second=0)
    if a.smoke:
        deadline = min(deadline, _now()) if _now() > deadline else _now()
    end_ts = time.monotonic() + (30 if a.smoke else
                                 max(60, (deadline - _now()).total_seconds()))
    while not _STOP and time.monotonic() < end_ts:
        time.sleep(1.0)

    try:
        ws.disconnect()
    except Exception:  # noqa: BLE001
        pass
    with _LOCK:
        _RAW_FH.close()
        _RAW_FH = None
    print(f"collected {_MSG_COUNT} 條紀錄 → {raw_path}", flush=True)
    if not a.smoke:
        aggregate(raw_path, OUT_DIR / f"summary_{day}.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
