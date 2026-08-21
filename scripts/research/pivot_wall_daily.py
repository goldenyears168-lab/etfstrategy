#!/usr/bin/env python3
"""每日累積：主要轉折處到底有沒有牆（order book wall）——附對照組與反向檢定。

為什麼是每天跑而不是一次跑完
----------------------------
2026-08-20 用單一夜盤（MXF 8 小時）量到一個誘人的結果：58 個 60 點轉折處，
防守側最大單檔量 ≥3× 前 10 分鐘基準的比例是 **46.6%**，而 800 個隨機非轉折
時刻只有 **12.2%**。但那個數字幾乎是套套邏輯——價格會停在有量的地方。

真正可交易的是反向條件 P(轉折｜有牆)。同一晚的反向檢定表面上也很好
（未來 300 秒：買方牆 +4.54 點 / 賣方牆 −18.81 / 沒有牆 +0.19），但一加上
波動分層就塌了：低波動與中波動下**買方牆比沒有牆還糟**，整個效果只活在最高
波動的那 1/3。而且 242–362 個樣本來自自相關序列、5 分鐘視窗互相重疊，
有效獨立事件只有十幾到幾十個。

所以這支的工作不是「再算一次」，是**每天把同一組數字追加進 ledger**，
讓「以日為叢集」變成可能。判準沿用 2026-08-20 三輪調查付代價換到的：
對照組必備、波動分層、以日為叢集、跨日一致性優先於任何 p 值。

**這支唯讀，沒有任何送單路徑。**

用法
----
    # 處理前一個日曆日（launchd 每日 06:00 的預設行為）
    PYTHONPATH=src .venv/bin/python scripts/research/pivot_wall_daily.py

    # 指定日期與商品
    PYTHONPATH=src .venv/bin/python scripts/research/pivot_wall_daily.py \\
        --day 2026-08-20 --roots TMF,MXF,TXF

    # 看累積結果
    PYTHONPATH=src .venv/bin/python scripts/research/pivot_wall_daily.py --report
"""
from __future__ import annotations

import argparse
import bisect
import json
import random
import statistics as st
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
MAX_BOOK_AGE_SEC = 5.0
#: ZigZag 轉折門檻（點）。60 點在微台/小台的夜盤約可抓到每小時數個轉折。
PIVOT_TH = 60.0
#: 牆＝該側最大單檔量 ≥ 前 N 分鐘同一量的中位數 × RATIO
BASE_MINUTES = 10
WALL_RATIOS = (2.0, 3.0, 5.0)
#: 轉折前後取簿子的窗口（秒）。含 +3 秒是為了容忍兩條 feed 的時鐘偏移
#: （實測交易所比本機快約 0.4–0.5 秒）。
LOOK_BACK_SEC = 45.0
LOOK_FWD_SEC = 3.0
FWD_HORIZONS = (60, 300, 600)

# --- 牆事件 → 轉折 的分辨（2026-08-20 夜盤歸納出來的假設）------------------
# 今晚 MXF 132 個牆事件：薄牆（ratio 低三分位）轉折率 63.6%、厚牆（高三分位）
# 40.9%，落差 −22.7pp，兩側同號、前後半夜同號。方向與「找最厚的牆去掛單」相反，
# 但與另兩份獨立資料一致：5 天 TMF 五檔研究的「牆越厚擋得越少」（−6.63→−3.43pp
# 單調遞減），以及「≥5× 巨牆 62% 的移除是成交造成、普通檔只有 14%」——厚牆是被
# 吃掉的，不是被尊重的。
# 但關鍵的增量檢定（控制區間位置後，薄買方牆 +14.7pp）只有 n=14 ≈ 1.1σ。
# 所以這裡只負責**每天記一次**，讓「薄牆勝過厚牆」跨 session-day 累積成可判的證據。
#: 轉折＝先觸及對該牆有利方向的障礙（first-passage，不用平均報酬，避免被單邊尾巴拉走）
TURN_BARRIER_PTS = 20.0
TURN_HORIZON_SEC = 300.0
#: 牆事件門檻與去重冷卻——同一堵牆連續滿足條件不重複計數
WALL_EVENT_RATIO = 3.0
WALL_COOLDOWN_SEC = 60.0

# --- 覆蓋率斷言（2026-08-21 事故）------------------------------------------
# 收集器當天 11:07:58 斷線，日盤 300 分鐘只收到 143 分鐘（52% 掉了）。而原本的
# 門檻只有「成交 ≥500 筆、五檔 ≥2000 列」——半截的 session 照樣過關，會被當成完整
# session 記進 ledger。這很致命，因為整套判準靠的正是**跨 session-day 一致性**：
# 混進半截 session 等於在證據裡摻雜訊，而且看不出來。
#
# 更陰險的是偵測方式：一開始我用「相鄰兩列間距 > 60 秒」找缺口，回報 0 缺口——
# 因為資料是**截斷**不是中斷，不存在「兩列之間」。檔案 mtime 還是新的（重連後的
# 凍結重送 stale=true 照樣寫入）。所以這裡改成量**實際覆蓋到的分鐘數**，不是量缺口。
#: 日盤 08:45–13:45＝300 分鐘；夜盤 15:00–05:00＝840 分鐘
SESSION_MINUTES = {"day": 300, "night": 840}
#: 低於這個覆蓋率就標記 partial；仍然寫進 ledger（資料有價值），但彙總時排除
MIN_COVERAGE_PCT = 80.0

