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
import math
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
RETAIL_LOTS = 1            # 散戶門檻：單筆張數（固定金額門檻對高價股失效，1 張是唯一不受股價影響的定義）
# 排除族群。證據（全部在同一把尺：13:30收盤競價進 → 隔日09:00開盤競價出、45檔期貨宇宙、
# 固定基準＝對全宇宙均值）：
#   · 入選個股本身超額：A 版 -50.1bps(t=-1.58)、BIG 版 -42.2(t=-1.63)，前後半同號
#   · 逐日配對「剔除−保留」：A +3.3(t=1.23) / BIG +5.4(t=1.87) / 同向格 +4.5(t=0.86) /
#     同向格收緊 +5.6(t=1.07) —— 四個配置全部為正，勝日也全部上升
#   · 只看有金融入榜的日子：A +7.0(t=1.23)、BIG +9.0(t=1.89)；BIG 版 >=3 檔金融那 17 天 +20.7
#   · 機制：20日波動中位 196bps＝全宇宙 448bps 的 44%，低波動股要擠進前段需異常大的大戶淨流，
#     而該流量在低波動股上推不動價格 → 入選即誤報
# ⚠ 單一檢定皆未達 t=2（有金融入榜的日子只有 60~75 天），但六個角度同號、無反例。
# 2026-09-03 註：初版誤判為「無證據支持剔除」，成因是剔除後又用剩餘股票重算基準（同時換了
# 標的與標尺）。改用固定基準後符號翻正。
WEAK_CATS = {"金融保險"}
OUT_DIR = DATA_DIR.parent / "cache" / "biglot_live_watch"
MAIL_AT = ["12:00", "13:00", "13:30"]
EARLY_MAIL_AT = ["09:15", "09:35"]   # 早盤觀察信（paper-trade 前瞻紀錄，2026-09-03 起）
_PCLOSE: dict[str, float] = {}       # 昨收（算今晨跳空與漲停價）
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
                                "rb": 0.0, "rs": 0.0, "px0": px, "px1": px})
    acc["vol"] += sz
    acc["n"] += 1
    acc["px1"] = px
    if px * sz * 1000 >= BIG_AMT:      # 用當下價換算，股價漂移自動跟上
        if side > 0:
            acc["bb"] += sz
        elif side < 0:
            acc["bs"] += sz
    if sz == RETAIL_LOTS:              # 散戶＝單筆 1 張（最小交易單位）
        if side > 0:
            acc["rb"] += sz
        elif side < 0:
            acc["rs"] += sz


def _load_vixtwn():
    """VIXTWN 日頻史（唯讀）。回傳 (dates, closes)；資料可能陳舊，呼叫端要標示日期。"""
    import sqlite3
    con = sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)
    # 同一 date 有 finmind / computed 兩列（5,938 列 / 5,821 個日期），不去重會讓
    # 近期重複日在 252 日視窗內佔兩格、擠掉更早的資料，分位失真（2026-09-03 實測 2.0% vs 1.6%）
    rows = con.execute(
        "SELECT date, AVG(close) FROM market_vix_daily WHERE symbol='VIXTWN' "
        "AND close IS NOT NULL GROUP BY date ORDER BY date").fetchall()
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


def _ticksz(p: float) -> float:
    return 0.01 if p < 10 else 0.05 if p < 50 else 0.1 if p < 100 else \
        0.5 if p < 500 else 1.0 if p < 1000 else 5.0


def _limit_up(pc: float) -> float:
    raw = pc * 1.10
    tk = _ticksz(raw)
    return math.floor(raw / tk + 1e-9) * tk


def _load_prev_close(sids) -> dict[str, float]:
    """昨收（唯讀 DB）。失敗回空 dict，早盤信少掉跳空欄但不擋信。"""
    import sqlite3
    from datetime import timedelta
    try:
        con = sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)
        since = (_now().date() - timedelta(days=12)).isoformat()
        today = _now().strftime("%Y-%m-%d")
        q = ("SELECT stock_id, trade_date, close FROM stock_daily_bars "
             f"WHERE trade_date >= ? AND stock_id IN ({','.join('?' * len(sids))}) "
             "ORDER BY trade_date")
        out: dict[str, float] = {}
        for sid, d, c in con.execute(q, [since] + list(sids)):
            if d < today and c:
                out[sid] = float(c)   # 依日期序覆寫 → 留下最近一個昨收
        con.close()
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"prev close load failed: {exc!r}", flush=True)
        return {}


