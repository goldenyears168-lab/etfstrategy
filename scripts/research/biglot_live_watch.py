#!/usr/bin/env python3
"""大戶盤中佈局監看 · 09:00 起收逐筆 · 12:00/13:00/13:30 寄信 · 唯讀無送單.

判準（全部來自 2026-09-02 的回測，見 memory: pit-tick-bigflow-overnight-verdict）
--------------------------------------------------------------------------
· 大戶 = 單筆成交金額 >= 500 萬（2026-09-03 改；原自適應 P99 經 IS-only 重估後證實過擬：
  IS_IC +0.195→OOS +0.097，固定500萬 IS +0.177→OOS +0.100，增量 OOS t 固定1.7 vs 自適應1.3。
  自適應是 100 個配適參數、固定是 0 個，而門檻曲面本來就平）
  註：500萬 的空桶率 <100元股 4.6%、其餘 0%；2000萬/3000萬 低價股空桶率 34%/49%，不用
· 訊號 = (大戶主動買 − 大戶主動賣) / 當日累積量 × 100；Lee-Ready 定內外盤
· 訊號預測的是**隔日開盤跳空**，不是當日走勢（純盤中版本實測無效：+1.9~+9.6 bps 全不顯著）
· 必須從 09:00 起算：全場 IC +0.151，只取 12:00-13:00 掉到 +0.068、且對全場零增量
· 宇宙 = PIT 宇宙 ∩ 期貨日均量>=500 口（兩腿都要能做；個股期貨放空無平盤下限制）
· 離散度是**唯一通過 OOS 的擇時條件**（IS +0.362 / OOS +0.395）：
  離散度低於歷史 P30 的日子訊號弱，報告會標示

連線紀律：獨立 session、單頻道 <=45 訂閱、13:32 自動退出（沿 chip_intraday_lot_probe）。
"""
from __future__ import annotations

import json
import signal
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, "src")

from notify_email import send_alert  # noqa: E402
from order.fubon_session import connect_fubon  # noqa: E402
from stock_db import DATA_DIR, DEFAULT_DB_PATH  # noqa: E402

_TZ = ZoneInfo("Asia/Taipei")
CALIB = DATA_DIR / "cache" / "pit_universe_tick" / "_live_calib.json"
BIG_AMT = 5_000_000        # 大戶門檻：單筆成交金額（元）
OUT_DIR = DATA_DIR.parent / "cache" / "biglot_live_watch"
MAIL_AT = ["12:00", "13:00", "13:30"]
END_HHMM = (13, 32)
TOPN = 10
_STOP = False
_LOCK = threading.Lock()
_RAW = None
_ACC: dict[str, dict] = {}
_LAST: dict[str, float] = {}


def _now():
    return datetime.now(_TZ)


def _sig(signum, _f):
    global _STOP
    _STOP = True
    print(f"signal {signum} -> stopping", flush=True)


def _raw(kind, payload):
    if _RAW is None:
        return
    with _LOCK:
        _RAW.write(json.dumps({"ts": _now().isoformat(), "kind": kind,
                               "payload": payload}, ensure_ascii=False) + "\n")
        _RAW.flush()


def on_message(raw, THR):
    try:
        msg = json.loads(raw)
    except Exception:  # noqa: BLE001
        return
    _raw("message", msg)
    if msg.get("channel") != "trades" or msg.get("event") not in (None, "data"):
        return
    d = msg.get("data") or {}
    sid, px, sz = str(d.get("symbol", "")), d.get("price"), d.get("size")
    if not sid or px is None or sz is None:
        return
    if d.get("isTrial"):                      # 13:25-13:30 試撮不是成交
        return
    if d.get("isContinuous") is not True:      # 收盤競價：單一價，內外盤無意義
        return
    px, sz = float(px), float(sz)
    b, a = d.get("bid"), d.get("ask")
    side = 1 if (a is not None and px >= float(a)) else (
        -1 if (b is not None and px <= float(b)) else 0)
    if side == 0:
        p = _LAST.get(sid)
        side = 0 if p is None else (1 if px > p else (-1 if px < p else 0))
    _LAST[sid] = px
    acc = _ACC.setdefault(sid, {"vol": 0.0, "bb": 0.0, "bs": 0.0, "n": 0,
                                "px0": px, "px1": px})
    acc["vol"] += sz
    acc["n"] += 1
    acc["px1"] = px
    if px * sz * 1000 >= BIG_AMT:      # 用當下價換算，股價漂移自動跟上
        if side > 0:
            acc["bb"] += sz
        elif side < 0:
            acc["bs"] += sz


