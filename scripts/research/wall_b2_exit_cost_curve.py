#!/usr/bin/env python3
"""B2 — 市價出場成本曲線：深度 × 時段 × 尾部 × breakeven 敏感度 × 限價出場替代方案。

為什麼要擴充 futopt_market_exit_slippage.py
------------------------------------------
那支腳本給了一個單點數字（TMF qty=1：日盤 1.67、夜盤 1.42 點），而整條 TMF 策略
的「換小台 MXF 就轉正」結論就押在它上面。本腳本沿用它的成本模型常數（fee/tax/
limit slip/gross），但補四件它沒做的事，外加修一個會影響數字的資料 bug：

  * 殭屍列（stale zombie）過濾：前一支腳本沒做，本腳本一律先做（有 ``stale`` 欄位就
    信它；舊資料用 age = ts − book_time > 5s），規則同
    scripts/research/tmf_book_microstructure_diag.py::load_live_books。
    **實測結論：在這個量測上幾乎沒差（qty=1 平均滑價 −0.0003 點）。**
    原因是殭屍列只在 session 續約時每小時各出現一列（08-17 全天 273,288 列中
    只有 422 列），而且它們與下一列相隔約 58 分鐘，早就被「間距 > 60s 不計入
    時間加權」的規則排掉了。這條紀錄本身就是產出：不要再擔心這支數字被殭屍污染，
    但也不要把「殭屍不影響」推廣到別的量測（存續期、牆壁存活時間就會被嚴重污染）。
    ``--compare-unfiltered`` 可重現這個對照。

  A. 深度曲線     qty = 1,2,3,5,10 的滑價 + 五檔吃不滿的比例 + 五檔總深度分布
  B. 時段結構     逐小時（不只日盤/夜盤兩分）+ 開盤/收盤前/清晨等命名時段
  C. 尾部         時間加權 p50/p90/p99/max，以及最壞時刻集中在哪個小時
  D. breakeven    毛額(2.0/2.86/3.5) × 市價出場比例(60/78/90%) × 商品(TMF/MXF/TXF)
                  ——TMF 是實測、MXF/TXF **是外推**，JSON 內每一格都有 measured 旗標
  E. 限價出場替代 queue-aware tick 重放：掛在觸價、T 秒內沒成交就轉市價。
                  回答「省多少、代價是什麼」，代價用「T 秒後才市價出場」量化。

因果邊界（硬性）：E 的每一個樣本只用 (t, t+T] 內的成交，錨點 book 只用 book_time ≤ t
的那一筆。切片在 _simulate_limit_exit() 內寫死，不吃任何未來資料。

用法
----
    PYTHONPATH=src .venv/bin/python scripts/research/wall_b2_exit_cost_curve.py \
        --days 2026-08-14,2026-08-15,2026-08-17,2026-08-18,2026-08-19 \
        --out reports/research/channel_lab/wall_b2_exit_cost_curve.json
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

TZ = timezone(timedelta(hours=8))
MAX_BOOK_AGE_SEC = 5.0        # 與 tmf_book_microstructure_diag 相同的殭屍門檻
MAX_GAP_SEC = 60.0            # 快照間距超過此值視為斷線缺口，不計入時間加權
ROOT_DIR = Path(__file__).resolve().parents[2]

QTYS = (1, 2, 3, 5, 10)

# ── 成本模型常數（SSOT 同 scripts/research/futopt_market_exit_slippage.py）────────
CONTRACT_SPEC = {
    "TMF": {"point_value": 10.0, "fee_ntd": 15.0, "label": "微型臺指"},
    "MXF": {"point_value": 50.0, "fee_ntd": 15.0, "label": "小型臺指"},
    "TXF": {"point_value": 200.0, "fee_ntd": 15.0, "label": "大型臺指"},
}
TAX_PTS_PER_SIDE = 0.9236          # 來自 25 筆真實成交；點數上三個契約相同，換契約省不掉
LIMIT_SLIP_PTS_PER_SIDE = -0.40    # 限價那一側的實測滑價（負＝對己方有利）
GROSS_GRID = (2.0, 2.86, 3.5)
SHARE_GRID = (0.60, 0.78, 0.90)
#: MXF/TXF **沒有實測簿子**。外推用的價差情境（點）。1.0 = 最小跳動單位，
#: 也就是「幾乎永遠一檔價差」的樂觀情境；3.0 ≈ 目前 TMF 實測水準（悲觀）。
EXTRAP_SPREAD_GRID = (1.0, 2.0, 3.0)
EXTRAP_BASE_SPREAD = 1.0
#: MXF/TXF 的手續費（TWD/邊）也沒實測。TMF 的 15 元是券商回報實數；大契約通常較高。
EXTRAP_FEE_GRID = (15.0, 30.0, 50.0)
EXTRAP_BASE_FEE = 30.0

PHASES = (
    ("day_open_15m", "day", "08:45", "09:00"),
    ("day_0900_1200", "day", "09:00", "12:00"),
    ("day_1200_1330", "day", "12:00", "13:30"),
    ("day_close_15m", "day", "13:30", "13:46"),
    ("night_open_15m", "night", "15:00", "15:15"),
    ("night_1515_1800", "night", "15:15", "18:00"),
    ("night_1800_2100", "night", "18:00", "21:00"),
    ("night_2100_0000", "night", "21:00", "24:00"),
    ("night_0000_0300", "night", "00:00", "03:00"),
    ("night_0300_0500", "night", "03:00", "05:01"),
)


def cache_dir() -> Path:
    try:
        import stock_db

        return Path(stock_db.DATA_DIR).parent / "cache"
    except Exception:  # noqa: BLE001
        return Path.home() / "goldenstocks-data" / "cache"


# ── 載入 ────────────────────────────────────────────────────────────────────────
def load_live_books(day: str, keep_zombies: bool = False) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """回傳依 book_time 排序的活簿快照。過濾規則同 tmf_book_microstructure_diag。"""
    path = cache_dir() / "tmf_books" / f"tmf_books_{day}.jsonl"
    stats: Counter = Counter()
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out, dict(stats)
    for line in path.open(encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        stats["rows"] += 1
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            stats["bad_json"] += 1
            continue
        bids, asks = r.get("bids") or [], r.get("asks") or []
        if not bids or not asks:
            stats["empty_side"] += 1
            continue
        try:
            wall = datetime.fromisoformat(str(r["ts"])).astimezone(TZ)
            book_ts = datetime.fromtimestamp(float(r["book_time"]) / 1e6, tz=TZ)
        except (KeyError, TypeError, ValueError):
            stats["bad_ts"] += 1
            continue
        if "stale" in r:
            is_stale = bool(r["stale"])
        else:
            is_stale = (wall - book_ts).total_seconds() > MAX_BOOK_AGE_SEC
        if is_stale:
            stats["stale_zombie"] += 1
            if not keep_zombies:
                continue
        else:
            stats["live"] += 1
        # session：收集器寫的欄位優先；沒有就用 quote_type（FUTURE=日盤／FUTURE_AH=夜盤）
        sess = r.get("session")
        if sess not in ("day", "night"):
            sess = "night" if str(r.get("quote_type", "")).endswith("_AH") else "day"
        bp = [float(b["price"]) for b in bids]
        bs = [int(b["size"]) for b in bids]
        ap = [float(a["price"]) for a in asks]
        asz = [int(a["size"]) for a in asks]
        if ap[0] <= bp[0]:
            stats["crossed"] += 1
            continue
        out.append({"t": book_ts.timestamp(), "hm": book_ts.strftime("%H:%M"),
                    "hour": book_ts.hour, "sess": sess,
                    "bp": bp, "bs": bs, "ap": ap, "asz": asz})
    out.sort(key=lambda r: r["t"])
    return out, dict(stats)


def load_live_trades(day: str) -> tuple[list[tuple[float, float, int]], dict[str, int]]:
    """(trade_time_epoch, price, size)，已過濾殭屍重送列。"""
    path = cache_dir() / "tmf_trades" / f"tmf_trades_{day}.jsonl"
    stats: Counter = Counter()
    out: list[tuple[float, float, int]] = []
    if not path.exists():
        return out, dict(stats)
    for line in path.open(encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        stats["rows"] += 1
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            wall = datetime.fromisoformat(str(r["ts"])).timestamp()
            tt = float(r["trade_time"]) / 1e6
            px = float(r["price"])
            sz = int(r["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if wall - tt > MAX_BOOK_AGE_SEC:   # 收盤側成交被原樣重送
            stats["stale_zombie"] += 1
            continue
        stats["live"] += 1
        out.append((tt, px, sz))
    out.sort(key=lambda x: x[0])
    return out, dict(stats)


# ── 基礎工具 ────────────────────────────────────────────────────────────────────
def walk(prices: list[float], sizes: list[int], qty: int) -> tuple[float | None, int]:
    """吃掉 qty 口的成交均價；回 (均價 or None, 實際成交口數)。"""
    filled = 0
    cost = 0.0
    for p, s in zip(prices, sizes):
        take = min(s, qty - filled)
        if take <= 0:
            break
        cost += p * take
        filled += take
        if filled >= qty:
            break
    return (cost / filled if filled else None), filled


def wstats(pairs: list[tuple[float, float]]) -> dict[str, float]:
    """時間加權統計。pairs = [(weight, value)]。"""
    if not pairs:
        return {}
    tw = sum(w for w, _ in pairs)
    if tw <= 0:
        return {}
    mean = sum(w * v for w, v in pairs) / tw
    srt = sorted(pairs, key=lambda p: p[1])
    out = {"n": len(pairs), "weight_sec": round(tw, 1), "mean": round(mean, 4)}
    targets = {"p50": 0.50, "p90": 0.90, "p99": 0.99}
    acc = 0.0
    keys = sorted(targets.items(), key=lambda kv: kv[1])
    ki = 0
    for w, v in srt:
        acc += w
        while ki < len(keys) and acc >= keys[ki][1] * tw:
            out[keys[ki][0]] = round(v, 4)
            ki += 1
    while ki < len(keys):
        out[keys[ki][0]] = round(srt[-1][1], 4)
        ki += 1
    out["max"] = round(srt[-1][1], 4)
    return out


def _hm_in(hm: str, lo: str, hi: str) -> bool:
    return lo <= hm < hi


def phase_of(sess: str, hm: str) -> str | None:
    for name, s, lo, hi in PHASES:
        if s == sess and _hm_in(hm, lo, hi):
            return name
    return None


# ── A/B/C：深度 × 時段 × 尾部 ──────────────────────────────────────────────────
def scan_books(days: list[str], keep_zombies: bool = False) -> dict[str, Any]:
    """單次掃描算出所有 qty / 時段 / 尾部統計。"""
    # bucket -> qty -> [(w, slip)]
    buckets: dict[str, dict[int, list[tuple[float, float]]]] = defaultdict(lambda: defaultdict(list))
    spread_buckets: dict[str, list[tuple[float, float]]] = defaultdict(list)
    depth_buckets: dict[str, list[tuple[float, float]]] = defaultdict(list)
    shallow_w: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    total_w: dict[str, float] = defaultdict(float)
    load_stats: dict[str, dict[str, int]] = {}
    dropped_gap_w = 0.0
    dropped_gap_n = 0
    kept_n = 0

    for day in days:
        rows, st = load_live_books(day, keep_zombies=keep_zombies)
        load_stats[day] = st
        if len(rows) < 2:
            continue
        for i in range(len(rows) - 1):
            r = rows[i]
            dur = rows[i + 1]["t"] - r["t"]
            if dur <= 0:
                continue
            if dur > MAX_GAP_SEC:
                dropped_gap_w += dur
                dropped_gap_n += 1
                continue
            kept_n += 1
            mid = (r["ap"][0] + r["bp"][0]) / 2.0
            spread = r["ap"][0] - r["bp"][0]
            dep_b, dep_a = sum(r["bs"]), sum(r["asz"])
            hm = r["hm"]
            ph = phase_of(r["sess"], hm)
            keys = ["all", f"sess:{r['sess']}", f"day:{day}", f"hour:{r['hour']:02d}",
                    f"sesshour:{r['sess']}:{r['hour']:02d}", f"sessday:{r['sess']}:{day}"]
            if ph:
                keys.append(f"phase:{ph}")
            for q in QTYS:
                bpx, nb = walk(r["ap"], r["asz"], q)     # 市價買 → 吃 asks
                spx, ns = walk(r["bp"], r["bs"], q)      # 市價賣 → 吃 bids
                short = (nb < q) or (ns < q)
                if bpx is None or spx is None:
                    continue
                # 深度不足時，用「吃完可得部分」的均價（下界；真實成本更高）
                slip = ((bpx - mid) + (mid - spx)) / 2.0
                for k in keys:
                    buckets[k][q].append((dur, slip))
                    if short:
                        shallow_w[k][q] += dur
            for k in keys:
                spread_buckets[k].append((dur, spread))
                depth_buckets[k].append((dur, float(min(dep_b, dep_a))))
                total_w[k] += dur

    out: dict[str, Any] = {"load_stats": load_stats,
                           "gap_dropped": {"n": dropped_gap_n, "weight_sec": round(dropped_gap_w, 1)},
                           "kept_intervals": kept_n,
                           "buckets": {}}
    for k in sorted(buckets):
        entry: dict[str, Any] = {
            "weight_sec": round(total_w[k], 1),
            "spread": wstats(spread_buckets[k]),
            "min_side_5lvl_depth": wstats(depth_buckets[k]),
            "qty": {},
        }
        for q in QTYS:
            s = wstats(buckets[k][q])
            if not s:
                continue
            s["shallow_pct_timeweighted"] = round(100 * shallow_w[k][q] / max(total_w[k], 1e-9), 4)
            entry["qty"][str(q)] = s
        out["buckets"][k] = entry
    return out


def worst_moment_concentration(days: list[str], qty: int, thresholds: Iterable[float]) -> dict[str, Any]:
    """尾部集中在哪：滑價 ≥ 門檻的時間，按 (session, hour) 分配。"""
    res: dict[str, Any] = {}
    tot_w: dict[float, float] = defaultdict(float)
    by_key: dict[float, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    all_w = 0.0
    for day in days:
        rows, _ = load_live_books(day)
        for i in range(len(rows) - 1):
            r = rows[i]
            dur = rows[i + 1]["t"] - r["t"]
            if not (0 < dur <= MAX_GAP_SEC):
                continue
            mid = (r["ap"][0] + r["bp"][0]) / 2.0
            bpx, nb = walk(r["ap"], r["asz"], qty)
            spx, ns = walk(r["bp"], r["bs"], qty)
            if bpx is None or spx is None:
                continue
            slip = ((bpx - mid) + (mid - spx)) / 2.0
            all_w += dur
            key = f"{r['sess']}:{r['hour']:02d}"
            for th in thresholds:
                if slip >= th:
                    tot_w[th] += dur
                    by_key[th][key] += dur
    for th in thresholds:
        share = {k: round(100 * v / max(tot_w[th], 1e-9), 2) for k, v in
                 sorted(by_key[th].items(), key=lambda kv: -kv[1])}
        res[f">={th}"] = {
            "pct_of_all_time": round(100 * tot_w[th] / max(all_w, 1e-9), 3),
            "top_buckets_pct_of_tail": dict(list(share.items())[:8]),
        }
    return res


# ── D：breakeven 敏感度 ────────────────────────────────────────────────────────
def round_trip_cost(root: str, market_exit_slip: float, market_share: float,
                    fee_ntd: float | None = None,
                    entry_slip: float = LIMIT_SLIP_PTS_PER_SIDE) -> float:
    spec = CONTRACT_SPEC[root]
    fee = spec["fee_ntd"] if fee_ntd is None else fee_ntd
    fee_pts = fee / spec["point_value"]
    blended_exit = market_share * market_exit_slip + (1 - market_share) * LIMIT_SLIP_PTS_PER_SIDE
    return 2 * fee_pts + 2 * TAX_PTS_PER_SIDE + entry_slip + blended_exit


def max_tolerable_spread(root: str, gross: float, market_share: float,
                         fee_ntd: float | None = None,
                         entry_slip: float = LIMIT_SLIP_PTS_PER_SIDE) -> float:
    """反推：市價出場滑價 = 半價差，則價差最多可以到幾點還能損益兩平。"""
    spec = CONTRACT_SPEC[root]
    fee = spec["fee_ntd"] if fee_ntd is None else fee_ntd
    fee_pts = fee / spec["point_value"]
    fixed = 2 * fee_pts + 2 * TAX_PTS_PER_SIDE + entry_slip + (1 - market_share) * LIMIT_SLIP_PTS_PER_SIDE
    return 2 * (gross - fixed) / market_share


def breakeven_grid(tmf_slip_day: float, tmf_slip_night: float, tmf_slip_all: float,
                   entry_slip: float = LIMIT_SLIP_PTS_PER_SIDE) -> dict[str, Any]:
    cells = []
    for root in ("TMF", "MXF", "TXF"):
        measured = root == "TMF"
        if measured:
            slips = {"measured_all": tmf_slip_all, "measured_day": tmf_slip_day,
                     "measured_night": tmf_slip_night}
            fees = {"measured_15twd": CONTRACT_SPEC[root]["fee_ntd"]}
        else:
            slips = {f"extrap_spread_{s:g}pt": s / 2.0 for s in EXTRAP_SPREAD_GRID}
            fees = {f"extrap_fee_{f:g}twd": f for f in EXTRAP_FEE_GRID}
        for slip_name, slip in slips.items():
            for fee_name, fee in fees.items():
                for gross in GROSS_GRID:
                    for share in SHARE_GRID:
                        cost = round_trip_cost(root, slip, share, fee, entry_slip)
                        cells.append({
                            "root": root,
                            "measured": measured,
                            "slip_scenario": slip_name,
                            "exit_market_slip_pts": round(slip, 4),
                            "fee_scenario": fee_name,
                            "fee_ntd_per_side": fee,
                            "gross_pts": gross,
                            "market_exit_share": share,
                            "round_trip_cost_pts": round(cost, 4),
                            "net_pts_per_trade": round(gross - cost, 4),
                            "net_ntd_per_trade": round((gross - cost) * CONTRACT_SPEC[root]["point_value"], 1),
                            "positive": bool(gross - cost > 0),
                        })
    tol = []
    for root in ("TMF", "MXF", "TXF"):
        fee_opts = ([CONTRACT_SPEC[root]["fee_ntd"]] if root == "TMF" else list(EXTRAP_FEE_GRID))
        for fee in fee_opts:
            for gross in GROSS_GRID:
                for share in SHARE_GRID:
                    tol.append({"root": root, "fee_ntd_per_side": fee, "gross_pts": gross,
                                "market_exit_share": share,
                                "max_tolerable_spread_pts": round(
                                    max_tolerable_spread(root, gross, share, fee, entry_slip), 4)})
    return {"cells": cells, "max_tolerable_spread": tol,
            "extrapolation_note": (
                "MXF/TXF 完全沒有實測簿子（收集器解析不到那兩個代碼），所有 MXF/TXF 數字都是"
                "外推：假設『市價滑價 = 半個價差』並直接指定價差情境 1/2/3 點，以及手續費 15/30/50 元。"
                "TMF 的 15 元／1.67 點是實測，MXF/TXF 的沒有任何一個是。"),
            "entry_slip_pts": entry_slip}


# ── E：限價出場 vs 市價出場（queue-aware tick 重放）──────────────────────────────
def simulate_limit_exit(days: list[str], horizons: tuple[int, ...], stride_sec: float,
                        qty: int = 1) -> dict[str, Any]:
    """掛在觸價、T 秒內未成交就轉市價 —— 相對「立刻市價」省下／賠掉多少點。

    因果切片：錨點只用 book_time ≤ t 的最後一筆 book；成交只看 (t, t+T]；
    fallback 的 book 只用 book_time ≤ t+T 的最後一筆。函式外不得傳入任何未來資料。

    排隊規則（保守）：賣限價掛在 best ask A，排在既有 QA 口之後。
      成交條件 = (t, t+T] 內成交價 ≥ A 的累計口數 > QA，或出現成交價 > A。
      忽略「排在我前面的人取消」（會低估成交率）與「我自己的口數推升 QA」。
    """
    per_day: dict[str, Any] = {}
    agg: dict[tuple[str, int], list[dict[str, float]]] = defaultdict(list)

    for day in days:
        rows, _ = load_live_books(day)
        trades, _ = load_live_trades(day)
        if len(rows) < 10 or len(trades) < 10:
            continue
        btimes = [r["t"] for r in rows]
        ttimes = [t[0] for t in trades]
        maxT = max(horizons)
        day_res: dict[str, Any] = {}
        samples: dict[tuple[str, int], list[dict[str, float]]] = defaultdict(list)

        # 以 stride 秒等距取樣（時間加權天然成立）
        t0, t1 = rows[0]["t"], rows[-1]["t"]
        n_t = int((t1 - t0) / stride_sec)
        for k in range(n_t):
            t = t0 + k * stride_sec
            i = bisect.bisect_right(btimes, t) - 1
            if i < 0:
                continue
            r = rows[i]
            if t - r["t"] > MAX_GAP_SEC:      # 這一刻其實沒有活簿（休息時間）
                continue
            A, QA = r["ap"][0], r["asz"][0]
            B, QB = r["bp"][0], r["bs"][0]
            sess = r["sess"]
            # 因果：只用 book_time ≤ t−60 的那一筆算前置 60 秒報酬。
            # 用途＝把「隨機時刻出場」換成「剛被打到停損那種時刻出場」。
            mid_now = (A + B) / 2.0
            i0 = bisect.bisect_right(btimes, t - 60.0) - 1
            ret60 = math.nan
            if i0 >= 0:
                r0 = rows[i0]
                if (t - 60.0) - r0["t"] <= MAX_GAP_SEC and r0["sess"] == sess:
                    ret60 = mid_now - (r0["ap"][0] + r0["bp"][0]) / 2.0
            # 掃 (t, t+maxT] 的成交，記錄各邊被吃掉的累計量與跨越時刻
            j = bisect.bisect_right(ttimes, t)
            cum_ask, cum_bid = 0, 0
            fill_sell_at: float | None = None      # 賣限價（掛 A）成交時刻
            fill_buy_at: float | None = None       # 買限價（掛 B）成交時刻
            while j < len(trades) and trades[j][0] <= t + maxT:
                tt, px, sz = trades[j]
                if fill_sell_at is None:
                    if px > A:
                        fill_sell_at = tt
                    elif px >= A:
                        cum_ask += sz
                        if cum_ask > QA:
                            fill_sell_at = tt
                if fill_buy_at is None:
                    if px < B:
                        fill_buy_at = tt
                    elif px <= B:
                        cum_bid += sz
                        if cum_bid > QB:
                            fill_buy_at = tt
                if fill_sell_at is not None and fill_buy_at is not None:
                    break
                j += 1
            for T in horizons:
                # fallback：t+T 當下的活簿
                m = bisect.bisect_right(btimes, t + T) - 1
                if m < 0:
                    continue
                rr = rows[m]
                if (t + T) - rr["t"] > MAX_GAP_SEC or rr["sess"] != sess:
                    continue
                fb_bid, nfb_b = walk(rr["bp"], rr["bs"], qty)
                fb_ask, nfb_a = walk(rr["ap"], rr["asz"], qty)
                if fb_bid is None or fb_ask is None:
                    continue
                mkt_sell_now, _ = walk(r["bp"], r["bs"], qty)
                mkt_buy_now, _ = walk(r["ap"], r["asz"], qty)
                if mkt_sell_now is None or mkt_buy_now is None:
                    continue
                sell_filled = fill_sell_at is not None and fill_sell_at <= t + T
                buy_filled = fill_buy_at is not None and fill_buy_at <= t + T
                # 賣出場：市價現在拿 mkt_sell_now；限價成功拿 A；失敗拿 t+T 的市價
                gain_sell = (A if sell_filled else fb_bid) - mkt_sell_now
                # 買出場：市價現在付 mkt_buy_now；限價成功付 B；失敗付 t+T 的市價
                gain_buy = mkt_buy_now - (B if buy_filled else fb_ask)
                samples[(sess, T)].append({
                    "ret60": ret60,
                    "gain": (gain_sell + gain_buy) / 2.0,
                    "gain_sell": gain_sell, "gain_buy": gain_buy,
                    "filled": (int(sell_filled) + int(buy_filled)) / 2.0,
                    "gain_if_filled_sell": gain_sell if sell_filled else math.nan,
                    "gain_if_unfilled_sell": math.nan if sell_filled else gain_sell,
                    "gain_if_filled_buy": gain_buy if buy_filled else math.nan,
                    "gain_if_unfilled_buy": math.nan if buy_filled else gain_buy,
                })
        for (sess, T), rowsx in samples.items():
            day_res[f"{sess}:T{T}"] = _summarise_limit(rowsx)
            agg[(sess, T)].extend(rowsx)
        per_day[day] = day_res

    overall = {f"{sess}:T{T}": _summarise_limit(v) for (sess, T), v in sorted(agg.items())}
    stress = {f"{sess}:T{T}": _summarise_stress(v) for (sess, T), v in sorted(agg.items())}
    return {"per_day": per_day, "overall": overall, "stress": stress,
            "stride_sec": stride_sec, "qty": qty, "horizons": list(horizons),
            "stress_note": (
                "overall 是『隨機時刻出場』。但策略 78% 的市價出場是被停損／struct_break "
                "觸發的，也就是**價格正在往不利方向跑**的時刻——那正是限價掛不到的時刻。"
                "stress 用因果的前置 60 秒 mid 變動當代理：賣出場只取 ret60 ≤ −k 的樣本，"
                "買出場只取 ret60 ≥ +k。這是條件化後的上界估計，仍不是策略真實出場時點。")}


def _summarise_stress(rows: list[dict[str, float]], ks: tuple[float, ...] = (0.0, 3.0, 6.0, 10.0)) -> dict[str, Any]:
    """條件在「前 60 秒 mid 已往不利方向跑 ≥ k 點」的子樣本上重算限價出場優勢。"""
    out: dict[str, Any] = {}
    for k in ks:
        sell = [r for r in rows if not math.isnan(r["ret60"]) and r["ret60"] <= -k]
        buy = [r for r in rows if not math.isnan(r["ret60"]) and r["ret60"] >= k]
        if len(sell) < 30 or len(buy) < 30:
            continue
        fr_s = _mean([0.0 if math.isnan(r["gain_if_filled_sell"]) else 1.0 for r in sell])
        fr_b = _mean([0.0 if math.isnan(r["gain_if_filled_buy"]) else 1.0 for r in buy])
        g_s = _mean([r["gain_sell"] for r in sell])
        g_b = _mean([r["gain_buy"] for r in buy])
        out[f"adverse_ret60_ge_{k:g}pt"] = {
            "n_sell": len(sell), "n_buy": len(buy),
            "fill_rate_sell": round(fr_s, 4), "fill_rate_buy": round(fr_b, 4),
            "mean_gain_sell_pts": round(g_s, 4), "mean_gain_buy_pts": round(g_b, 4),
            "mean_gain_pts": round((g_s + g_b) / 2.0, 4),
            "fill_rate": round((fr_s + fr_b) / 2.0, 4),
        }
    return out


def _mean(xs: list[float]) -> float:
    xs = [x for x in xs if not math.isnan(x)]
    return sum(xs) / len(xs) if xs else math.nan


def _summarise_limit(rows: list[dict[str, float]]) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {}
    fill = _mean([r["filled"] for r in rows])
    gain = _mean([r["gain"] for r in rows])
    gf = _mean([r["gain_if_filled_sell"] for r in rows] + [r["gain_if_filled_buy"] for r in rows])
    gu = _mean([r["gain_if_unfilled_sell"] for r in rows] + [r["gain_if_unfilled_buy"] for r in rows])
    nu = sum(1 for r in rows if math.isnan(r["gain_if_filled_sell"])) + \
        sum(1 for r in rows if math.isnan(r["gain_if_filled_buy"]))
    return {
        "n_samples": n,
        "fill_rate": round(fill, 4),
        "fill_rate_sell": round(_mean([1.0 if not math.isnan(r["gain_if_filled_sell"]) else 0.0 for r in rows]), 4),
        "fill_rate_buy": round(_mean([1.0 if not math.isnan(r["gain_if_filled_buy"]) else 0.0 for r in rows]), 4),
        "mean_gain_sell_pts": round(_mean([r["gain_sell"] for r in rows]), 4),
        "mean_gain_buy_pts": round(_mean([r["gain_buy"] for r in rows]), 4),
        "mean_gain_vs_market_now_pts": round(gain, 4),
        "mean_gain_when_filled_pts": None if math.isnan(gf) else round(gf, 4),
        "mean_gain_when_unfilled_pts": None if math.isnan(gu) else round(gu, 4),
        "n_unfilled_legs": nu,
    }


# ── Q5：把 78% 市價出場改成限價出場，省多少？────────────────────────────────────
def q5_savings(payload: dict, horizon: int = 60) -> dict[str, Any]:
    """用已算好的 book_scan + limit_exit_sim 回答「改限價出場省多少、剩下多少缺口」。

    saving_per_trade = MARKET_EXIT_SHARE × gain(limit-with-T-fallback vs market-now)
    （只有目前走市價的那 78% 會被改變；另外 22% 本來就是限價。）
    """
    bs = payload["book_scan"]["buckets"]
    sim = payload["limit_exit_sim"]
    out: dict[str, Any] = {"horizon_sec": horizon, "market_exit_share": 0.78, "by_session": {}}
    for sess in ("day", "night"):
        slip = bs[f"sess:{sess}"]["qty"]["1"]["mean"]
        uncond = (sim["overall"].get(f"{sess}:T{horizon}") or {}).get("mean_gain_vs_market_now_pts")
        stress = ((sim["stress"].get(f"{sess}:T{horizon}") or {})
                  .get("adverse_ret60_ge_3pt", {})).get("mean_gain_pts")
        row: dict[str, Any] = {"market_exit_slip_pts": round(slip, 4),
                               "gain_uncond_pts": uncond, "gain_stress_ret60_3pt_pts": stress,
                               "scenarios": {}}
        for name, g in (("uncond", uncond), ("stress", stress)):
            if g is None:
                continue
            saving = 0.78 * g
            row["scenarios"][name] = {
                "saving_pts_per_trade": round(saving, 4),
                "saving_ntd_per_trade_TMF": round(saving * CONTRACT_SPEC["TMF"]["point_value"], 1),
                "tmf_net_before": round(2.86 - round_trip_cost("TMF", slip, 0.78), 4),
                "tmf_net_after": round(2.86 - round_trip_cost("TMF", slip, 0.78) + saving, 4),
                "tmf_still_negative": bool(2.86 - round_trip_cost("TMF", slip, 0.78) + saving < 0),
            }
        row["fill_rate"] = (sim["overall"].get(f"{sess}:T{horizon}") or {}).get("fill_rate")
        out["by_session"][sess] = row
    out["cost_note"] = (
        "省下的是點數；代價有兩層。第一層本模擬已內含：掛不到就在 T 秒後轉市價，"
        "day T=60 有 9.4% 的腿掛不到、平均多付 21.1 點；night 17.3% 掛不到、平均多付 10.4 點。"
        "第二層量不到：持倉多留最多 T 秒會改變策略本身的損益分布（停損被延後觸發、"
        "下一筆訊號被佔住槽位），本研究無法評估。")
    return out


# ── 主程式 ─────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", default="2026-08-14,2026-08-15,2026-08-17,2026-08-18,2026-08-19")
    ap.add_argument("--horizons", default="15,30,60,120,300")
    ap.add_argument("--stride", type=float, default=10.0)
    ap.add_argument("--skip-limit-sim", action="store_true")
    ap.add_argument("--q5-only", action="store_true",
                    help="只用既有 JSON 重算 Q5（限價出場省多少）並併回")
    ap.add_argument("--sim-only", action="store_true",
                    help="只重跑 E（限價出場模擬），把結果併回既有 JSON 的 limit_exit_sim 欄位")
    ap.add_argument("--compare-unfiltered", action="store_true",
                    help="同時跑一份「不過濾殭屍」的版本，量化前一支腳本的偏誤")
    ap.add_argument("--out", default="reports/research/channel_lab/wall_b2_exit_cost_curve.json")
    args = ap.parse_args()

    days = [d.strip() for d in args.days.split(",") if d.strip()]
    horizons = tuple(int(x) for x in args.horizons.split(","))

    out_path0 = Path(args.out)
    if not out_path0.is_absolute():
        out_path0 = ROOT_DIR / out_path0
    if args.q5_only:
        payload = json.loads(out_path0.read_text(encoding="utf-8"))
        q5 = q5_savings(payload)
        payload["q5_limit_exit_savings"] = q5
        for sess, r in q5["by_session"].items():
            print(f"  [{sess}] 市價滑價 {r['market_exit_slip_pts']:.3f} 點／T=60 成交率 {r['fill_rate']:.3f}")
            for name, sc in r["scenarios"].items():
                print(f"      {name:<7} 每筆省 {sc['saving_pts_per_trade']:+.3f} 點"
                      f"（TMF {sc['saving_ntd_per_trade_TMF']:+,.0f} 元）  "
                      f"TMF 淨額 {sc['tmf_net_before']:+.3f} → {sc['tmf_net_after']:+.3f}"
                      f"{'（仍為負）' if sc['tmf_still_negative'] else '（轉正）'}")
        out_path0.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n更新 {out_path0}")
        return 0
    if args.sim_only:
        payload = json.loads(out_path0.read_text(encoding="utf-8"))
        sim = simulate_limit_exit(days, horizons, args.stride)
        payload["limit_exit_sim"] = sim
        for k, v in sim["overall"].items():
            if v:
                print(f"    {k:<14} n={v['n_samples']:>6,}  成交率={v['fill_rate']:.3f}  "
                      f"淨益={v['mean_gain_vs_market_now_pts']:+.3f} 點  "
                      f"(成交 {v['mean_gain_when_filled_pts']:+.3f}／未成交 "
                      f"{v['mean_gain_when_unfilled_pts']:+.3f})")
        print("\n  -- 壓力情境（T=60）--")
        for kk in ("day:T60", "night:T60"):
            for cond, v in (sim["stress"].get(kk) or {}).items():
                print(f"    {kk:<11} {cond:<24} n={v['n_sell']:>5,}/{v['n_buy']:<5,} "
                      f"成交率={v['fill_rate']:.3f}  淨益={v['mean_gain_pts']:+.3f} 點 "
                      f"[賣 {v['mean_gain_sell_pts']:+.3f}／買 {v['mean_gain_buy_pts']:+.3f}]")
        print("\n  -- 逐日 T=60 --")
        for d, dv in sim["per_day"].items():
            for sess in ("day", "night"):
                v = dv.get(f"{sess}:T60")
                if v:
                    print(f"    {d} {sess:<6} n={v['n_samples']:>5,} 成交率={v['fill_rate']:.3f} "
                          f"淨益={v['mean_gain_vs_market_now_pts']:+.3f}")
        out_path0.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n更新 {out_path0}")
        return 0

    print("=" * 84)
    print("A/B/C — 深度曲線 × 時段 × 尾部（已過濾 stale zombie）")
    scan = scan_books(days)
    for d, st in scan["load_stats"].items():
        print(f"  [{d}] {st}")
    print(f"  時間加權區間 {scan['kept_intervals']:,} 段；"
          f"因間距 >{MAX_GAP_SEC:.0f}s 丟棄 {scan['gap_dropped']['n']:,} 段"
          f"（{scan['gap_dropped']['weight_sec']:,.0f}s）")

    def show(key: str, title: str) -> None:
        b = scan["buckets"].get(key)
        if not b or not b["qty"]:
            return
        q1 = b["qty"]["1"]
        print(f"  {title:<22} w={b['weight_sec']:>8,.0f}s  spread p50={b['spread'].get('p50',float('nan')):.1f} "
              f"mean={b['spread'].get('mean',float('nan')):.2f}  "
              f"| qty1 slip mean={q1['mean']:.3f} p90={q1['p90']:.2f} p99={q1['p99']:.2f} max={q1['max']:.2f}  "
              f"| depth5 p50={b['min_side_5lvl_depth'].get('p50',float('nan')):.0f}")

    print("\n-- 全體／session --")
    for k, t in (("all", "ALL"), ("sess:day", "日盤"), ("sess:night", "夜盤")):
        show(k, t)
    print("\n-- 逐日（叢集單位）--")
    for d in days:
        show(f"day:{d}", d)
    print("\n-- 逐日 × session（真正的叢集單位）--")
    for d in days:
        for sess in ("day", "night"):
            show(f"sessday:{sess}:{d}", f"{d} {sess}")
    print("\n-- 命名時段 --")
    for name, *_ in PHASES:
        show(f"phase:{name}", name)
    print("\n-- 逐小時（session:hour）--")
    for k in sorted(scan["buckets"]):
        if k.startswith("sesshour:"):
            show(k, k.replace("sesshour:", ""))

    print("\n-- 深度曲線（qty → 滑價／五檔吃不滿比例）--")
    for k, t in (("sess:day", "日盤"), ("sess:night", "夜盤")):
        b = scan["buckets"].get(k)
        if not b:
            continue
        d5 = b["min_side_5lvl_depth"]
        print(f"  [{t}] 五檔單邊總深度（取較淺那側）p50={d5.get('p50',0):.0f} "
              f"p90={d5.get('p90',0):.0f} mean={d5.get('mean',0):.1f} 口")
        for q in QTYS:
            s = b["qty"].get(str(q))
            if not s:
                continue
            print(f"      qty={q:<3} mean={s['mean']:.3f}  p50={s['p50']:.2f}  p90={s['p90']:.2f}  "
                  f"p99={s['p99']:.2f}  max={s['max']:.2f}  吃不滿={s['shallow_pct_timeweighted']:.2f}%")

    tail = worst_moment_concentration(days, qty=1, thresholds=(2.5, 3.0, 4.0))
    print("\n-- 尾部集中在哪（qty=1 滑價門檻）--")
    for th, v in tail.items():
        print(f"  slip {th}: 佔全部時間 {v['pct_of_all_time']:.2f}%；尾部時間分布 "
              f"{v['top_buckets_pct_of_tail']}")

    day_slip = scan["buckets"].get("sess:day", {}).get("qty", {}).get("1", {}).get("mean", float("nan"))
    night_slip = scan["buckets"].get("sess:night", {}).get("qty", {}).get("1", {}).get("mean", float("nan"))
    all_slip = scan["buckets"].get("all", {}).get("qty", {}).get("1", {}).get("mean", float("nan"))

    unfiltered = None
    if args.compare_unfiltered:
        uf = scan_books(days, keep_zombies=True)
        unfiltered = {k: uf["buckets"].get(k, {}).get("qty", {}).get("1", {})
                      for k in ("all", "sess:day", "sess:night")}
        print("\n-- 殭屍過濾的影響（qty=1 平均滑價）--")
        for k in ("all", "sess:day", "sess:night"):
            a = unfiltered[k].get("mean")
            b = scan["buckets"].get(k, {}).get("qty", {}).get("1", {}).get("mean")
            if a is not None and b is not None:
                print(f"  {k:<12} 未過濾 {a:.4f}  →  過濾後 {b:.4f}  （差 {b - a:+.4f}）")

    print("\n" + "=" * 84)
    print("D — breakeven 敏感度（毛額 × 市價出場比例 × 商品）")
    grid = breakeven_grid(day_slip, night_slip, all_slip)
    print("  TMF＝實測；MXF/TXF＝外推（標 *）")
    for root in ("TMF", "MXF", "TXF"):
        if root == "TMF":
            scen = [("measured_all", "measured_15twd")]
        else:
            scen = [(f"extrap_spread_{EXTRAP_BASE_SPREAD:g}pt", f"extrap_fee_{EXTRAP_BASE_FEE:g}twd")]
        for sl, fe in scen:
            star = "" if root == "TMF" else " *"
            print(f"\n  【{root}{star}】{sl} / {fe}")
            print("      gross\\share   " + "".join(f"{s:>10.0%}" for s in SHARE_GRID))
            for g in GROSS_GRID:
                line = f"      {g:>10.2f}   "
                for s in SHARE_GRID:
                    c = next(c for c in grid["cells"] if c["root"] == root and c["slip_scenario"] == sl
                             and c["fee_scenario"] == fe and c["gross_pts"] == g and c["market_exit_share"] == s)
                    mark = "+" if c["positive"] else " "
                    line += f"{c['net_pts_per_trade']:>9.2f}{mark}"
                print(line)
    print("\n  -- MXF 外推敏感度：net pts/trade（gross=2.86, share=78%）--")
    print("      spread\\fee " + "".join(f"{f:>10.0f}元" for f in EXTRAP_FEE_GRID))
    for s in EXTRAP_SPREAD_GRID:
        line = f"      {s:>9.1f}點 "
        for f in EXTRAP_FEE_GRID:
            c = next(c for c in grid["cells"] if c["root"] == "MXF"
                     and c["slip_scenario"] == f"extrap_spread_{s:g}pt"
                     and c["fee_scenario"] == f"extrap_fee_{f:g}twd"
                     and c["gross_pts"] == 2.86 and c["market_exit_share"] == 0.78)
            line += f"{c['net_pts_per_trade']:>10.2f}{'+' if c['positive'] else ' '}"
        print(line)
    print("\n  -- 可容忍價差上限（實測價差低於此值才為正）--")
    for t in grid["max_tolerable_spread"]:
        if t["gross_pts"] == 2.86 and t["market_exit_share"] == 0.78:
            print(f"      {t['root']} fee={t['fee_ntd_per_side']:.0f}元/邊 → 價差 < "
                  f"{t['max_tolerable_spread_pts']:.2f} 點")

    limit_sim = None
    if not args.skip_limit_sim:
        print("\n" + "=" * 84)
        print(f"E — 限價出場 vs 市價出場（queue-aware tick 重放，stride={args.stride}s）")
        limit_sim = simulate_limit_exit(days, horizons, args.stride)
        print("  gain > 0 ＝ 限價出場比「立刻市價」好幾點（含掛不到而 T 秒後轉市價的代價）")
        for k, v in limit_sim["overall"].items():
            if not v:
                continue
            print(f"    {k:<14} n={v['n_samples']:>6,}  成交率={v['fill_rate']:.3f}  "
                  f"淨益={v['mean_gain_vs_market_now_pts']:+.3f} 點  "
                  f"(成交時 {v['mean_gain_when_filled_pts']:+.3f}／沒成交時 "
                  f"{v['mean_gain_when_unfilled_pts']:+.3f})  "
                  f"[賣邊 {v['mean_gain_sell_pts']:+.3f}／買邊 {v['mean_gain_buy_pts']:+.3f}]")
        print("\n  -- 壓力情境：只取『前 60 秒已往不利方向跑 ≥ k 點』的時刻 --")
        for k in ("day:T60", "night:T60"):
            st = limit_sim["stress"].get(k) or {}
            for cond, v in st.items():
                print(f"    {k:<11} {cond:<24} n={v['n_sell']:>5,}/{v['n_buy']:<5,} "
                      f"成交率={v['fill_rate']:.3f}  淨益={v['mean_gain_pts']:+.3f} 點 "
                      f"[賣 {v['mean_gain_sell_pts']:+.3f}／買 {v['mean_gain_buy_pts']:+.3f}]")
        print("\n  -- 逐日（叢集單位）T=60 --")
        for d, dv in limit_sim["per_day"].items():
            for sess in ("day", "night"):
                v = dv.get(f"{sess}:T60")
                if v:
                    print(f"    {d} {sess:<6} n={v['n_samples']:>5,} 成交率={v['fill_rate']:.3f} "
                          f"淨益={v['mean_gain_vs_market_now_pts']:+.3f}")

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT_DIR / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "wall-b2-exit-cost-curve-v1",
        "generated_at": datetime.now(TZ).isoformat(),
        "days": days,
        "config": {
            "qtys": list(QTYS), "max_gap_sec": MAX_GAP_SEC, "max_book_age_sec": MAX_BOOK_AGE_SEC,
            "strategy_max_lots_from_config_order_yaml": 1,
            "tax_pts_per_side": TAX_PTS_PER_SIDE,
            "limit_slip_pts_per_side": LIMIT_SLIP_PTS_PER_SIDE,
            "gross_grid": list(GROSS_GRID), "share_grid": list(SHARE_GRID),
        },
        "book_scan": scan,
        "tail_concentration": tail,
        "unfiltered_comparison": unfiltered,
        "breakeven": grid,
        "limit_exit_sim": limit_sim,
        "caveats": [
            "只有 5 天資料（2 天完整），日內樣本高度自相關；所有數字以『日』為叢集單位看逐日欄位。",
            "MXF/TXF 沒有任何實測；breakeven 表中 measured=false 的格子全是外推。",
            "限價出場模擬忽略『排在前面的人取消』（低估成交率），也忽略自己的單會被別人插隊。",
            "限價出場的真實代價不只是 T 秒後轉市價：持倉延長會改變策略本身的損益分布，這裡量不到。",
        ],
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n寫出 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