def early_report(cal, label) -> tuple[str, str]:
    """早盤四欄觀察信（09:15 / 09:35 · paper-trade 前瞻紀錄 · 不下單）。

    2026-09-03 凍結規格：各欄皆跳過已鎖漲停、排除金融；出場＝隔日 09:00 開盤競價。
    2026-09-04 增 ④ 大戶−散戶(B−R)：早盤散戶是反指標（追高盤，IS t≈−1.7），故用減法不用同向；
    回測（09:30版/126日）全期毛 +63.7、IS +88.9 → OOS 絕對 +15.4（超額 +24.6/t=3.1 但
    載具 carry 7-9 月轉負）——與其餘欄同一凍結判準裁決，不另立標準。
    裁決判準（寫死）：累積 ≥40 交易日後，日均毛 ≥+35bps 且勝日 ≥60% 的欄位才續命。
    回測參考（09:30 版 · 126 日）：①A +29.4 ②同業相對 +39.1 ③A+今晨跳空 +33.3 bps/日；
    後半樣本 +15.6 / +14.6 / +29.7 —— 期望值請按後半看，成本約 27bps。
    """
    meta = {r["sid"]: r for r in cal["universe"]}
    rows = []
    for sid, a in _ACC.items():
        m = meta.get(sid)
        if m is None or a["vol"] < 30 or m.get("cat") in WEAK_CATS:
            continue
        pc = _PCLOSE.get(sid)
        lu = _limit_up(pc) if pc else None
        tk = _ticksz(a["px1"])
        rows.append({"sid": sid, "name": m["name"], "cat": m.get("cat", "?"),
                     "A": (a["bb"] - a["bs"]) / a["vol"] * 100, "px": a["px1"],
                     "retail": (a.get("rb", 0.0) - a.get("rs", 0.0)) / a["vol"] * 100,
                     "gap": (a["px0"] / pc - 1) * 1e4 if pc else None,
                     "dlim": (lu / a["px1"] - 1) * 100 if lu else None,
                     "locked": bool(lu is not None and a["px1"] >= lu - tk * 0.5)})
    if len(rows) < 15:
        return f"大戶早盤觀察 {label} {_now():%m/%d}", f"覆蓋不足（{len(rows)} 檔），本次略過"
    from collections import defaultdict
    byc = defaultdict(list)
    for r in rows:
        byc[r["cat"]].append(r["A"])
    for r in rows:
        v = byc[r["cat"]]
        r["indrel"] = r["A"] - sum(v) / len(v) if len(v) >= 4 else r["A"]
    av = sorted(r["A"] for r in rows)
    gv = sorted(r["gap"] for r in rows if r["gap"] is not None)
    pct = lambda arr, x: sum(1 for y in arr if y <= x) / len(arr)
    for r in rows:
        r["scoreC"] = (pct(av, r["A"]) + pct(gv, r["gap"])) if (r["gap"] is not None and gv) else None
    rv = sorted(r["retail"] for r in rows)
    for r in rows:
        # ④ 早盤散戶=反指標 → 減法（大戶rank − 散戶rank），非同向 AND
        r["scoreD"] = pct(av, r["A"]) - pct(rv, r["retail"])
    cols = [("① A 原始", "A", sorted(rows, key=lambda r: -r["A"])),
            ("② 同業相對", "indrel", sorted(rows, key=lambda r: -r["indrel"])),
            ("③ A+今晨跳空", "scoreC",
             sorted([r for r in rows if r["scoreC"] is not None], key=lambda r: -r["scoreC"])),
            ("④ 大戶−散戶(B−R)", "scoreD", sorted(rows, key=lambda r: -r["scoreD"]))]
    L = [f"【{label} · 早盤觀察】{_now():%Y-%m-%d %H:%M}  覆蓋 {len(rows)} 檔（已排除金融）",
         "paper-trade 前瞻紀錄 · 不下單。進場＝收信市價；出場＝隔日 09:00 開盤集合競價（市價平倉）",
         ""]
    for title, key, arr in cols:
        skipped = [r["name"] for r in arr[:8] if r["locked"]]
        picked = [r for r in arr if not r["locked"]][:8]
        L.append(f"── {title} 前 8（另有 {len(skipped)} 檔已鎖漲停跳過：{'、'.join(skipped) or '無'}）──")
        L.append(f"{'#':<3}{'代號':<6}{'名稱':<10}{'分數':>8}{'現價':>9}{'距漲停%':>9}{'今晨跳空':>9}")
        for i, r in enumerate(picked, 1):
            gp = f"{r['gap']:+8.0f}" if r["gap"] is not None else "     n/a"
            dl = f"{r['dlim']:+8.1f}" if r["dlim"] is not None else "     n/a"
            L.append(f"{i:<3}{r['sid']:<6}{r['name']:<10}{r[key]:>+8.2f}{r['px']:>9.2f}{dl:>9}{gp:>9}")
        L.append("")
    L += ["【凍結判準】≥40 交易日後裁決：日均毛 ≥+35bps 且勝日 ≥60% → 續命；",
          "  +15~+35 且 t≥1.5 → 延長至 80 日再裁一次；其餘 → 收攤",
          "  （40 日的偵測下限：SE≈17bps，真值 +25 也有七成機率過不了 +35——「未過」≠「證明為零」）",
          "⚠ 寬價差股（跳動>35bps，如南電）live 與回測的內外盤判定可差 20 點、散戶欄可翻號",
          "  （09-03 八檔對帳：整體 Spearman 0.95，唯 8046 大幅分歧）；該類股名次僅供參考",
          "回測參考（09:30版/126日）：① +29.4 ② +39.1 ③ +33.3 ④ +63.7 bps/日；後半 +15.6/+14.6/+29.7/+15.4；成本≈27bps",
          "  ④ 的散戶腿對寬價差股（跳動>35bps）翻號風險加倍，該類股在 ④ 的名次特別不可信",
          "⚠ 期望值按後半看；訊號僅 15~35 分鐘資料、比 13:00 版吵；四欄擇一是裁決的事，勿現在就挑"]
    return f"大戶早盤觀察 {label} {_now():%m/%d}", "\n".join(L)