# --- 破牆研究（2026-08-21 預先登記）-----------------------------------------
# 指數期貨與個股期貨的破牆行為**符號相反**，所以同一份 ledger 要能分開判：
#   MXF 破牆後 30 秒續行 −0.77 點（n=444）→ 逆勢，買賣力用盡
#   CCF 破牆後 30 秒續行 +21.0 bps（n=25）→ 順勢，真實供需耗竭
# 牆撐的時間也差兩個數量級：MXF 中位 3.4 秒 / CCF 中位 282 秒。個股期貨那邊
# 20 秒對帳迴圈綽綽有餘，不需要動 live worker、不會撞 API 保險絲。
#
# 使用者指名的三個濾網（**預先登記，不是事後挖掘**）：
#   ① 量縮：破牆前 3 秒成交量 / 前 60 秒的每 3 秒均量 < 1
#   ② 事前盤整：破牆前 60 秒的 mid 區間偏小
#   ③ 外部同方向：破牆方向與另一個**低相關**商品的 5 秒動能同號
#
# ③ 的參照必須是非恆等式的。實測 5 秒報酬相關（對 MXF）：
#   TXF 0.989 / TMF 0.982 ❌恆等式  ·  EXF 0.295 / CCF 0.151 / SOF 0.113
#   / SPF 0.048 / SXF 0.012 ✅獨立
# 用 TXF 確認 MXF 等於問「自己跟自己同不同方向」——濾掉的只有 6%，續行從
# −0.64 變 −0.63。
#
# 【硬判準，寫在前面免得事後放寬】
#   2026-08-21 單晚實測 ①+②+③(EXF) 在 MXF 上是 +6.66 點對成本線 3.07（過線），
#   但 n=19、兩段拆不開，而且 **③ 單獨用時方向是反的**（EXF 同方向 −1.42 /
#   反方向 +0.80；SPF 同方向 −0.66 / 反方向 +1.16，兩個獨立參照一致）。
#   成分反向、交互作用為正、n=19 —— 這是過擬合的形狀。當天在同一份資料上
#   試過 77 種以上的組合，出現一個 2.7σ 的格子在期望值以內。
#   所以採納條件是**兩條同時成立**：
#     (a) ①+②+③ 跨 session-day 穩定超過該商品的成本門檻
#     (b) ③ 單獨使用時方向為正
#   只有 (a) 不算數。
BREAK_RATIO = 3.0          # 牆＝該檔 size ≥ 基準 × 此值
BREAK_MIN_LOTS = 10        # 且絕對口數下限（個股期貨簿子薄，門檻不能只看倍數）
# 「價格走開多遠算 HOLD」不能用固定 bps——2026-08-21 實測這個門檻對兩類商品意義相反：
#   MXF 指數：25 bps ＝ 112 點，比 10 分鐘全距還大 → 幾乎判不出 HOLD，
#             episode 從 444 膨脹到 1,154、撐的時間從 3.4s 變 9.6s（假的）
#   CCF 個股：光價差就 41.6 bps → 25 bps 連一個價差都不到 → 過嚴
# 改成**自我校準**：用該 session 自己的 10 分鐘 mid 全距中位數的一半。
# 這樣每個商品都用自己的波動尺度，跨商品才可比。
BREAK_AWAY_RANGE_FRAC = 0.5
#: 自我校準的上下限（bps），擋掉極端安靜或極端劇烈的 session 算出荒謬門檻
BREAK_AWAY_MIN_BPS, BREAK_AWAY_MAX_BPS = 2.0, 60.0
BREAK_MAXDUR_SEC = 900.0
BREAK_HORIZONS = (30, 60)
#: 期貨交易稅 0.00002/邊 → 來回 4 bps（點數上尺度不變，bps 上也是）
TAX_BPS_ROUNDTRIP = 4.0
#: ③ 的外部參照。相關係數一併記進 ledger，恆等式才看得出來
REF_ROOTS = ("MXF", "EXF", "SPF")
#: 要跑破牆分析的商品（比 pivot 分析廣：個股期貨才是這條線的主線）
BREAK_ROOTS = ("TMF", "MXF", "TXF", "CCF", "DQF", "PWF", "SFF", "CKF",
               "LUF", "RWF", "IRF", "RA", "KB", "OP", "PJ", "QD", "GU",
               "QL", "OW", "HB", "GH", "IP", "FF")


def cache_dir() -> Path:
    try:
        import stock_db

        return Path(stock_db.DATA_DIR).parent / "cache"
    except Exception:  # noqa: BLE001
        return Path.home() / "goldenstocks-data" / "cache"


def ledger_path() -> Path:
    try:
        from report_paths import REPORTS_ROOT

        return Path(REPORTS_ROOT) / "research" / "channel_lab" / "pivot_wall_ledger.jsonl"
    except Exception:  # noqa: BLE001
        return (Path(__file__).resolve().parents[2] / "reports" / "research"
                / "channel_lab" / "pivot_wall_ledger.jsonl")


def _load(root: str, kind: str, days: list[str]) -> list[dict]:
    """kind ∈ {books, trades}。殭屍列（凍結重送）一律丟棄。"""
    tk = "trade_time" if kind == "trades" else "book_time"
    out: list[dict] = []
    for day in days:
        p = cache_dir() / f"{root.lower()}_{kind}" / f"{root.lower()}_{kind}_{day}.jsonl"
        if not p.exists():
            continue
        for line in p.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("stale"):
                continue
            try:
                ts = float(r[tk]) / 1e6
            except (KeyError, TypeError, ValueError):
                continue
            if "stale" not in r:
                # 舊資料沒有 stale 欄位，自己用 ts − book_time 反推
                try:
                    wall = datetime.fromisoformat(str(r["ts"])).timestamp()
                except (KeyError, TypeError, ValueError):
                    continue
                if wall - ts > MAX_BOOK_AGE_SEC:
                    continue
            r["_ts"] = ts
            out.append(r)
    out.sort(key=lambda r: r["_ts"])
    return out


