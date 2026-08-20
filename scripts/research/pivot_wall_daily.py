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
    print(f"{'商品':<5}{'日期':<12}{'盤別':<7}{'轉折':>5}{'轉折≥3x':>9}"
          f"{'對照≥3x':>9}{'倍率':>7}{'區間':>7}")
    lifts = []
    for r in recs:
        pv, ct = r["pivot"].get("pct_ge_3x"), r["control"].get("pct_ge_3x")
        lift = (pv / ct) if (pv and ct) else None
        if lift:
            lifts.append(lift)
        print(f"{r['root']:<5}{r['session_date']:<12}{r['session']:<7}"
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
    ws = [r for r in recs if r.get("wall_events", {}).get("n_events")]
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
    print("\n跨 session-day 一致性比任何單日的 p 值都重要（2026-08-20 教訓）。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", default=None, help="預設為前一個日曆日")
    ap.add_argument("--roots", default="TMF,MXF,TXF")
    ap.add_argument("--sessions", default="day,night")
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
            print(f"{root} {day} {session}: 轉折 {rec['n_pivots_scored']} 個 · "
                  f"≥3x 轉折 {pv}% vs 對照 {ct}% · 區間 {rec['px_range']:.0f} 點")
    print(f"\n寫入 {n_ok} 筆 → {out}")
    return 0 if n_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