def _weak(r) -> str:
    """低效族群標記（只影響顯示，不影響排序與門檻）。"""
    return "!" if r.get("cat") in WEAK_CATS else ""


def concord_block(rows) -> list[str]:
    """大戶 ∩ 散戶 同向格（2026-09-03 新增 · 觀察用，不取代上方 A 名單）。

    判準來自 100 檔 × 126 日回測（13:00 截點 → 隔日 09:00-09:05 開盤，期貨可交易宇宙）：
    · 大戶買⅓ ∩ 散戶買⅓ = +62.4bps/日（t=4.66、勝日 66%、區塊自助 P(>跨價差成本)=90%）
    · 大戶買⅓ ∩ 散戶賣⅓ = +6.2bps（t=0.73）—— 「大戶買散戶賣」沒有訊號
    · 中間層（非大戶非散戶）獨立係數 t=0.50，可整層忽略
    · corr(大戶, 散戶)=-0.062，兩者近乎獨立，同向本身即低機率事件
    ⚠ 集中度高（每日 3~5 檔）、SD 是 A 名單的 2.4 倍、僅 126 日樣本；先觀察對照，勿放大部位。
    """
    ok = [r for r in rows if r.get("big_imb") is not None and r.get("ret_imb") is not None
          and r.get("cat") not in WEAK_CATS]
    if len(ok) < 20:
        return ["", "【大戶 ∩ 散戶 同向】可用樣本不足，略過"]
    bs = sorted(r["big_imb"] for r in ok)
    rs = sorted(r["ret_imb"] for r in ok)
    # 與回測一致：等同 pandas rank(pct=True)（含自身的最大名次 / n）
    pct = lambda arr, v: sum(1 for x in arr if x <= v) / len(arr)
    for r in ok:
        r["rb_"] = pct(bs, r["big_imb"])
        r["rr_"] = pct(rs, r["ret_imb"])
        r["s_"] = r["rb_"] + r["rr_"]
    lng = sorted([r for r in ok if r["rb_"] >= 0.5 and r["rr_"] >= 2 / 3],
                 key=lambda r: -r["s_"])
    sht = sorted([r for r in ok if r["rb_"] <= 0.5 and r["rr_"] <= 1 / 3],
                 key=lambda r: r["s_"])
    L = ["", "【大戶 ∩ 散戶 同向】（觀察組 · 不取代上方名單 · 判準見程式註解）",
         f"{'#':<3}{'代號':<6}{'名稱':<10}{'大戶imb':>9}{'散戶imb':>9}"
         f"{'大戶淨':>9}{'散戶淨':>9}{'開盤起%':>8}{'跳動bps':>8}"]
    for tag, arr in (("多方（大戶買≥P50 ∩ 散戶買≥P67）", lng),
                     ("空方（大戶賣≤P50 ∩ 散戶賣≤P33）", sht)):
        L.append(f"── {tag}：{len(arr)} 檔通過 ──")
        if not arr:
            L.append("   （今日無標的通過閘門）")
        for i, r in enumerate(arr[:5], 1):
            L.append(f"{i:<3}{r['sid']:<6}{r['name'] + _weak(r):<12}{r['big_imb']:>+9.3f}{r['ret_imb']:>+9.3f}"
                     f"{r['net']:>9,.0f}{r['ret_net']:>9,.0f}{r['chg']:>+7.2f}%{r['tick_bps']:>8.1f}")
    L.append("· 同向＝大戶與散戶同時偏買（或同時偏賣）；「大戶買、散戶賣」實測無訊號（t=0.73）")
    L.append("· ⚠ 09-03 補驗：同向格入選者 55.7% 收盤鎖漲停（買不到）；跳過鎖死後僅 +13.6bps")
    L.append("  (t=1.01、月正3/6) —— 散戶同向其實大半是「漲停偵測器」。此區塊同屬觀察性，非可執行")
    L.append("· 樣本 126 日、每日僅 3~5 檔，單股事件風險大；請與上方 A 名單並行觀察後再判斷")
    return L