def _session_window(day: str, session: str) -> tuple[float, float]:
    d = date.fromisoformat(day)
    if session == "day":
        a = datetime(d.year, d.month, d.day, 8, 45, tzinfo=TZ)
        b = datetime(d.year, d.month, d.day, 13, 45, tzinfo=TZ)
    else:
        a = datetime(d.year, d.month, d.day, 15, 0, tzinfo=TZ)
        nd = d + timedelta(days=1)
        b = datetime(nd.year, nd.month, nd.day, 5, 0, tzinfo=TZ)
    return a.timestamp(), b.timestamp()


def _zigzag(trades: list[dict], th: float) -> list[tuple[str, dict]]:
    if not trades:
        return []
    piv: list[tuple[str, dict]] = []
    direction = 0
    hi = lo = trades[0]
    for r in trades:
        p = r["price"]
        if p > hi["price"]:
            hi = r
        if p < lo["price"]:
            lo = r
        if direction >= 0 and hi["price"] - p >= th:
            piv.append(("high", hi))
            direction = -1
            lo = r
        elif direction <= 0 and p - lo["price"] >= th:
            piv.append(("low", lo))
            direction = 1
            hi = r
    return piv


def _prep_books(books: list[dict]) -> tuple[list[float], list[float], list[dict | None]]:
    T: list[float] = []
    MID: list[float] = []
    PRE: list[dict | None] = []
    for b in books:
        bids, asks = b.get("bids") or [], b.get("asks") or []
        if len(bids) < 5 or len(asks) < 5:
            continue
        sb = [x["size"] for x in bids]
        sa = [x["size"] for x in asks]
        kb, ka = sb.index(max(sb)), sa.index(max(sa))
        T.append(b["_ts"])
        MID.append(0.5 * (bids[0]["price"] + asks[0]["price"]))
        PRE.append({
            "bids": (max(sb), kb, bids[kb]["price"]),
            "asks": (max(sa), ka, asks[ka]["price"]),
        })
    return T, MID, PRE