def _load_vixtwn():
    """VIXTWN 日頻史（唯讀）。回傳 (dates, closes)；資料可能陳舊，呼叫端要標示日期。"""
    import sqlite3
    con = sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT date, close FROM market_vix_daily WHERE symbol='VIXTWN' "
        "AND close IS NOT NULL ORDER BY date").fetchall()
    con.close()
    return [r[0] for r in rows], [float(r[1]) for r in rows]


def count_limit_down(session) -> int | None:
    """全市場跌停家數（REST snapshot，2 個 request）。失敗回 None。"""
    try:
        rest = session.sdk.marketdata.rest_client.stock
        n = 0
        for market in ("TSE", "OTC"):
            resp = rest.snapshot.quotes(market=market)
            data = resp.get("data", resp) if isinstance(resp, dict) else resp
            for q in data or []:
                sid = str(q.get("symbol", ""))
                chg = q.get("changePercent")
                if len(sid) == 4 and sid.isdigit() and chg is not None and chg <= -9.45:
                    n += 1
        return n
    except Exception as exc:  # noqa: BLE001
        print(f"limit-down count failed: {exc!r}", flush=True)
        return None


# 11 年實測 (n=2,813)：VIXTWN 一年分位帶 × 跌停家數帶 → (明日極端左尾<-300bps 機率%, 左尾深度bps)
_GAUGE = {(0, 0): (1.1, -96), (0, 1): (1.4, -108), (0, 2): (None, -125),
          (1, 0): (2.5, -105), (1, 1): (4.0, -128), (1, 2): (12.5, -244),
          (2, 0): (4.6, -129), (2, 1): (11.2, -159), (2, 2): (22.2, -233)}


def risk_gauge(session, vix_hist) -> list[str]:
    """留倉風險儀表（VIXTWN 分位 + 跌停家數 → 歷史對照）。任何失敗都不擋信。"""
    try:
        dates, closes = vix_hist
        if len(closes) < 120:
            return ["【留倉風險儀表】VIXTWN 史料不足，略過"]
        cur = closes[-1]
        win = closes[-252:]
        pctl = sum(1 for x in win if x < cur) / len(win)
        vb = 0 if pctl < 0.5 else (1 if pctl < 0.8 else 2)
        ld = count_limit_down(session)
        lb = None if ld is None else (0 if ld <= 2 else (1 if ld <= 10 else 2))
        vlbl = ["低(<P50)", "中(P50-80)", "高(>P80)"][vb]
        age = (_now().date() - datetime.strptime(dates[-1], "%Y-%m-%d").date()).days
        out = ["【留倉風險儀表】（預告明晚個股左尾多寬，非方向）",
               f"· VIXTWN {cur:.1f} → 一年分位 {vlbl}（資料日 {dates[-1]}"
               + (f"，⚠ 已陳舊 {age} 天" if age > 5 else "") + "）"]
        if lb is None:
            out.append("· 跌停家數：REST 快照失敗，無法判定")
            return out
        prob, depth = _GAUGE[(vb, lb)]
        llbl = ["≤2家", "3-10家", ">10家"][lb]
        out.append(f"· 跌停家數 {ld} → {llbl}")
        if prob is None:
            out.append(f"· 歷史對照：此格樣本不足（左尾約 {depth}bps），保守處理")
        else:
            out.append(f"· 歷史對照：明日極端左尾(<-300bps)機率 {prob:.0f}%、左尾深度約 {depth}bps"
                       f"（全樣本基準 3.8% / -117bps）")
        act = ("正常留倉" if vb == 0 and lb == 0 else
               "不留個股倉（或全對沖）" if vb == 2 and lb == 2 else
               "留倉減半、優先留有期貨可對沖者" if vb == 2 or lb == 2 else "正常留倉，留意尾巴")
        out.append(f"· 建議：{act}")
        out.append("· 但書：風控級非alpha級；效應集中急崩型（2022陰跌年失效）；VIXTWN 資料若陳舊分位會失真")
        return out
    except Exception as exc:  # noqa: BLE001
        return [f"【留倉風險儀表】計算失敗：{exc!r}"]