def build_report(cal, label) -> tuple[str, str]:
    meta = {r["sid"]: r for r in cal["universe"]}
    rows = []
    for sid, a in _ACC.items():
        if a["vol"] < 50 or sid not in meta:
            continue
        bl = a["bb"] + a["bs"]
        rl = a.get("rb", 0.0) + a.get("rs", 0.0)
        rows.append({**meta[sid], "net": a["bb"] - a["bs"],
                     "norm": (a["bb"] - a["bs"]) / a["vol"] * 100,
                     "vol": a["vol"], "n": a["n"],
                     "big_imb": (a["bb"] - a["bs"]) / bl if bl else None,
                     "ret_imb": (a.get("rb", 0.0) - a.get("rs", 0.0)) / rl if rl else None,
                     "ret_net": a.get("rb", 0.0) - a.get("rs", 0.0),
                     "chg": (a["px1"] / a["px0"] - 1) * 100})
    excluded = [r for r in rows if r.get("cat") in WEAK_CATS]
    rows = [r for r in rows if r.get("cat") not in WEAK_CATS]
    rows.sort(key=lambda r: -r["norm"])
    disp = float(np.std([r["norm"] for r in rows])) if len(rows) > 5 else 0.0
    p = cal["disp_pct"]
    band = ("高（>P70）" if disp >= p["70"] else
            "中" if disp >= p["30"] else "低（<P30，訊號偏弱）")
    ROLE = {"12:00": "觀察（訊號未定，勿據此下單）",
            "13:00": "預告（訊號已近定型，可開始準備）",
            "13:30": "★可執行：現貨收盤競價已撮合、訊號定型；個股期貨尚有 13:30–13:45 可下單",
            "收盤": "對帳（盤後回顧，非交易用）"}
    L = [f"【{label} · 大戶佈局】{_now():%Y-%m-%d %H:%M}  涵蓋 {len(rows)}/45 檔",
         f"用途：{ROLE.get(label, '—')}",
         f"橫斷面離散度 {disp:.2f} → {band}（歷史 P30={p['30']} 中位={p['50']} P70={p['70']}）",
         "",
         f"{'#':<3}{'代號':<6}{'名稱':<10}{'訊號%':>8}{'大戶淨(張)':>11}{'今日%':>8}{'期貨量':>8}{'跳動bps':>8}"]
    L.append("── 做多前 10 ──")
    for i, r in enumerate(rows[:TOPN], 1):
        L.append(f"{i:<3}{r['sid']:<6}{r['name'] + _weak(r):<12}{r['norm']:>+8.1f}{r['net']:>11,.0f}"
                 f"{r['chg']:>+7.2f}%{r['fut_vol']:>8,}{r['tick_bps']:>8.1f}")
    L.append("── 做空前 10 ──")
    for i, r in enumerate(rows[-TOPN:][::-1], 1):
        L.append(f"{i:<3}{r['sid']:<6}{r['name'] + _weak(r):<12}{r['norm']:>+8.1f}{r['net']:>11,.0f}"
                 f"{r['chg']:>+7.2f}%{r['fut_vol']:>8,}{r['tick_bps']:>8.1f}")
    if excluded:
        ex = sorted(excluded, key=lambda r: -abs(r["norm"]))[:6]
        L.append(f"· 已排除 {'／'.join(sorted(WEAK_CATS))} {len(excluded)} 檔（不參與上方排名）："
                 + "、".join(f"{r['name']}{r['norm']:+.1f}" for r in ex))
        L.append("  理由：入選個股本身超額 -50.1bps(t=-1.58)；逐日配對剔除後 A +3.3 / BIG +5.4(t=1.87) /"
                 " 同向格收緊 +5.6，四配置全正；20日波動僅全宇宙 44%，入選多為分母偏小造成的誤報")
    L += concord_block(rows)
    L += ["", "【判讀提醒】",
          "· 訊號預測的是隔日開盤跳空，不是今天收盤前的走勢",
          "· 個股層級排名經三種度量檢定皆不可持續（IC/命中率/期望報酬 IS→OOS 相關 ≈0）",
          "  → 這份名單要整組用（前10檔一起），不要只挑其中一兩檔",
          "· 逐檔訊號強弱有 90.6% 是量測噪音（真變異 τ=0.032 vs 噪音 σ=0.098），",
          "  「這檔特別準」的說法沒有統計基礎；歷史 t 值篩選在樣本外反而更差",
          "",
          "【回測基準】2026-09-03 重驗（100 檔 ×126 日，期貨可交易 45 檔子集）",
          "· 路徑：訊號 09:00–13:00 → 今日 13:30 收盤集合競價進 → 隔日 09:00 開盤集合競價出",
          "  兩端皆單一價格、不跨買賣價差；多腿前 20% 等權",
          "· 成績：多腿超額 +50.5bps/日（t=6.81）、Sharpe 9.40、勝日 73%、逐月 6/6 為正",
          "  區塊自助 95%CI [+33,+59]；扣期貨稅費 8bps 後 P(為正)=100%、扣現股 38.6bps 後 87%",
          "· 出場點越晚越差：開盤競價 +51.8 → 09:05VWAP +46.8 → 10:00 +43.3 → 隔日收盤 +49.4(SD 大 1.6 倍)",
          "· 訊號截點 12:00–13:25 是平台（13:25 減 13:00 = -4.4bps, t=-1.37，無差異）；",
          "  09:30 截點明顯較差（+35.0），早收不划算",
          "⚠ 可成交性（09-03 補驗·決定性）：多腿 Q5 有 31.3% 收盤鎖漲停 —— 收盤競價買不到，",
          "  而帳面 alpha 幾乎全在那批（鎖死組 +112bps vs 可成交組 +23.9bps, t=0.86）。",
          "  實戰模擬（跳過鎖死、遞補次名取前8）僅 +15.7bps/日(t=1.71)、CI[-2,+35]、",
          "  P(>掛限價成本27bps)=13% → 本名單定位為【觀察性報告】：訊號統計上為真，",
          "  但主要是漲停鎖死的代理，多腿不可執行；期貨也救不了（期貨價會先反映鎖死需求）",
          "⚠ 空腿絕對報酬為負：樣本期市場隔夜平均 +75bps，空腿標的仍平均跳 +30 → 裸空腿在此",
          "  regime 虧錢；為正的是對沖後價差或超額，不是放空本身",
          "· 日層級擇時：絕對分數門檻與當日成績無關(ρ=+0.06,p=0.54)；唯一有效的是上方",
          "  「橫斷面離散度」(ρ=+0.22,p=0.01)，離散度低於 P30 的日子訊號弱",
          "⚠ 樣本僅 126 日、單一多頭 regime；空腿在現股上因平盤下不得放空而做不了"]
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
    _PCLOSE.update(_load_prev_close(list(THR)))
    print(f"prev close loaded {len(_PCLOSE)}/{len(THR)}", flush=True)
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
        for t in EARLY_MAIL_AT + MAIL_AT:
            if hm >= t and t not in sent:
                sent.add(t)
                try:
                    if t in EARLY_MAIL_AT:
                        sub, body = early_report(cal, t)
                    else:
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