def analyse(root: str, day: str, session: str) -> dict | None:
    lo_ts, hi_ts = _session_window(day, session)
    files = [day] if session == "day" else [day, str(date.fromisoformat(day) + timedelta(days=1))]
    trades = [r for r in _load(root, "trades", files) if lo_ts <= r["_ts"] <= hi_ts]
    books = [r for r in _load(root, "books", files) if lo_ts <= r["_ts"] <= hi_ts]
    if len(trades) < 500 or len(books) < 2000:
        return None
    T, MID, PRE = _prep_books(books)
    if len(T) < 2000:
        return None

    # 覆蓋率：量「有 live 五檔的分鐘數」，不是量缺口——資料被截斷時沒有「兩列之間」
    covered = {int(ts // 60) for ts in T}
    expected = SESSION_MINUTES[session]
    cov_pct = round(100.0 * len(covered) / expected, 1)
    coverage = {
        "covered_minutes": len(covered), "expected_minutes": expected,
        "coverage_pct": cov_pct,
        "first_live": datetime.fromtimestamp(T[0], tz=TZ).isoformat(timespec="seconds"),
        "last_live": datetime.fromtimestamp(T[-1], tz=TZ).isoformat(timespec="seconds"),
        "partial": cov_pct < MIN_COVERAGE_PCT,
    }

    def span(a: float, b: float) -> tuple[int, int]:
        return bisect.bisect_left(T, a), bisect.bisect_right(T, b)

    def peak(i0: int, i1: int, side: str) -> tuple[float, int, float] | None:
        best = None
        for j in range(i0, i1):
            pr = PRE[j]
            if pr and (best is None or pr[side][0] > best[0]):
                best = pr[side]
        return best

    def baseline(ts: float, side: str) -> float | None:
        i0, i1 = span(ts - BASE_MINUTES * 60, ts)
        vals = [PRE[j][side][0] for j in range(i0, i1) if PRE[j]]
        return st.median(vals) if len(vals) > 50 else None

    def ratio_at(ts: float, side: str) -> tuple[float, int, float] | None:
        i0, i1 = span(ts - LOOK_BACK_SEC, ts + LOOK_FWD_SEC)
        pk, base = peak(i0, i1, side), baseline(ts, side)
        if not pk or not base or base <= 0:
            return None
        return pk[0] / base, pk[1] + 1, pk[2]

    # ---- 轉折處 ----
    piv = _zigzag(trades, PIVOT_TH)
    pivot_ratios: list[float] = []
    pivot_rows: list[dict] = []
    for kind, r in piv:
        side = "bids" if kind == "low" else "asks"
        got = ratio_at(r["_ts"], side)
        if not got:
            continue
        ratio, tier, wpx = got
        pivot_ratios.append(ratio)
        pivot_rows.append({
            "t": datetime.fromtimestamp(r["_ts"], tz=TZ).isoformat(timespec="seconds"),
            "kind": kind, "pivot_px": r["price"], "side": side,
            "ratio": round(ratio, 3), "tier": tier,
            "dist_pts": abs(wpx - r["price"]),
        })

    # ---- 對照組：離任何轉折 ≥300 秒的隨機時刻 ----
    pts = [r["_ts"] for _, r in piv]
    rng = random.Random(20260820)
    cand = [T[j] for j in range(0, len(T), 29)
            if all(abs(T[j] - p) > 300 for p in pts)]
    ctrl_ratios: list[float] = []
    for ts in rng.sample(cand, min(400, len(cand))):
        for side in ("bids", "asks"):
            got = ratio_at(ts, side)
            if got:
                ctrl_ratios.append(got[0])

    # ---- 反向檢定：看到牆之後價格往哪走（含波動分層）----
    base_b: list[float] = []
    base_a: list[float] = []
    cb = ca = 1.0
    for i in range(len(T)):
        if i % 500 == 0:
            i0 = bisect.bisect_left(T, T[i] - BASE_MINUTES * 60)
            step = max(1, (i - i0) // 400)
            vb = [PRE[j]["bids"][0] for j in range(i0, i, step) if PRE[j]] or [1.0]
            va = [PRE[j]["asks"][0] for j in range(i0, i, step) if PRE[j]] or [1.0]
            cb, ca = st.median(vb), st.median(va)
        base_b.append(cb)
        base_a.append(ca)
    rv = []
    for i in range(len(T)):
        i0 = bisect.bisect_left(T, T[i] - 300)
        seg = MID[i0:i + 1] or [MID[i]]
        rv.append(max(seg) - min(seg))
    rs = sorted(rv)
    t1, t2 = rs[len(rs) // 3], rs[2 * len(rs) // 3]

    fwd: dict[str, dict] = {}
    for H in FWD_HORIZONS:
        agg: dict[str, list[float]] = {}
        aggv: dict[str, list[float]] = {}
        for i in range(0, len(T), 7):
            if not PRE[i]:
                continue
            j = bisect.bisect_left(T, T[i] + H)
            if j >= len(T):
                continue
            d = MID[j] - MID[i]
            bw = PRE[i]["bids"][0] >= 3.0 * base_b[i]
            aw = PRE[i]["asks"][0] >= 3.0 * base_a[i]
            k = ("bid_wall" if bw and not aw else "ask_wall" if aw and not bw
                 else "both" if aw and bw else "none")
            agg.setdefault(k, []).append(d)
            vk = "lo" if rv[i] <= t1 else ("mid" if rv[i] <= t2 else "hi")
            aggv.setdefault(f"{vk}|{k}", []).append(d)
        fwd[f"h{H}"] = {
            "pooled": {k: {"n": len(v), "mean": round(st.mean(v), 3),
                           "pct_up": round(100 * sum(1 for x in v if x > 0) / len(v), 1)}
                       for k, v in agg.items() if len(v) >= 30},
            "by_vol": {k: {"n": len(v), "mean": round(st.mean(v), 3),
                           "pct_up": round(100 * sum(1 for x in v if x > 0) / len(v), 1)}
                       for k, v in aggv.items() if len(v) >= 30},
        }

    # ---- 牆事件 → 轉折：哪些牆會轉、哪些不會 ----
    def first_passage(i: int, up_first: bool) -> bool | None:
        """先觸 +BARRIER 還是 −BARRIER。True = 先觸對該牆有利的方向。"""
        m0 = MID[i]
        j = i
        while j < len(T) and T[j] <= T[i] + TURN_HORIZON_SEC:
            d = MID[j] - m0
            if d >= TURN_BARRIER_PTS:
                return up_first
            if d <= -TURN_BARRIER_PTS:
                return not up_first
            j += 1
        return None                       # 視野不足，不猜

    def pos_in_range(i: int, sec: int = 1800) -> float:
        i0 = bisect.bisect_left(T, T[i] - sec)
        seg = MID[i0:i + 1] or [MID[i]]
        span_ = max(seg) - min(seg)
        return (MID[i] - min(seg)) / span_ if span_ > 0 else 0.5

    events: list[dict] = []
    last_ev = {"bids": -1e18, "asks": -1e18}
    for i in range(len(T)):
        if not PRE[i]:
            continue
        for side, base_arr in (("bids", base_b), ("asks", base_a)):
            mx = PRE[i][side][0]
            if base_arr[i] <= 0 or mx < WALL_EVENT_RATIO * base_arr[i]:
                continue
            if T[i] - last_ev[side] < WALL_COOLDOWN_SEC:
                continue
            last_ev[side] = T[i]
            turn = first_passage(i, up_first=(side == "bids"))
            if turn is None:
                continue
            i10 = bisect.bisect_left(T, T[i] - 600)
            events.append({
                "side": side, "t": T[i],
                "ratio": mx / base_arr[i], "size": mx,
                "tier": PRE[i][side][1] + 1,
                "pos30": pos_in_range(i),
                "trend10": MID[i] - MID[i10],
                "turn": turn,
            })

    def rate(xs: list[dict]) -> float | None:
        return round(100 * sum(1 for e in xs if e["turn"]) / len(xs), 1) if xs else None

    def tercile_gap(xs: list[dict], key: str) -> dict:
        """高三分位轉折率 − 低三分位轉折率。負值＝該特徵越大越不會轉折。"""
        if len(xs) < 12:
            return {"n": len(xs), "lo": None, "hi": None, "gap": None}
        v = sorted(xs, key=lambda e: e[key])
        k = len(v) // 3
        lo, hi = v[:k], v[-k:]
        rl, rh = rate(lo), rate(hi)
        return {"n": len(xs), "n_tercile": k, "lo": rl, "hi": rh,
                "gap": round(rh - rl, 1) if (rl is not None and rh is not None) else None}

    # 隨機時刻的 first-passage 基準率（牆事件要贏過的對照）
    base_turn: dict[str, float | None] = {}
    for side in ("bids", "asks"):
        hits = [first_passage(i, up_first=(side == "bids")) for i in range(0, len(T), 53)]
        hits = [h for h in hits if h is not None]
        base_turn[side] = round(100 * sum(hits) / len(hits), 1) if hits else None

    half_t = (T[0] + T[-1]) / 2 if T else 0.0
    wall_events = {
        "barrier_pts": TURN_BARRIER_PTS, "horizon_sec": TURN_HORIZON_SEC,
        "n_events": len(events),
        "baseline_turn_pct": base_turn,
        "by_side": {
            s: {"n": len([e for e in events if e["side"] == s]),
                "turn_pct": rate([e for e in events if e["side"] == s]),
                "vs_baseline_pp": (
                    round(rate([e for e in events if e["side"] == s]) - base_turn[s], 1)
                    if rate([e for e in events if e["side"] == s]) is not None
                    and base_turn[s] is not None else None),
                "ratio_gap": tercile_gap([e for e in events if e["side"] == s], "ratio")}
            for s in ("bids", "asks")
        },
        # 主假設：ratio 的三分位落差應為負（薄牆勝過厚牆）
        "pooled_gap": {k: tercile_gap(events, k)
                       for k in ("ratio", "size", "trend10", "pos30")},
        # session 內分半——當日自我檢查，擋掉「只有某一段成立」
        "split_half_ratio_gap": [
            tercile_gap([e for e in events if (e["t"] < half_t) == (h == 0)], "ratio")["gap"]
            for h in (0, 1)
        ],
    }

    def frac(xs: list[float], thr: float) -> float | None:
        return round(100 * sum(1 for x in xs if x >= thr) / len(xs), 2) if xs else None

    return {
        "schema": "pivot-wall-daily-v1",
        "root": root, "session_date": day, "session": session,
        "coverage": coverage,
        "n_trades": len(trades), "n_books_live": len(T),
        "px_range": round(max(r["price"] for r in trades) - min(r["price"] for r in trades), 1),
        "pivot_threshold_pts": PIVOT_TH,
        "n_pivots": len(piv), "n_pivots_scored": len(pivot_ratios),
        "pivot": {
            "median_ratio": round(st.median(pivot_ratios), 3) if pivot_ratios else None,
            **{f"pct_ge_{t:g}x": frac(pivot_ratios, t) for t in WALL_RATIOS},
        },
        "control": {
            "n": len(ctrl_ratios),
            "median_ratio": round(st.median(ctrl_ratios), 3) if ctrl_ratios else None,
            **{f"pct_ge_{t:g}x": frac(ctrl_ratios, t) for t in WALL_RATIOS},
        },
        "forward": fwd,
        "wall_events": wall_events,
        "pivots": pivot_rows,
    }


def _mid_series(root: str, files: list[str], lo: float, hi: float) -> tuple[list[float], list[float]]:
    """(ts, mid)，只取 live 列。給 ③ 外部參照用。"""
    T: list[float] = []
    M: list[float] = []
    for r in _load(root, "books", files):
        if not (lo <= r["_ts"] <= hi):
            continue
        b, a = r.get("bids") or [], r.get("asks") or []
        if not b or not a:
            continue
        bp, ap = b[0].get("price"), a[0].get("price")
        if not bp or not ap or ap <= bp:
            continue
        T.append(r["_ts"])
        M.append(0.5 * (bp + ap))
    return T, M


def analyse_breaks(root: str, day: str, session: str,
                   refs: dict[str, tuple[list[float], list[float]]]) -> dict | None:
    """破牆事件：撐多久、破後續行、三個預先登記的濾網。全部以 bps 計價。"""
    lo_ts, hi_ts = _session_window(day, session)
    files = [day] if session == "day" else [day, str(date.fromisoformat(day) + timedelta(days=1))]
    books = [r for r in _load(root, "books", files)
             if lo_ts <= r["_ts"] <= hi_ts
             and len(r.get("bids") or []) >= 5 and len(r.get("asks") or []) >= 5]
    trades = [r for r in _load(root, "trades", files) if lo_ts <= r["_ts"] <= hi_ts]
    if len(books) < 2000 or len(trades) < 100:
        return None
    T = [r["_ts"] for r in books]
    MID = [0.5 * (r["bids"][0]["price"] + r["asks"][0]["price"]) for r in books]
    SPR = [(r["asks"][0]["price"] - r["bids"][0]["price"]) / m * 1e4
           for r, m in zip(books, MID)]
    TT = [r["_ts"] for r in trades]
    TP = [r["price"] for r in trades]
    TV = [r["size"] for r in trades]

    base: dict[str, list[list[float]]] = {}
    for side in ("bids", "asks"):
        bs = []
        for k in range(5):
            col = [r[side][k]["size"] for r in books]
            cur, out = 1.0, [1.0] * len(T)
            for i in range(len(T)):
                if i % 400 == 0:
                    i0 = bisect.bisect_left(T, T[i] - BASE_MINUTES * 60)
                    step = max(1, (i - i0) // 300)
                    cur = st.median(col[i0:i:step] or [1.0])
                out[i] = cur
            bs.append(out)
        base[side] = bs

    # 自我校準的 HOLD 門檻：該 session 自己的 10 分鐘全距中位數 × 0.5
    rng_samples: list[float] = []
    for i in range(0, len(T), max(1, len(T) // 400)):
        i0 = bisect.bisect_left(T, T[i] - 600)
        seg = MID[i0:i + 1]
        if len(seg) > 5 and MID[i]:
            rng_samples.append((max(seg) - min(seg)) / MID[i] * 1e4)
    away_bps = min(BREAK_AWAY_MAX_BPS,
                   max(BREAK_AWAY_MIN_BPS,
                       (st.median(rng_samples) if rng_samples else 10.0) * BREAK_AWAY_RANGE_FRAC))

    def refmom(k: str, ts: float, lb: float = 5.0) -> float | None:
        rt, rm = refs.get(k, ([], []))
        if not rt:
            return None
        j = bisect.bisect_right(rt, ts) - 1
        i = bisect.bisect_right(rt, ts - lb) - 1
        if j < 0 or i < 0 or ts - rt[j] > 20 or rm[i] == 0:
            return None
        return (rm[j] - rm[i]) / rm[i] * 1e4

    active: dict[tuple, dict] = {}
    brk: list[dict] = []
    n_hold = 0
    for i, r in enumerate(books):
        if i and T[i] - T[i - 1] > 1800:
            active.clear()                      # 跨 session 斷點
        for side in ("bids", "asks"):
            for k in range(5):
                lvl = r[side][k]
                sz, px = lvl["size"], lvl["price"]
                key = (side, px)
                if key in active or not px:
                    continue
                if sz >= BREAK_MIN_LOTS and base[side][k][i] > 0 and sz >= BREAK_RATIO * base[side][k][i]:
                    active[key] = {"t0": T[i], "px": px, "side": side,
                                   "ratio": sz / base[side][k][i], "size0": sz}
        mid = MID[i]
        for key in list(active):
            e = active[key]
            side, px = key
            k0 = bisect.bisect_left(TT, e["t0"])
            k1 = bisect.bisect_right(TT, T[i])
            thr = [m for m in range(k0, k1) if (TP[m] < px if side == "bids" else TP[m] > px)]
            if not thr:
                away = abs(mid - px) / px * 1e4
                gone = ((side == "bids" and mid > px) or (side == "asks" and mid < px))
                if (gone and away >= away_bps) or T[i] - e["t0"] > BREAK_MAXDUR_SEC:
                    n_hold += 1
                    del active[key]
                continue
            m0 = thr[0]
            tb = TT[m0]
            d = -1.0 if side == "bids" else 1.0
            a3 = bisect.bisect_left(TT, tb - 3)
            a60 = bisect.bisect_left(TT, tb - 60)
            v3 = sum(TV[m] for m in range(a3, m0))
            v60 = sum(TV[m] for m in range(a60, m0))
            j = bisect.bisect_left(T, tb)
            j60 = bisect.bisect_left(T, tb - 60)
            seg = MID[j60:j + 1] or [mid]
            rec = {"dur": tb - e["t0"], "dir": d, "ratio": e["ratio"],
                   "spread_bps": SPR[min(j, len(SPR) - 1)],
                   "vol_ratio": (v3 / (v60 / 20.0)) if v60 > 0 else None,
                   "range60_bps": (max(seg) - min(seg)) / px * 1e4}
            for k in REF_ROOTS:
                rec[f"m_{k.lower()}"] = refmom(k, tb)
            for hz in BREAK_HORIZONS:
                jj = bisect.bisect_left(T, tb + hz)
                rec[f"run{hz}"] = (((px - MID[jj]) if side == "bids" else (MID[jj] - px))
                                   / px * 1e4) if jj < len(T) else None
            brk.append(rec)
            del active[key]

    ok = [e for e in brk if e.get("run30") is not None and e.get("vol_ratio") is not None]
    if len(ok) < 5:
        return {"root": root, "n_break": len(brk), "n_hold": n_hold, "n_scored": len(ok),
                "note": "破牆事件太少，只記數量"}

    def q(xs: list[float], p: float) -> float:
        s = sorted(xs)
        return s[min(len(s) - 1, int(p * (len(s) - 1)))]

    def stats(sub: list[dict], hz: int = 30) -> dict | None:
        v = [e[f"run{hz}"] for e in sub if e.get(f"run{hz}") is not None]
        if len(v) < 5:
            return {"n": len(v)}
        m = st.mean(v)
        return {"n": len(v), "mean_bps": round(m, 2),
                "se_bps": round(st.stdev(v) / len(v) ** 0.5, 2) if len(v) > 1 else None,
                "pct_up": round(100 * sum(1 for x in v if x > 0) / len(v), 1)}

    spread_med = st.median([e["spread_bps"] for e in ok])
    cost_bps = round(spread_med + TAX_BPS_ROUNDTRIP, 2)
    vr = sorted(e["vol_ratio"] for e in ok)
    r6 = sorted(e["range60_bps"] for e in ok)
    f1 = [e for e in ok if e["vol_ratio"] <= q(vr, 1 / 3)]
    f2 = [e for e in ok if e["range60_bps"] <= q(r6, 1 / 3)]
    f12 = [e for e in ok if e["vol_ratio"] <= q(vr, 1 / 3) and e["range60_bps"] <= q(r6, 1 / 3)]
    dur = [e["dur"] for e in ok]
    out: dict = {
        "root": root, "n_break": len(brk), "n_hold": n_hold, "n_scored": len(ok),
        "dur_sec": {"p50": round(q(dur, .5), 1), "p90": round(q(dur, .9), 1),
                    "pct_ge_5s": round(100 * sum(1 for x in dur if x >= 5) / len(dur), 1),
                    "pct_ge_60s": round(100 * sum(1 for x in dur if x >= 60) / len(dur), 1)},
        "away_bps": round(away_bps, 2),
        "spread_bps_median": round(spread_med, 2),
        "cost_bps": cost_bps,       # 來回價差＋稅；手續費未計（各商品不同）
        "baseline": stats(ok), "f1_vol": stats(f1), "f2_consol": stats(f2), "f12": stats(f12),
        "ref": {},
    }
    # ③ 外部參照：單獨用（硬判準 b）與三層疊（硬判準 a），一起記才判得了
    for k in REF_ROOTS:
        kk = f"m_{k.lower()}"
        have = [e for e in ok if e.get(kk) is not None]
        if len(have) < 20:
            continue
        out["ref"][k] = {
            "n_matched": len(have),
            "solo_same": stats([e for e in have if e[kk] * e["dir"] > 0]),
            "solo_opp": stats([e for e in have if e[kk] * e["dir"] < 0]),
            "f123": stats([e for e in f12 if e.get(kk) is not None and e[kk] * e["dir"] > 0]),
        }
    return out


def report() -> int:
    p = ledger_path()
    if not p.exists():
        print(f"ledger 還不存在：{p}")
        return 1
    recs = [json.loads(x) for x in p.open(encoding="utf-8") if x.strip()]
    seen: dict[tuple, dict] = {}
    for r in recs:                       # 同一 (root, day, session) 取最後一筆
        seen[(r["root"], r["session_date"], r["session"])] = r
    recs = sorted(seen.values(), key=lambda r: (r["root"], r["session_date"], r["session"]))
    print(f"累積 {len(recs)} 個 session-day\n")
    def partial(r: dict) -> bool:
        return bool(r.get("coverage", {}).get("partial"))

    n_partial = sum(1 for r in recs if partial(r))
    if n_partial:
        print(f"⚠️  其中 {n_partial} 個是**半截 session**（覆蓋率 < {MIN_COVERAGE_PCT:.0f}%），"
              f"下方以 ! 標示、且**不計入彙總**\n")

    print(f"{'商品':<5}{'日期':<12}{'盤別':<7}{'覆蓋':>7}{'轉折':>5}{'轉折≥3x':>9}"
          f"{'對照≥3x':>9}{'倍率':>7}{'區間':>7}")
    lifts = []
    for r in recs:
        pv, ct = r["pivot"].get("pct_ge_3x"), r["control"].get("pct_ge_3x")
        lift = (pv / ct) if (pv and ct) else None
        if lift and not partial(r):
            lifts.append(lift)
        cov = r.get("coverage", {}).get("coverage_pct")
        cov_s = f"{cov:.0f}%!" if partial(r) else (f"{cov:.0f}%" if cov is not None else "—")
        print(f"{r['root']:<5}{r['session_date']:<12}{r['session']:<7}{cov_s:>7}"
              f"{r['n_pivots_scored']:>5}{(f'{pv:.1f}%' if pv is not None else '—'):>9}"
              f"{(f'{ct:.1f}%' if ct is not None else '—'):>9}"
              f"{(f'{lift:.2f}x' if lift else '—'):>7}{r['px_range']:>7.0f}")
    if len(lifts) >= 2:
        print(f"\n轉折/對照 的 ≥3× 倍率：中位 {st.median(lifts):.2f}× · "
              f"n={len(lifts)} session-day · >1 的比例 "
              f"{100 * sum(1 for x in lifts if x > 1) / len(lifts):.0f}%")
    else:
        print("\n樣本還太少，先累積。判準：對照組扣除後跨 session-day 是否一致。")

    # --- 主假設：薄牆勝過厚牆（ratio 三分位落差應為負）---
    ws = [r for r in recs
          if r.get("wall_events", {}).get("n_events") and not partial(r)]
    if ws:
        print(f"\n=== 牆事件 → 轉折（障礙 ±{ws[-1]['wall_events']['barrier_pts']:.0f} 點 / "
              f"{ws[-1]['wall_events']['horizon_sec']:.0f} 秒）===")
        print(f"{'商品':<5}{'日期':<12}{'盤別':<7}{'事件':>5}{'買方vs基準':>11}"
              f"{'賣方vs基準':>11}{'薄→厚落差':>11}{'分半同號':>9}")
        gaps = []
        for r in ws:
            w = r["wall_events"]
            g = w["pooled_gap"]["ratio"]["gap"]
            sh = w.get("split_half_ratio_gap") or [None, None]
            same = ("✓" if (sh[0] is not None and sh[1] is not None
                            and sh[0] * sh[1] > 0) else "✗")
            if g is not None:
                gaps.append(g)
            bs = w["by_side"]["bids"]["vs_baseline_pp"]
            as_ = w["by_side"]["asks"]["vs_baseline_pp"]
            print(f"{r['root']:<5}{r['session_date']:<12}{r['session']:<7}"
                  f"{w['n_events']:>5}{(f'{bs:+.1f}pp' if bs is not None else '—'):>11}"
                  f"{(f'{as_:+.1f}pp' if as_ is not None else '—'):>11}"
                  f"{(f'{g:+.1f}pp' if g is not None else '—'):>11}{same:>9}")
        if len(gaps) >= 2:
            neg = 100 * sum(1 for g in gaps if g < 0) / len(gaps)
            print(f"\n薄→厚落差：中位 {st.median(gaps):+.1f}pp · n={len(gaps)} session-day · "
                  f"為負（薄牆勝）的比例 {neg:.0f}%")
            print("假設成立的樣子＝落差穩定為負且跨 session-day 一致；"
                  "若在 0 附近擺盪，就是 2026-08-20 那晚的雜訊。")
    # --- 破牆 ledger ---
    bp = p.with_name("pivot_wall_break_ledger.jsonl")
    if bp.exists():
        brecs = [json.loads(x) for x in bp.open(encoding="utf-8") if x.strip()]
        seen_b: dict[tuple, dict] = {}
        for r in brecs:
            seen_b[(r["root"], r["session_date"], r["session"])] = r
        brecs = [r for r in seen_b.values() if (r.get("baseline") or {}).get("mean_bps") is not None]
        if brecs:
            print(f"\n\n=== 破牆研究（{len(brecs)} 個 root-session）===")
            print(f"{'商品':<5}{'日期':<12}{'盤別':<7}{'n':>5}{'撐p50':>8}{'續行bps':>9}"
                  f"{'成本bps':>9}{'淨':>8}{'①+②':>8}")
            per_root: dict[str, list] = {}
            for r in sorted(brecs, key=lambda x: (x["root"], x["session_date"], x["session"])):
                b = r["baseline"]
                f12 = (r.get("f12") or {}).get("mean_bps")
                net = b["mean_bps"] - r["cost_bps"]
                per_root.setdefault(r["root"], []).append(net)
                print(f"{r['root']:<5}{r['session_date']:<12}{r['session']:<7}{b['n']:>5}"
                      f"{r['dur_sec']['p50']:>7.0f}s{b['mean_bps']:>+9.1f}{r['cost_bps']:>9.1f}"
                      f"{net:>+8.1f}{(f'{f12:+.1f}' if f12 is not None else '—'):>8}")
            print("\n【硬判準】採納要 (a)①+②+③ 穩定 > 成本 **且** (b)③ 單獨用方向為正。"
                  "\n2026-08-21 單晚 MXF 的 ①+②+③(EXF)=+6.66 對成本 3.07 過線，但 n=19、"
                  "\n③ 單獨用是 −1.42（EXF）/ −0.66（SPF），成分反向 → 不採納。")
            print(f"\n{'商品':<6}{'③單獨(同方向)':>16}{'③單獨(反方向)':>16}{'①+②+③':>12}"
                  f"{'判準(b)':>9}")
            agg: dict[str, dict] = {}
            for r in brecs:
                for k, v in (r.get("ref") or {}).items():
                    a = agg.setdefault((r["root"], k), {"same": [], "opp": [], "f123": []})
                    for tag, key in (("same", "solo_same"), ("opp", "solo_opp"), ("f123", "f123")):
                        m = (v.get(key) or {}).get("mean_bps")
                        if m is not None:
                            a[tag].append(m)
            for (root_, k), a in sorted(agg.items()):
                if not a["same"]:
                    continue
                s = st.mean(a["same"])
                o = st.mean(a["opp"]) if a["opp"] else float("nan")
                f = st.mean(a["f123"]) if a["f123"] else float("nan")
                print(f"{root_}·{k:<3}{s:>+15.1f}{o:>+16.1f}{f:>+12.1f}"
                      f"{('✓' if s > 0 else '✗'):>9}")

    print("\n跨 session-day 一致性比任何單日的 p 值都重要（2026-08-20 教訓）。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", default=None, help="預設為前一個日曆日")
    ap.add_argument("--roots", default="TMF,MXF,TXF")
    ap.add_argument("--sessions", default="day,night")
    ap.add_argument("--breaks", action="store_true", default=True,
                    help="同時跑破牆研究（預設開）")
    ap.add_argument("--no-breaks", dest="breaks", action="store_false")
    ap.add_argument("--break-roots", default=",".join(BREAK_ROOTS))
    ap.add_argument("--report", action="store_true", help="只印累積結果，不重算")
    args = ap.parse_args()
    if args.report:
        return report()

    day = args.day or str(datetime.now(tz=TZ).date() - timedelta(days=1))
    out = ledger_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    n_ok = 0
    for root in [x.strip().upper() for x in args.roots.split(",") if x.strip()]:
        for session in [x.strip() for x in args.sessions.split(",") if x.strip()]:
            try:
                rec = analyse(root, day, session)
            except Exception as exc:  # noqa: BLE001 -- 單一組合失敗不能拖垮整批
                print(f"{root} {day} {session}: 失敗 {exc!r}")
                continue
            if rec is None:
                print(f"{root} {day} {session}: 資料不足，略過")
                continue
            rec["generated_at"] = datetime.now(tz=TZ).isoformat(timespec="seconds")
            with out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_ok += 1
            pv, ct = rec["pivot"].get("pct_ge_3x"), rec["control"].get("pct_ge_3x")
            cov = rec["coverage"]
            flag = "  ⚠️ 半截 session，彙總時會排除" if cov["partial"] else ""
            print(f"{root} {day} {session}: 覆蓋 {cov['coverage_pct']:.0f}%"
                  f"（{cov['covered_minutes']}/{cov['expected_minutes']} 分，"
                  f"{cov['first_live'][11:16]}–{cov['last_live'][11:16]}）· "
                  f"轉折 {rec['n_pivots_scored']} 個 · "
                  f"≥3x 轉折 {pv}% vs 對照 {ct}% · 區間 {rec['px_range']:.0f} 點{flag}")
    print(f"\n寫入 {n_ok} 筆 → {out}")

    # --- 破牆研究（預先登記的三個濾網）---
    if args.breaks:
        bout = out.with_name("pivot_wall_break_ledger.jsonl")
        n_b = 0
        for session in [x.strip() for x in args.sessions.split(",") if x.strip()]:
            lo_ts, hi_ts = _session_window(day, session)
            files = ([day] if session == "day"
                     else [day, str(date.fromisoformat(day) + timedelta(days=1))])
            refs = {k: _mid_series(k, files, lo_ts, hi_ts) for k in REF_ROOTS}
            for root in [x.strip().upper() for x in args.break_roots.split(",") if x.strip()]:
                try:
                    rec = analyse_breaks(root, day, session, refs)
                except Exception as exc:  # noqa: BLE001 -- 單一商品失敗不能拖垮整批
                    print(f"  破牆 {root} {day} {session}: 失敗 {exc!r}")
                    continue
                if rec is None:
                    continue
                rec.update({"schema": "pivot-wall-break-v1", "session_date": day,
                            "session": session,
                            "generated_at": datetime.now(tz=TZ).isoformat(timespec="seconds")})
                with bout.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_b += 1
                b = rec.get("baseline") or {}
                if b.get("mean_bps") is not None:
                    print(f"  破牆 {root} {session}: n={rec['n_scored']} · "
                          f"撐 p50={rec['dur_sec']['p50']}s · 續行 {b['mean_bps']:+.1f} bps · "
                          f"成本 {rec['cost_bps']:.1f} bps")
        print(f"寫入 {n_b} 筆破牆紀錄 → {bout}")
        n_ok += n_b
    return 0 if n_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
