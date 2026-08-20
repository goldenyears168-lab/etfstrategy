#!/usr/bin/env python3
"""A2 — 五檔「牆」消失時，是被成交吃掉還是被撤單撤掉？

問題脈絡
--------
TMF 日內回歸通道策略想知道：五檔簿子裡的厚檔（wall）有沒有「承諾價值」。
若牆多半是被撤掉的，它就只是誘餌／冰山對手的殘影，不能當支撐壓力用；
若牆多半是被真實成交吃掉的，它才代表真實供需。

資料
----
  books  : $GOLDENSTOCKS_DATA_DIR/cache/tmf_books/tmf_books_YYYY-MM-DD.jsonl
  trades : $GOLDENSTOCKS_DATA_DIR/cache/tmf_trades/tmf_trades_YYYY-MM-DD.jsonl
兩者都會把日盤（quote_type=FUTURE）與夜盤（FUTURE_AH）交錯寫進同一個檔，
且收盤那一側會凍結重送 → 一律先做殭屍過濾（見 `_is_stale`）。
trades 檔只有 2026-08-17 / 08-18 / 08-19(到 04:31) 有實際內容，
08-14/08-15 沒有逐筆檔 → 本研究只能用 2 個完整日 + 1 個殘缺夜盤。

牆的定義（causal，只用該時刻為止的資料）
--------------------------------------
對每個 (session, side, level_index) 維護「前 N 筆快照的 size 中位數」作為基準
（rolling median，先評估再更新 deque，保證不含當下）。這樣做的理由：樸素的
「最大檔 ≥ 該側中位 ×3」會被簿子天然的深度斜率綁架（越遠掛越多），量到的
大多是第 5 檔；改用 level-specific baseline 就把斜率除掉了。

  wall(k)  : size >= k * baseline_median(level, side)  且  size >= MIN_ABS
  control  : CTRL_LO <= size / baseline_median <= CTRL_HI  （「長得很普通」的檔位）

事件（episode）
--------------
以 (session, side, price) 追蹤「該價位出現在可見五檔內」的一段連續存在期。
anchor = 該存在期內第一次滿足 wall(k)（或 control）條件的快照 —— 之後才開始
累計，anchor 之前的歷史不算，因此完全是「當下看到牆 → 往後會怎樣」的條件量測。

結束時的三分類（terminal fate）
------------------------------
  REMOVED      該價位從可見簿子的「近端」消失（bid: p > best_bid；ask: p < best_ask）
               或落在可見價帶內卻不見了（簿子有洞）→ 這一檔真的被清掉了
  OUT_OF_VIEW  價格整個走開，該價位掉出第 5 檔（bid: p < bid5；ask: p > ask5）
               → 不知道它後來怎樣，第三類，不硬塞進前兩類
  TRUNCATED    session 結束 / 資料斷檔（相鄰 live 快照間隔 > GAP_SEC）→ 排除

被吃掉 vs 被撤掉
---------------
兩套指標都報，因為兩者問的問題不同：

  Metric A（題目原文的定義）：結束前後短窗 [t_end - W, t_end + TOL] 內，該價位
      累計成交量 / anchor 當下的口數 >= theta → 被吃掉。W 與 theta 都掃。
  Metric B（流量會計）：逐區間用 removed_i = max(0, -Δsize) 與該價位在同區間的
      成交量 v_i，令 trade_attributed = Σ min(v_i, removed_i)，
      eaten_fraction = trade_attributed / Σ removed_i。這比只看最後一格穩健，
      因為牆常常是被一塊一塊啃掉的。

對照組
------
同一套流程跑「長得普通」的檔位（control）。若牆與非牆的被吃比例沒有差別，
那「牆」這個標籤就不帶資訊 —— 這是本研究最重要的一個對照。額外做
size-matched 對照（同一個絕對口數桶內比 wall vs control），排除「大的本來就
比較難被吃完」這個機械性解釋。

因果邊界
--------
  * baseline 只用 anchor 之前的快照。
  * 成交比對窗口寫死在 `TRADE_TOL_SEC` / `WINDOWS_SEC`，且只在「判定 episode
    已經結束之後」才回頭查那個窗口 —— 這是事後量測，不是可交易訊號；任何要
    拿去當訊號用的人必須注意 +TOL 那一段是未來資料。
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TZ = timezone(timedelta(hours=8))

MAX_BOOK_AGE_SEC = 5.0       # book_time 落後 wall-clock 超過這個秒數 = 殭屍
MAX_TRADE_AGE_SEC = 5.0
BASELINE_N = 500             # rolling median 視窗（筆）
BASELINE_MIN = 50            # 少於這麼多筆就不判定
MIN_ABS = 10                 # 牆的絕對口數下限
WALL_KS = (2.0, 3.0, 5.0)
# 比率階梯（都要求 size >= MIN_ABS，才能把「絕對厚度」和「相對異常」分開）
RATIO_BANDS = (
    (0.75, 1.35, "band1"),   # 普通
    (1.35, 2.00, "band2"),   # 偏厚
    (2.00, 3.00, "band3"),   # 厚
    (3.00, 5.00, "band4"),   # 牆
    (5.00, 1e9, "band5"),    # 巨牆
)
CTRL_LO, CTRL_HI = 0.75, 1.35
GAP_SEC = 30.0               # 相鄰 live 快照超過這麼久 = 斷檔，開著的 episode 作廢
TRADE_TOL_SEC = 1.0          # 結束時刻往後容忍的時鐘偏移
WINDOWS_SEC = (1.0, 5.0, 30.0)
THETAS = (0.2, 0.33, 0.5, 0.75, 1.0)

SIZE_BUCKETS = ((10, 14), (15, 19), (20, 29), (30, 49), (50, 10**9))
DIST_BUCKETS = ((0.0, 2.0), (2.0, 4.0), (4.0, 6.0), (6.0, 1e9))


def data_root() -> Path:
    try:
        import stock_db

        return Path(stock_db.DATA_DIR).parent
    except Exception:  # noqa: BLE001
        return Path.home() / "goldenstocks-data"


# ---------------------------------------------------------------- loaders
def _wall_clock(ts: str) -> float:
    return datetime.fromisoformat(str(ts)).timestamp()


def load_books(day: str) -> tuple[dict[str, list[dict[str, Any]]], Counter]:
    """回傳 {session: [snapshot,...]}，snapshot 依 book_time 排序、已去殭屍去重。"""
    path = data_root() / "cache" / "tmf_books" / f"tmf_books_{day}.jsonl"
    st = Counter()
    streams: dict[str, list[dict[str, Any]]] = {"day": [], "night": []}
    if not path.exists():
        return streams, st
    for line in path.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        st["rows"] += 1
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            st["bad_json"] += 1
            continue
        bids, asks = r.get("bids") or [], r.get("asks") or []
        if len(bids) < 5 or len(asks) < 5:
            st["short_side"] += 1
            continue
        try:
            wall = _wall_clock(r["ts"])
            bt = float(r["book_time"]) / 1e6
        except (KeyError, TypeError, ValueError):
            st["bad_ts"] += 1
            continue
        if "stale" in r:
            is_stale = bool(r["stale"])
        else:
            is_stale = (wall - bt) > MAX_BOOK_AGE_SEC
        if is_stale:
            st["stale_zombie"] += 1
            continue
        qt = str(r.get("quote_type") or "")
        sess = "night" if qt.endswith("_AH") else "day"
        st[f"live_{sess}"] += 1
        streams[sess].append(
            {
                "t": bt,
                "bp": [int(b["price"]) for b in bids],
                "bs": [int(b["size"]) for b in bids],
                "ap": [int(a["price"]) for a in asks],
                "asz": [int(a["size"]) for a in asks],
            }
        )
    for sess, rows in streams.items():
        rows.sort(key=lambda r: r["t"])
        dedup: list[dict[str, Any]] = []
        for r in rows:
            if dedup and r["t"] == dedup[-1]["t"]:
                dedup[-1] = r  # 同一個 book_time 只留最後一筆
                st["dup_book_time"] += 1
                continue
            dedup.append(r)
        streams[sess] = dedup
    return streams, st


def load_trades(day: str) -> tuple[dict[str, dict[int, tuple[list[float], list[int], list[int]]]], Counter]:
    """回傳 {session: {price: (times, sizes, aggr)}}；aggr=+1 buy-initiated, -1 sell, 0 unknown。"""
    path = data_root() / "cache" / "tmf_trades" / f"tmf_trades_{day}.jsonl"
    st = Counter()
    raw: dict[str, dict[int, list[tuple[float, int, int]]]] = {"day": {}, "night": {}}
    if not path.exists():
        return {"day": {}, "night": {}}, st
    for line in path.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        st["rows"] += 1
        try:
            r = json.loads(line)
            wall = _wall_clock(r["ts"])
            tt = float(r["trade_time"]) / 1e6
            price = int(r["price"])
            size = int(r["size"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            st["bad_row"] += 1
            continue
        if (wall - tt) > MAX_TRADE_AGE_SEC:
            st["stale_zombie"] += 1
            continue
        sec = (datetime.fromtimestamp(tt, tz=TZ).hour * 3600
               + datetime.fromtimestamp(tt, tz=TZ).minute * 60)
        sess = "day" if 8 * 3600 + 40 * 60 <= sec <= 13 * 3600 + 50 * 60 else "night"
        bid, ask = r.get("bid"), r.get("ask")
        aggr = 0
        if isinstance(bid, (int, float)) and price <= bid:
            aggr = -1
        elif isinstance(ask, (int, float)) and price >= ask:
            aggr = +1
        raw[sess].setdefault(price, []).append((tt, size, aggr))
        st[f"live_{sess}"] += 1
        st[f"vol_{sess}"] += size
    out: dict[str, dict[int, tuple[list[float], list[int], list[int]]]] = {}
    for sess, d in raw.items():
        out[sess] = {}
        for price, lst in d.items():
            lst.sort()
            out[sess][price] = ([x[0] for x in lst], [x[1] for x in lst], [x[2] for x in lst])
    return out, st


class TradeIndex:
    """某個 session 的逐筆成交，依價位建索引；查 (price, t0, t1] 的累計口數。"""

    def __init__(self, per_price: dict[int, tuple[list[float], list[int], list[int]]]) -> None:
        self.d = {}
        for price, (times, sizes, aggr) in per_price.items():
            cum = [0]
            for s in sizes:
                cum.append(cum[-1] + s)
            cum_dir = {+1: [0], -1: [0]}
            for s, a in zip(sizes, aggr):
                cum_dir[+1].append(cum_dir[+1][-1] + (s if a == +1 else 0))
                cum_dir[-1].append(cum_dir[-1][-1] + (s if a == -1 else 0))
            self.d[price] = (times, cum, cum_dir)

    def vol(self, price: int, t0: float, t1: float, direction: int = 0) -> int:
        e = self.d.get(price)
        if e is None or t1 <= t0:
            return 0
        times, cum, cum_dir = e
        i0 = bisect.bisect_right(times, t0)
        i1 = bisect.bisect_right(times, t1)
        if direction == 0:
            return cum[i1] - cum[i0]
        return cum_dir[direction][i1] - cum_dir[direction][i0]


# ---------------------------------------------------------------- episodes
class LevelState:
    """一個 (side, price) 在可見簿子內的存在期。"""

    __slots__ = ("price", "side", "size", "t", "t0", "nobs", "anchors")

    def __init__(self, price: int, side: str, size: int, t: float) -> None:
        self.price = price
        self.side = side
        self.size = size
        self.t = t
        self.t0 = t
        self.nobs = 0
        # tag -> dict(anchor_t, anchor_size, removed, trade_attr, level_idx, dist, ratio)
        self.anchors: dict[str, dict[str, Any]] = {}


def _bucket(x: float, buckets) -> str:
    for lo, hi in buckets:
        if lo <= x <= hi if hi < 1e8 else lo <= x:
            return f"{lo:g}-{hi:g}" if hi < 1e8 else f"{lo:g}+"
    return "na"


def size_bucket(n: int) -> str:
    for lo, hi in SIZE_BUCKETS:
        if lo <= n <= hi:
            return f"{lo}-{hi}" if hi < 10**8 else f"{lo}+"
    return f"<{SIZE_BUCKETS[0][0]}"


def dist_bucket(d: float) -> str:
    for lo, hi in DIST_BUCKETS:
        if lo <= d < hi:
            return f"{lo:g}-{hi:g}" if hi < 1e8 else f"{lo:g}+"
    return "na"


def process_stream(
    snaps: list[dict[str, Any]],
    tidx: TradeIndex,
    day: str,
    sess: str,
    episodes: list[dict[str, Any]],
    st: Counter,
) -> None:
    baseline: dict[tuple[str, int], deque] = defaultdict(lambda: deque(maxlen=BASELINE_N))
    active: dict[tuple[str, int], LevelState] = {}
    prev_t: float | None = None
    prev_snap: dict[str, Any] | None = None

    def close(state: LevelState, fate: str, end_t: float, final_size: int) -> None:
        for tag, a in state.anchors.items():
            removed = a["removed"] + (final_size if fate == "REMOVED" else 0)
            trade_attr = a["trade_attr"]
            v_total = a["v_total"]
            if fate == "REMOVED" and final_size > 0:
                v = tidx.vol(state.price, a["last_t"], end_t + TRADE_TOL_SEC)
                trade_attr += min(v, final_size)
                v_total += v
            episodes.append(
                {
                    "day": day,
                    "sess": sess,
                    "side": state.side,
                    "price": state.price,
                    "tag": tag,
                    "fate": fate,
                    "anchor_t": a["anchor_t"],
                    "end_t": end_t,
                    "life": end_t - a["anchor_t"],
                    "anchor_size": a["anchor_size"],
                    "level": a["level"],
                    "dist": a["dist"],
                    "ratio": a["ratio"],
                    "age": a["age"],
                    "nobs": a["nobs"],
                    "removed": removed,
                    "trade_attr": trade_attr,
                    "v_total": v_total,
                    "peak": a["peak"],
                }
            )
            st[f"ep_{tag}_{fate}"] += 1

    for snap in snaps:
        t = snap["t"]
        if prev_t is not None and (t - prev_t) > GAP_SEC:
            for state in active.values():
                close(state, "TRUNCATED", prev_t, state.size)
            active.clear()
            prev_snap = None
            st["gap_break"] += 1
        mid = (snap["bp"][0] + snap["ap"][0]) / 2.0
        seen: set[tuple[str, int]] = set()
        for side, pk, sk in (("bid", "bp", "bs"), ("ask", "ap", "asz")):
            prices, sizes = snap[pk], snap[sk]
            for i in range(5):
                p, s = prices[i], sizes[i]
                key = (side, p)
                seen.add(key)
                base = baseline[(side, i)]
                med = None
                if len(base) >= BASELINE_MIN:
                    sb = sorted(base)
                    med = sb[len(sb) // 2]
                base.append(s)
                state = active.get(key)
                if state is None:
                    state = LevelState(p, side, s, t)
                    active[key] = state
                else:
                    delta = s - state.size
                    if state.anchors:
                        v = tidx.vol(p, state.t, t)
                        removed = max(0, -delta)
                        attr = min(v, removed)
                        for a in state.anchors.values():
                            a["removed"] += removed
                            a["trade_attr"] += attr
                            a["v_total"] += v
                            a["peak"] = max(a["peak"], s)
                            a["last_t"] = t
                    else:
                        pass
                    state.size = s
                    state.t = t
                state.nobs += 1
                # anchor 判定（causal：med 不含當下這筆）
                if med and med > 0:
                    ratio = s / med
                    dist = (mid - p) if side == "bid" else (p - mid)
                    tags = []
                    if s >= MIN_ABS:
                        for k in WALL_KS:
                            tag = f"wall{k:g}"
                            if ratio >= k and tag not in state.anchors:
                                tags.append((tag, ratio))
                        for lo, hi, tag in RATIO_BANDS:
                            if lo <= ratio < hi and tag not in state.anchors:
                                tags.append((tag, ratio))
                    if CTRL_LO <= ratio <= CTRL_HI and "ctrl" not in state.anchors:
                        tags.append(("ctrl", ratio))
                    if s >= MIN_ABS and CTRL_LO <= ratio <= CTRL_HI and "ctrl_big" not in state.anchors:
                        tags.append(("ctrl_big", ratio))
                    for tag, ratio_v in tags:
                        state.anchors[tag] = {
                            "anchor_t": t,
                            "last_t": t,
                            "anchor_size": s,
                            "peak": s,
                            "level": i + 1,
                            "dist": dist,
                            "ratio": ratio_v,
                            "removed": 0,
                            "trade_attr": 0,
                            "v_total": 0,
                            "age": t - state.t0,
                            "nobs": state.nobs,
                        }
                state.size = s
                state.t = t
        # 收掉這一筆快照裡消失的價位
        if prev_snap is not None:
            gone = [k for k in active if k not in seen]
            for key in gone:
                state = active.pop(key)
                side, p = key
                if side == "bid":
                    if p < snap["bp"][4]:
                        fate = "OUT_OF_VIEW"
                    elif p > snap["bp"][0]:
                        fate = "REMOVED"
                    else:
                        fate = "REMOVED"  # 可見價帶內的洞
                        st["hole_bid"] += 1
                else:
                    if p > snap["ap"][4]:
                        fate = "OUT_OF_VIEW"
                    elif p < snap["ap"][0]:
                        fate = "REMOVED"
                    else:
                        fate = "REMOVED"
                        st["hole_ask"] += 1
                if state.anchors:
                    close(state, fate, t, state.size)
        prev_snap = snap
        prev_t = t
    for state in active.values():
        if state.anchors:
            close(state, "TRUNCATED", prev_t or 0.0, state.size)


# ---------------------------------------------------------------- summarise
def frac(a: int, b: int) -> float:
    return round(a / b, 4) if b else float("nan")


def wilson(k: int, n: int) -> list[float]:
    """95% Wilson CI（僅供量級參考；日內樣本高度自相關，真實 CI 會更寬）。"""
    if n == 0:
        return [float("nan"), float("nan")]
    z = 1.96
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def summarise(eps: list[dict[str, Any]], tidx_by: dict[tuple[str, str], TradeIndex]) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def fate_table(sel) -> dict[str, Any]:
        c = Counter(e["fate"] for e in sel)
        n = len(sel)
        return {
            "n": n,
            "REMOVED": c["REMOVED"], "OUT_OF_VIEW": c["OUT_OF_VIEW"], "TRUNCATED": c["TRUNCATED"],
            "removed_frac": frac(c["REMOVED"], n),
            "out_of_view_frac": frac(c["OUT_OF_VIEW"], n),
            "truncated_frac": frac(c["TRUNCATED"], n),
        }

    def metricB(sel) -> dict[str, Any]:
        rem = [e for e in sel if e["fate"] == "REMOVED" and e["removed"] > 0]
        if not rem:
            return {"n_removed": 0}
        fr = [min(1.0, e["trade_attr"] / e["removed"]) for e in rem]
        fr_hi = [min(1.0, e["v_total"] / e["removed"]) for e in rem]
        fr_sorted = sorted(fr)
        tot_r = sum(e["removed"] for e in rem)
        tot_a = sum(e["trade_attr"] for e in rem)
        d = {
            "n_removed": len(rem),
            "eaten_frac_p50": round(fr_sorted[len(fr) // 2], 4),
            "eaten_frac_mean": round(sum(fr) / len(fr), 4),
            "volume_weighted_eaten_frac": round(tot_a / tot_r, 4),
            "eaten_frac_hi_p50": round(sorted(fr_hi)[len(fr_hi) // 2], 4),
            "eaten_frac_hi_mean": round(sum(fr_hi) / len(fr_hi), 4),
            "pure_cancel_share": frac(sum(1 for x in fr_hi if x <= 0.0), len(fr_hi)),
            "median_life_sec": round(sorted(e["life"] for e in rem)[len(rem) // 2], 3),
        }
        for th in THETAS:
            k = sum(1 for x in fr if x >= th)
            d[f"eaten_share_theta{th:g}"] = frac(k, len(rem))
            d[f"eaten_ci_theta{th:g}"] = wilson(k, len(rem))
        return d

    tags = ["wall2", "wall3", "wall5", "ctrl", "ctrl_big",
            "band1", "band2", "band3", "band4", "band5"]

    out["overall"] = {}
    for tag in tags:
        sel = [e for e in eps if e["tag"] == tag]
        out["overall"][tag] = {"fate": fate_table(sel), "metric_b": metricB(sel)}

    def group(keyfn, name):
        g: dict[str, Any] = {}
        for tag in tags:
            for e in eps:
                if e["tag"] != tag:
                    continue
                g.setdefault(str(keyfn(e)), {}).setdefault(tag, []).append(e)
        return {k: {t: {"fate": fate_table(v), "metric_b": metricB(v)} for t, v in d.items()}
                for k, d in sorted(g.items())}

    out["by_session"] = group(lambda e: e["sess"], "sess")
    out["by_day"] = group(lambda e: e["day"], "day")
    out["by_side"] = group(lambda e: e["side"], "side")
    out["by_size_bucket"] = group(lambda e: size_bucket(e["anchor_size"]), "size")
    out["by_dist_bucket"] = group(lambda e: dist_bucket(e["dist"]), "dist")
    out["by_level"] = group(lambda e: e["level"], "level")
    out["by_day_side_session"] = group(lambda e: f"{e['day']}|{e['sess']}|{e['side']}", "dss")
    out["by_age_bucket"] = group(lambda e: age_bucket(e["age"]), "age")
    out["by_level_x_age"] = group(lambda e: f"L{e['level']}|age{age_bucket(e['age'])}", "lxa")
    out["by_level_x_size"] = group(lambda e: f"L{e['level']}|sz{size_bucket(e['anchor_size'])}", "lxs")
    return out


def age_bucket(a: float) -> str:
    for lo, hi in ((0.0, 0.5), (0.5, 2.0), (2.0, 10.0), (10.0, 1e9)):
        if lo <= a < hi:
            return f"{lo:g}-{hi:g}" if hi < 1e8 else f"{lo:g}+"
    return "na"


def metric_a(eps: list[dict[str, Any]], tidx_by, tags=("wall3", "ctrl_big")) -> dict[str, Any]:
    """題目原文定義：結束前後短窗內的成交量 / anchor 口數 >= theta。"""
    res: dict[str, Any] = {}
    for tag in tags:
        sel = [e for e in eps if e["tag"] == tag and e["fate"] == "REMOVED"]
        res[tag] = {"n_removed": len(sel)}
        for w in WINDOWS_SEC:
            ratios = []
            for e in sel:
                ti = tidx_by[(e["day"], e["sess"])]
                v = ti.vol(e["price"], e["end_t"] - w, e["end_t"] + TRADE_TOL_SEC)
                ratios.append(v / max(1, e["anchor_size"]))
            for th in THETAS:
                k = sum(1 for r in ratios if r >= th)
                res[tag][f"w{w:g}s_theta{th:g}"] = frac(k, len(ratios))
            if ratios:
                rs = sorted(ratios)
                res[tag][f"w{w:g}s_ratio_p50"] = round(rs[len(rs) // 2], 4)
    return res


def cluster_contrast(eps, theta: float = 0.5) -> dict:
    """以 (day, session, side) 為叢集單位比較 wall3 vs ctrl_big 的被吃比例。

    日內樣本高度自相關，把每個 episode 當獨立觀察會嚴重高估檢定力；這裡改看
    「有幾個叢集的方向一致」，叢集數只有個位數，本來就只能當方向性線索。
    """
    clus: dict[str, dict[str, list]] = {}
    for e in eps:
        if e["tag"] not in ("wall3", "ctrl_big") or e["fate"] != "REMOVED" or e["removed"] <= 0:
            continue
        key = f"{e['day']}|{e['sess']}|{e['side']}"
        clus.setdefault(key, {}).setdefault(e["tag"], []).append(
            min(1.0, e["trade_attr"] / e["removed"])
        )
    rows = []
    for key in sorted(clus):
        d = clus[key]
        w, c = d.get("wall3", []), d.get("ctrl_big", [])
        if len(w) < 20 or len(c) < 20:
            continue
        sw = sum(1 for x in w if x >= theta) / len(w)
        sc = sum(1 for x in c if x >= theta) / len(c)
        rows.append({"cluster": key, "n_wall": len(w), "n_ctrl": len(c),
                     "wall_eaten_share": round(sw, 4), "ctrl_eaten_share": round(sc, 4),
                     "diff": round(sw - sc, 4)})
    pos = sum(1 for r in rows if r["diff"] > 0)
    return {"theta": theta, "clusters": rows, "n_clusters": len(rows),
            "n_clusters_wall_higher": pos,
            "sign_test_note": "叢集數 <10，符號檢定無檢定力，只當方向性線索"}


def headline_three_way(summary: dict, theta: float = 0.5) -> dict:
    """把每個 section 拉平成三分類：走開(視野外) / 被吃 / 被撤。

    純粹是 summary 的函數（fate 比例 × 被吃比例），所以可以用 --from-json
    在不重跑原始資料的情況下重算。
    """
    out: dict[str, Any] = {}
    for sec, body in summary.items():
        cell: dict[str, Any] = {}
        # "overall" 是 {tag: {...}}，其餘 section 是 {key: {tag: {...}}}
        norm = {"_all": body} if sec == "overall" else body
        for key, tags in norm.items():
            for tag, e in tags.items():
                f, b = e["fate"], e["metric_b"]
                if f["n"] < 40 or not b.get("n_removed"):
                    continue
                rm, oov = f["removed_frac"], f["out_of_view_frac"]
                es = b[f"eaten_share_theta{theta:g}"]
                cell.setdefault(key, {})[tag] = {
                    "n": f["n"],
                    "walked_away": round(oov, 4),
                    "eaten": round(rm * es, 4),
                    "cancelled": round(rm * (1 - es), 4),
                    "eaten_given_removed": es,
                    "median_wall_life_sec": b.get("median_life_sec"),
                }
        out[sec] = cell
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", nargs="*", default=["2026-08-17", "2026-08-18", "2026-08-19"])
    ap.add_argument("--out", default="reports/research/channel_lab/wall_a2_eaten_vs_cancelled.json")
    ap.add_argument("--from-json", action="store_true",
                    help="不重跑原始資料，只用既有 JSON 的 summary 重算 headline_three_way")
    args = ap.parse_args()

    if args.from_json:
        out = Path(args.out)
        payload = json.loads(out.read_text(encoding="utf-8"))
        payload["headline_three_way_theta0.5"] = headline_three_way(payload["summary"], 0.5)
        payload["headline_three_way_theta0.33"] = headline_three_way(payload["summary"], 0.33)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"rebuilt headline in {out}")
        return 0

    episodes: list[dict[str, Any]] = []
    tidx_by: dict[tuple[str, str], TradeIndex] = {}
    stats: dict[str, Any] = {}
    for day in args.days:
        books, bst = load_books(day)
        trades, tst = load_trades(day)
        st = Counter()
        for sess in ("day", "night"):
            snaps = books.get(sess) or []
            if not snaps or not (trades.get(sess) or {}):
                st[f"skipped_{sess}"] = len(snaps)
                continue
            ti = TradeIndex(trades.get(sess) or {})
            tidx_by[(day, sess)] = ti
            process_stream(snaps, ti, day, sess, episodes, st)
            st[f"snaps_{sess}"] = len(snaps)
        stats[day] = {"books": dict(bst), "trades": dict(tst), "episodes": dict(st)}
        print(f"[{day}] live books d/n={bst.get('live_day')}/{bst.get('live_night')} "
              f"trades d/n={tst.get('live_day')}/{tst.get('live_night')} eps={len(episodes)}")

    payload = {
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "question": "五檔牆消失時是被成交吃掉還是被撤單撤掉？牆 vs 非牆有無差別？",
        "definitions": {
            "wall_k": "size >= k * causal_rolling_median(same side, same level index, "
                      f"last {BASELINE_N} snapshots) 且 size >= {MIN_ABS} 口",
            "control(ctrl)": f"size/median 落在 [{CTRL_LO}, {CTRL_HI}]（不設口數下限）",
            "control(ctrl_big)": f"同上但額外要求 size >= {MIN_ABS} 口（size-matched 對照）",
            "anchor": "存在期內第一次滿足條件的快照；之後才開始累計（causal）",
            "fate.REMOVED": "價位從可見簿子近端消失或在可見價帶內留下洞",
            "fate.OUT_OF_VIEW": "價格走開、該價位掉出第五檔（第三類，不併入前兩類）",
            "fate.TRUNCATED": f"session 結束或相鄰 live 快照間隔 > {GAP_SEC}s，排除",
            "metric_b_eaten_frac": "Σ min(該價位區間成交量, 該區間 size 減少量) / Σ size 減少量（下界）",
            "metric_b_eaten_frac_hi": "Σ 該價位區間成交量 / Σ size 減少量（上界，容忍時鐘偏移）",
            "metric_a": "題目原文：[t_end-W, t_end+TOL] 內該價位成交量 / anchor 口數 >= theta",
            "trade_window_tolerance_sec": TRADE_TOL_SEC,
        },
        "data_caveats": [
            "只有 2026-08-17 / 08-18 兩個完整日 + 08-19 夜盤到 04:31；08-14/08-15 沒有逐筆成交檔。",
            "殭屍列已濾（stale 欄位優先，否則 ts-book_time > 5s）。",
            "book 是事件驅動快照，兩筆之間的中間狀態看不到 → 高頻加減單會被平滑掉。",
            "逐筆成交與五檔是兩條 feed，時鐘偏移約 0.4-0.5s；因此同時報下界與上界。",
        ],
        "ingest_stats": stats,
        "n_episodes": len(episodes),
        "summary": summarise(episodes, tidx_by),
        "metric_a": metric_a(episodes, tidx_by,
                             tags=("wall3", "wall5", "ctrl_big", "band1", "band2", "band3", "band4", "band5")),
        "cluster_contrast_theta0.5": cluster_contrast(episodes, 0.5),
        "cluster_contrast_theta0.33": cluster_contrast(episodes, 0.33),
    }
    payload["headline_three_way_theta0.5"] = headline_three_way(payload["summary"], 0.5)
    payload["headline_three_way_theta0.33"] = headline_three_way(payload["summary"], 0.33)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out}  episodes={len(episodes)}")
    ov = payload["summary"]["overall"]
    for tag in ("wall3", "ctrl_big", "ctrl"):
        f, b = ov[tag]["fate"], ov[tag]["metric_b"]
        print(f"{tag:>9}: n={f['n']:>7} removed={f['removed_frac']} oov={f['out_of_view_frac']} "
              f"| eaten>=0.5 {b.get('eaten_share_theta0.5')} "
              f"vw={b.get('volume_weighted_eaten_frac')} hi_mean={b.get('eaten_frac_hi_mean')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