def build_report(cal, label) -> tuple[str, str]:
    meta = {r["sid"]: r for r in cal["universe"]}
    rows = []
    for sid, a in _ACC.items():
        if a["vol"] < 50 or sid not in meta:
            continue
        rows.append({**meta[sid], "net": a["bb"] - a["bs"],
                     "norm": (a["bb"] - a["bs"]) / a["vol"] * 100,
                     "vol": a["vol"], "n": a["n"],
                     "chg": (a["px1"] / a["px0"] - 1) * 100})
    rows.sort(key=lambda r: -r["norm"])
    disp = float(np.std([r["norm"] for r in rows])) if len(rows) > 5 else 0.0
    p = cal["disp_pct"]
    band = ("高（>P70）" if disp >= p["70"] else
            "中" if disp >= p["30"] else "低（<P30，訊號偏弱）")
    L = [f"【{label} · 大戶佈局】{_now():%Y-%m-%d %H:%M}  涵蓋 {len(rows)}/45 檔",
         f"橫斷面離散度 {disp:.2f} → {band}（歷史 P30={p['30']} 中位={p['50']} P70={p['70']}）",
         "",
         f"{'#':<3}{'代號':<6}{'名稱':<10}{'訊號%':>8}{'大戶淨(張)':>11}{'今日%':>8}{'期貨量':>8}{'跳動bps':>8}"]
    L.append("── 做多前 10 ──")
    for i, r in enumerate(rows[:TOPN], 1):
        L.append(f"{i:<3}{r['sid']:<6}{r['name']:<10}{r['norm']:>+8.1f}{r['net']:>11,.0f}"
                 f"{r['chg']:>+7.2f}%{r['fut_vol']:>8,}{r['tick_bps']:>8.1f}")
    L.append("── 做空前 10 ──")
    for i, r in enumerate(rows[-TOPN:][::-1], 1):
        L.append(f"{i:<3}{r['sid']:<6}{r['name']:<10}{r['norm']:>+8.1f}{r['net']:>11,.0f}"
                 f"{r['chg']:>+7.2f}%{r['fut_vol']:>8,}{r['tick_bps']:>8.1f}")
    L += ["", "【判讀提醒】",
          "· 訊號預測的是隔日開盤跳空，不是今天收盤前的走勢",
          "· 個股層級排名經三種度量檢定皆不可持續（IC/命中率/期望報酬 IS→OOS 相關 ≈0）",
          "  → 這份名單要整組用（前10檔一起），不要只挑其中一兩檔",
          "· 成本是生死線：兩腿各 1 個跳動已是 45 bps，實測價差 1~7 個跳動",
          "  跳動 bps 欄越小越好；華通型（寬價差）即使訊號強也不划算",
          f"· 回測基準：OOS 多空毛 +91~+120 bps/日（t≈4.2），扣 2 個跳動後約打平"]
    body = "\n".join(L)
    return f"大戶佈局 {label} {_now():%m/%d %H:%M}", body


def main() -> int:
    global _RAW
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    cal = json.loads(CALIB.read_text())
    THR = {r["sid"]: r["p99"] for r in cal["universe"]}   # 保留供報表顯示，不再用於分層
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    day = _now().strftime("%Y-%m-%d")
    _RAW = (OUT_DIR / f"raw_{day}.jsonl").open("a", encoding="utf-8")

    session = connect_fubon(realtime=True)
    print(f"session ready · 宇宙 {len(THR)} 檔 · 唯讀", flush=True)
    vix_hist = _load_vixtwn()
    print(f"VIXTWN 史 {len(vix_hist[0])} 日（至 {vix_hist[0][-1] if vix_hist[0] else '無'}）", flush=True)
    ws = session.sdk.marketdata.websocket_client.stock
    ws.on("connect", lambda: _raw("connect", None))
    ws.on("disconnect", lambda c, m: _raw("disconnect", {"code": c, "msg": m}))
    ws.on("error", lambda e: _raw("error", str(e)))
    ws.on("message", lambda raw: on_message(raw, THR))
    ws.connect()
    print(f"ws connected auth={getattr(ws, 'auth_status', '?')}", flush=True)
    for sid in THR:
        try:
            ws.subscribe({"channel": "trades", "symbol": sid})
        except Exception as exc:  # noqa: BLE001
            _raw("subscribe_error", {"symbol": sid, "error": str(exc)})
    print(f"subscribed {len(THR)} × trades", flush=True)

    sent = set()
    while not _STOP:
        time.sleep(2)
        hm = _now().strftime("%H:%M")
        if hm >= f"{END_HHMM[0]:02d}:{END_HHMM[1]:02d}":
            break
        for t in MAIL_AT:
            if hm >= t and t not in sent:
                sent.add(t)
                try:
                    sub, body = build_report(cal, t)
                    body += "\n\n" + "\n".join(risk_gauge(session, vix_hist))
                    send_alert(sub, body)
                    print(f"mailed {t} ({len(_ACC)} syms)", flush=True)
                except Exception as exc:  # noqa: BLE001
                    print(f"mail {t} failed: {exc!r}", flush=True)
    try:
        ws.disconnect()
    except Exception:  # noqa: BLE001
        pass
    try:
        sub, body = build_report(cal, "收盤")
        body += "\n\n" + "\n".join(risk_gauge(session, vix_hist))
        send_alert(sub + " (收盤)", body)
    except Exception as exc:  # noqa: BLE001
        print(f"final mail failed: {exc!r}", flush=True)
    _RAW.close()
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
