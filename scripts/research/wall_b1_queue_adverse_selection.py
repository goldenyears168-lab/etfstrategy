#!/usr/bin/env python3
"""B1 — 掛在牆上 vs 掛在牆前一檔：量化被動限價單的負選擇（adverse selection）。

問題
----
把被動限價單掛在厚牆（thick queue / wall）的**同一價位**，等於排在整堵牆後面：
  * 牆撐住、價格反彈 → 該價位成交量不足以吃穿牆 → 沒成交，錯過賺錢的分支
  * 牆被吃穿、價格續行 → 成交了，然後立刻套牢
即「只在輸的分支成交」。掛在**牆前一檔**（往 mid 方向 1 tick，付 1 點）則排在
幾乎空的隊伍前面，牆在下方當保護。誰贏是實證問題。

方法
----
1. 五檔簿（books）先做殭屍過濾（stale 欄位優先，舊資料用 ts-book_time>5s）。
2. 牆事件偵測：對每個 side、每個 tier j，用**因果的** EWMA 基線
   ``base[side][j]``（只用該筆之前的資料更新），要求
     size_j >= WALL_MULT * base[side][j]        （相對於「該檔平時多厚」，
                                                  消除「越遠檔越厚」的常態斜率）
     size_j >= WALL_MULT_NEIGH * median(其他 4 檔)（相對於當下鄰居，抓單點異常）
     size_j >= MIN_LOTS
3. episode 去重：同一 side 兩個 episode 至少間隔 EPISODE_SPACING 秒（預設 =
   訂單存活期），避免同一堵牆被重複計數、也避免 markout 視窗重疊。
4. 對每個 episode 模擬三張假想單（各 1 口，存活 HORIZON 秒）：
     A  wall_price       queue_ahead = 牆的全部可見量（排在牆後面）
     B  wall_price ± 1   queue_ahead = 該檔既有量（往 mid 方向 1 tick，貴 1 點）
     C  wall_price       queue_ahead = 0（隊頭；只是上界參考，不可交易）
   FIFO 消耗用 tmf_trades 逐筆重建：
     - 只有反向 aggressor（Lee-Ready 用該筆自帶的 bid/ask 分類）才消耗我們這側
     - 價格穿過我們的價位（trade-through）視為必定成交
   取消（cancel）模型兩種都跑：
     back   取消一律來自隊尾（隊前不減）→ 對 A 最不利，保守
     prop   取消按比例來自隊前（觀察到的該檔量下降扣掉成交量的部分）→ 對 A 最有利
5. markout：成交後 30/60/300 秒的 mid 相對於成交價（買方 = mid_future - fill）。
   同時報 ``adverse_move`` = mid(t_fill+N) - mid(t_fill)（買方符號調整後），
   那是不含半價差的純負選擇分量。
6. 期望值 EV = P(fill) * E[markout | fill]（每個 episode 未成交記 0）。
7. 不確定性：per-day 分解 + 以「一小時 tape」為區塊的 block bootstrap
   （日內高度自相關，不能把每筆快照當獨立觀察）。

限制（**不要忽略**）
--------------------
牆只出現在離 mid 6 點內，而策略實際掛單在 12–33 點（新方向 24–66 點）。
本研究量到的東西**不能直接套到現行掛單距離**，只有在「掛單距離縮到簿子看得
見的範圍」或「觸價前最後 1 tick 讓價」的情境下才有參考價值。
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import statistics as st
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import orjson as _fastjson

    def _loads(s: bytes) -> Any:
        return _fastjson.loads(s)
except Exception:  # noqa: BLE001
    def _loads(s: bytes) -> Any:
        return json.loads(s)

TZ = timezone(timedelta(hours=8))
MAX_AGE_SEC = 5.0
DAYS = ["2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17", "2026-08-18", "2026-08-19"]
POINT_VALUE_TWD = 10.0
TMF_COST_LINE_PTS = 4.05  # 已知的 TMF 每筆成本線（來回，含滑價與手續費）


def cache_dir(name: str) -> Path:
    try:
        import stock_db  # noqa: PLC0415

        return Path(stock_db.DATA_DIR).parent / "cache" / name
    except Exception:  # noqa: BLE001
        return Path.home() / "goldenstocks-data" / "cache" / name


def _session_of(t: datetime) -> str:
    hm = t.hour * 60 + t.minute
    return "day" if 8 * 60 + 45 <= hm < 13 * 60 + 45 else "night"


# --------------------------------------------------------------------------- books
def load_books(days: list[str]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """回傳按 book_time 排序的 live 五檔快照（殭屍已濾）。"""
    stats: Counter[str] = Counter()
    out: list[dict[str, Any]] = []
    for day in days:
        path = cache_dir("tmf_books") / f"tmf_books_{day}.jsonl"
        if not path.exists():
            continue
        with path.open("rb") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                stats["rows"] += 1
                try:
                    r = _loads(raw)
                except Exception:  # noqa: BLE001
                    stats["bad_json"] += 1
                    continue
                bids, asks = r.get("bids") or [], r.get("asks") or []
                if len(bids) < 5 or len(asks) < 5:
                    stats["short_side"] += 1
                    continue
                try:
                    wall = datetime.fromisoformat(str(r["ts"])).astimezone(TZ)
                    bt = float(r["book_time"]) / 1e6
                except (KeyError, TypeError, ValueError):
                    stats["bad_ts"] += 1
                    continue
                if "stale" in r:
                    is_stale = bool(r["stale"])
                else:
                    is_stale = (wall.timestamp() - bt) > MAX_AGE_SEC
                if is_stale:
                    stats["stale_zombie"] += 1
                    continue
                bp = [int(b["price"]) for b in bids]
                bs = [int(b["size"]) for b in bids]
                ap = [int(a["price"]) for a in asks]
                asz = [int(a["size"]) for a in asks]
                if bp[0] >= ap[0]:
                    stats["crossed"] += 1
                    continue
                stats["live"] += 1
                bdt = datetime.fromtimestamp(bt, tz=TZ)
                out.append(
                    {
                        "t": bt,
                        "date": bdt.strftime("%Y-%m-%d"),
                        "hour": bdt.strftime("%Y-%m-%d %H"),
                        "sess": _session_of(bdt),
                        "bp": bp, "bs": bs, "ap": ap, "as": asz,
                        "mid": (bp[0] + ap[0]) / 2.0,
                    }
                )
    out.sort(key=lambda r: r["t"])
    return out, dict(stats)


# -------------------------------------------------------------------------- trades
def load_trades(days: list[str]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    stats: Counter[str] = Counter()
    out: list[dict[str, Any]] = []
    prev_px: float | None = None
    for day in days:
        path = cache_dir("tmf_trades") / f"tmf_trades_{day}.jsonl"
        if not path.exists():
            continue
        with path.open("rb") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                stats["rows"] += 1
                try:
                    r = _loads(raw)
                except Exception:  # noqa: BLE001
                    stats["bad_json"] += 1
                    continue
                try:
                    wall = datetime.fromisoformat(str(r["ts"])).astimezone(TZ)
                    tt = float(r["trade_time"]) / 1e6
                    px = int(r["price"]); sz = int(r["size"])
                except (KeyError, TypeError, ValueError):
                    stats["bad_ts"] += 1
                    continue
                if (wall.timestamp() - tt) > MAX_AGE_SEC:
                    stats["stale_zombie"] += 1
                    continue
                stats["live"] += 1
                bid = r.get("bid"); ask = r.get("ask")
                side = 0  # +1 buy-initiated, -1 sell-initiated, 0 unknown
                if isinstance(ask, (int, float)) and px >= ask:
                    side = 1
                elif isinstance(bid, (int, float)) and px <= bid:
                    side = -1
                elif prev_px is not None:
                    side = 1 if px > prev_px else (-1 if px < prev_px else 0)
                prev_px = px
                out.append({"t": tt, "px": px, "sz": sz, "side": side})
    out.sort(key=lambda r: r["t"])
    return out, dict(stats)


# ---------------------------------------------------------------- wall detection
def detect_walls(books, *, mult_base: float, mult_neigh: float, min_lots: int,
                 halflife: float, warmup: int, spacing: float, max_dist: float,
                 mode: str = "wall", seed: int = 11):
    """因果 EWMA 基線 → 事件 → episode 去重。

    mode="wall"   ：size_j 明顯厚於自己的 EWMA 基線且厚於當下鄰居 → 真的牆
    mode="normal" ：size_j 貼近自己的基線、也貼近鄰居 → placebo 對照組
                    （用來回答「牆這個標籤本身有沒有加值」，而不是「排在大隊伍
                    後面會被負選擇」這個泛用事實）
    """
    alpha = 1.0 - math.exp(math.log(0.5) / halflife)
    rng = random.Random(seed)
    base = {"bid": [0.0] * 5, "ask": [0.0] * 5}
    seen = {"bid": 0, "ask": 0}
    last_start = {"bid": -1e18, "ask": -1e18}
    episodes: list[dict[str, Any]] = []
    tier_hits: Counter[str] = Counter()
    for idx, b in enumerate(books):
        for side, pk, sk in (("bid", "bp", "bs"), ("ask", "ap", "as")):
            sizes = b[sk]
            prices = b[pk]
            bb = base[side]
            n_seen = seen[side]
            if n_seen >= warmup and b["t"] - last_start[side] >= spacing:
                cand = []
                for j in range(5):
                    s = sizes[j]
                    if bb[j] <= 0:
                        continue
                    ratio = s / bb[j]
                    others = sorted(sizes[:j] + sizes[j + 1:])
                    med_other = (others[1] + others[2]) / 2.0
                    rel = (s / med_other) if med_other > 0 else float("inf")
                    if mode == "wall":
                        ok = s >= min_lots and ratio >= mult_base and rel >= mult_neigh
                    else:  # normal / placebo
                        ok = s >= 5 and 0.7 <= ratio <= 1.3 and 0.6 <= rel <= 1.6
                    if not ok:
                        continue
                    if abs(prices[j] - b["mid"]) > max_dist:
                        continue
                    cand.append(j)
                if cand:
                    j = cand[0] if mode == "wall" else rng.choice(cand)
                    s = sizes[j]
                    dist = abs(prices[j] - b["mid"])
                    episodes.append(
                        {
                            "i": idx, "t": b["t"], "date": b["date"], "hour": b["hour"],
                            "sess": b["sess"], "side": side, "tier": j + 1,
                            "price": prices[j], "wall_size": s, "mid": b["mid"],
                            "dist_pts": dist,
                            "base": round(bb[j], 2),
                            "ratio_base": round(s / bb[j], 2),
                            "spread": b["ap"][0] - b["bp"][0],
                        }
                    )
                    tier_hits[f"{side}_t{j+1}"] += 1
                    last_start[side] = b["t"]
            for j in range(5):
                bb[j] = sizes[j] if n_seen == 0 else bb[j] + alpha * (sizes[j] - bb[j])
            seen[side] = n_seen + 1
    return episodes, dict(tier_hits)


# ------------------------------------------------------------------ simulation
def simulate(ep, books, trades, bk_t, tr_t, *, horizon: float, cancel_model: str,
             through_time: str = "late"):
    """回傳三張假想單的模擬結果。side='bid' → 我們掛買單。"""
    side = ep["side"]
    is_buy = side == "bid"
    sign = 1.0 if is_buy else -1.0
    t0, t1 = ep["t"], ep["t"] + horizon
    wall_px = ep["price"]
    inner_px = wall_px + 1 if is_buy else wall_px - 1
    b0 = books[ep["i"]]

    # B 的價位若會越過對手價（marketable）就不算數
    if is_buy and inner_px >= b0["ap"][0]:
        return None
    if (not is_buy) and inner_px <= b0["bp"][0]:
        return None

    pk, sk = ("bp", "bs") if is_buy else ("ap", "as")
    inner_q0 = 0
    for j in range(5):
        if b0[pk][j] == inner_px:
            inner_q0 = b0[sk][j]
            break

    orders = {
        "A_on_wall_behind": {"px": wall_px, "q": float(ep["wall_size"])},
        "B_one_tick_inside": {"px": inner_px, "q": float(inner_q0)},
        "C_on_wall_front": {"px": wall_px, "q": 0.0},
    }
    for o in orders.values():
        o["q0"] = o["q"]
        o["cum"] = 0.0
        o["fill_t"] = None
        o["through"] = False
        o["last_px_t"] = None
        o["front"] = o["q"] <= 0

    lo = bisect.bisect_left(tr_t, t0)
    hi = bisect.bisect_right(tr_t, t1)
    consume_side = -1 if is_buy else 1  # 買單被賣方 aggressor 消耗

    # 觀察該價位可見量的變化（prop 取消模型用）
    bk_lo = ep["i"]
    bk_hi = bisect.bisect_right(bk_t, t1)
    obs_iter = {k: bk_lo for k in orders}
    prev_obs = {k: None for k in orders}

    wall_consumed = 0.0
    wall_broken_t = None

    def _visible(bi: int, px: int) -> float:
        bb = books[bi]
        for j in range(5):
            if bb[pk][j] == px:
                return float(bb[sk][j])
        # 不在五檔內：若價位已被穿過（買單而言 px > best bid）視為 0
        return 0.0

    ti = lo
    bi = bk_lo
    while ti < hi:
        tr = trades[ti]
        tt = tr["t"]
        # 先把 book 推進到 tt，處理 prop 取消
        if cancel_model == "prop":
            while bi + 1 < bk_hi and bk_t[bi + 1] <= tt:
                bi += 1
                for k, o in orders.items():
                    if o["fill_t"] is not None or o["q"] <= 0:
                        continue
                    v = _visible(bi, o["px"])
                    p = prev_obs[k]
                    if p is not None:
                        drop = p - v
                        # drop 已扣掉這段期間的成交（cum 增量在下面處理），此處
                        # 以「觀察量下降」近似總減少，取消量 = drop - traded_delta
                        cancel = drop - o.pop("_traded_delta", 0.0)
                        if cancel > 0:
                            frac = min(1.0, o["q"] / max(p, 1e-9))
                            o["q"] = max(0.0, o["q"] - cancel * frac)
                    prev_obs[k] = v
                    o["_traded_delta"] = 0.0
        # 成交消耗
        px = tr["px"]
        for k, o in orders.items():
            if o["fill_t"] is not None:
                continue
            opx = o["px"]
            through = (px < opx) if is_buy else (px > opx)
            if through:
                # 價格穿過我們的價位 → 該價位必然被清空，我們一定成交在 opx。
                # 成交「時點」有歧義：最晚是這筆穿價成交的時間（mid 已經走掉，
                # 對我們最不利）、最早是該價位上最後一筆成交的時間。兩種都跑。
                if through_time == "early" and o["last_px_t"] is not None:
                    o["fill_t"] = o["last_px_t"]
                else:
                    o["fill_t"] = tt
                o["through"] = True
                o["front"] = True
                continue
            if px == opx:
                o["last_px_t"] = tt
            if px == opx and tr["side"] == consume_side:
                o["cum"] += tr["sz"]
                o["_traded_delta"] = o.get("_traded_delta", 0.0) + tr["sz"]
                if o["cum"] >= o["q"]:  # 已消耗到隊頭
                    o["front"] = True
                if o["cum"] >= o["q"] + 1.0:
                    o["fill_t"] = tt
        # 牆本身是否被吃穿
        if wall_broken_t is None:
            if (px < wall_px) if is_buy else (px > wall_px):
                wall_broken_t = tt
            elif px == wall_px and tr["side"] == consume_side:
                wall_consumed += tr["sz"]
                if wall_consumed >= ep["wall_size"]:
                    wall_broken_t = tt
        ti += 1

    res = {
        "wall_broken": wall_broken_t is not None,
        "wall_consumed": wall_consumed,
        "wall_consume_frac": min(2.0, wall_consumed / max(1.0, ep["wall_size"])),
        "orders": {},
    }
    for k, o in orders.items():
        res["orders"][k] = {
            "price": o["px"], "q0": o["q0"], "filled": o["fill_t"] is not None,
            "fill_t": o["fill_t"], "through": o["through"],
            "reached_front": bool(o["front"]),
            "ttf": (o["fill_t"] - t0) if o["fill_t"] is not None else None,
            "sign": sign,
        }
    return res


def markout(books, bk_t, t_fill: float, horizons, tol: float = 15.0):
    out = {}
    i0 = bisect.bisect_right(bk_t, t_fill) - 1
    mid0 = books[i0]["mid"] if i0 >= 0 else None
    for n in horizons:
        tgt = t_fill + n
        i = bisect.bisect_right(bk_t, tgt) - 1
        if i < 0 or tgt - bk_t[i] > tol:
            out[n] = None
        else:
            out[n] = books[i]["mid"]
    return mid0, out


# ------------------------------------------------------------------ aggregation
def _mean(xs):
    return float(st.mean(xs)) if xs else None


def _agg(rows, key, horizons):
    """rows: list of per-episode dict for one order type."""
    n = len(rows)
    if n == 0:
        return {"n": 0}
    filled = [r for r in rows if r["filled"]]
    out = {
        "n_episodes": n,
        "n_filled": len(filled),
        "fill_rate": len(filled) / n,
        "reached_front_rate": sum(1 for r in rows if r["reached_front"]) / n,
        "through_rate": sum(1 for r in rows if r["through"]) / n,
        "median_q0": float(st.median([r["q0"] for r in rows])),
        "median_ttf_sec": float(st.median([r["ttf"] for r in filled])) if filled else None,
    }
    for h in horizons:
        mk = [r["markout"][h] for r in filled if r["markout"].get(h) is not None]
        adv = [r["adverse"][h] for r in filled if r["adverse"].get(h) is not None]
        al = [r["aligned"][h] for r in rows if r.get("aligned", {}).get(h) is not None]
        out[f"aligned_ev_{h}s_pts_per_episode"] = _mean(al)
        out[f"aligned_ev_{h}s_n"] = len(al)
        out[f"markout_{h}s_mean_pts"] = _mean(mk)
        out[f"markout_{h}s_median_pts"] = float(st.median(mk)) if mk else None
        out[f"markout_{h}s_n"] = len(mk)
        out[f"adverse_{h}s_mean_pts"] = _mean(adv)
        # EV：未成交 = 0
        out[f"ev_{h}s_pts_per_episode"] = (out["fill_rate"] * _mean(mk)) if mk else None
    return out


def block_bootstrap_diff(per_ep, horizon_key, reps=4000, seed=7):
    """以「一小時 tape」為區塊，bootstrap EV(B) - EV(A)。"""
    blocks = defaultdict(list)
    for e in per_ep:
        blocks[e["hour"]].append(e)
    keys = list(blocks)
    if len(keys) < 3:
        return None
    rng = random.Random(seed)

    def ev(sample, name):
        vals = []
        for e in sample:
            o = e["orders"][name]
            if not o["filled"]:
                vals.append(0.0)
            else:
                m = o["markout"].get(horizon_key)
                if m is None:
                    continue
                vals.append(m)
        return _mean(vals)

    diffs = []
    for _ in range(reps):
        pick = [k for k in (rng.choice(keys) for _ in keys)]
        sample = [e for k in pick for e in blocks[k]]
        a = ev(sample, "A_on_wall_behind")
        b = ev(sample, "B_one_tick_inside")
        if a is None or b is None:
            continue
        diffs.append(b - a)
    diffs.sort()
    if not diffs:
        return None
    return {
        "n_blocks": len(keys),
        "reps": len(diffs),
        "mean": _mean(diffs),
        "p05": diffs[int(0.05 * (len(diffs) - 1))],
        "p50": diffs[int(0.50 * (len(diffs) - 1))],
        "p95": diffs[int(0.95 * (len(diffs) - 1))],
        "frac_positive": sum(1 for d in diffs if d > 0) / len(diffs),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", nargs="*", default=DAYS)
    ap.add_argument("--horizon", type=float, default=120.0, help="假想單存活秒數")
    ap.add_argument("--spacing", type=float, default=120.0, help="同側 episode 最小間隔秒")
    ap.add_argument("--wall-mult-base", type=float, default=3.0)
    ap.add_argument("--wall-mult-neigh", type=float, default=2.0)
    ap.add_argument("--min-lots", type=int, default=15)
    ap.add_argument("--halflife", type=float, default=2000.0)
    ap.add_argument("--warmup", type=int, default=5000)
    ap.add_argument("--max-dist", type=float, default=12.0)
    ap.add_argument("--cancel-model", default="both", choices=["back", "prop", "both"])
    ap.add_argument("--through-modes", nargs="*", default=["late", "early"])
    ap.add_argument("--bootstrap-reps", type=int, default=4000)
    ap.add_argument("--out", default="reports/research/channel_lab/wall_b1_queue_adverse_selection.json")
    args = ap.parse_args()

    horizons = [30, 60, 300]

    books, bstats = load_books(args.days)
    trades, tstats = load_trades(args.days)
    print(f"books live={len(books)} {bstats}")
    print(f"trades live={len(trades)} {tstats}")
    if not books or not trades:
        print("no data")
        return 1
    bk_t = [b["t"] for b in books]
    tr_t = [t["t"] for t in trades]

    # 只保留有 trades 覆蓋的時間範圍（08-14/15 沒有 trades 檔）
    tr_lo, tr_hi = tr_t[0], tr_t[-1]

    episodes, tier_hits = detect_walls(
        books, mult_base=args.wall_mult_base, mult_neigh=args.wall_mult_neigh,
        min_lots=args.min_lots, halflife=args.halflife, warmup=args.warmup,
        spacing=args.spacing, max_dist=args.max_dist,
    )
    n_all = len(episodes)
    episodes = [e for e in episodes if tr_lo <= e["t"] <= tr_hi - args.horizon]
    print(f"wall episodes: {n_all} detected, {len(episodes)} inside trade coverage")
    print("tier distribution:", tier_hits)

    models = ["back", "prop"] if args.cancel_model == "both" else [args.cancel_model]
    through_modes = list(args.through_modes)
    report: dict[str, Any] = {
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "params": vars(args),
        "data": {
            "books": bstats, "trades": tstats,
            "trade_coverage": [
                datetime.fromtimestamp(tr_lo, tz=TZ).isoformat(timespec="seconds"),
                datetime.fromtimestamp(tr_hi, tz=TZ).isoformat(timespec="seconds"),
            ],
            "days_with_trades": sorted({
                datetime.fromtimestamp(t["t"], tz=TZ).strftime("%Y-%m-%d") for t in trades
            }),
        },
        "wall_events": {
            "n_detected": n_all,
            "n_used": len(episodes),
            "tier_distribution": tier_hits,
            "dist_from_mid_pts": {},
            "wall_size_lots": {},
            "by_day": dict(Counter(e["date"] for e in episodes)),
            "by_session": dict(Counter(e["sess"] for e in episodes)),
            "by_side": dict(Counter(e["side"] for e in episodes)),
        },
        "results": {},
    }
    if episodes:
        d = sorted(e["dist_pts"] for e in episodes)
        w = sorted(e["wall_size"] for e in episodes)
        q = lambda xs, p: xs[min(len(xs) - 1, int(p * (len(xs) - 1)))]  # noqa: E731
        report["wall_events"]["dist_from_mid_pts"] = {
            "p10": q(d, .1), "p50": q(d, .5), "p90": q(d, .9), "max": d[-1]}
        report["wall_events"]["wall_size_lots"] = {
            "p10": q(w, .1), "p50": q(w, .5), "p90": q(w, .9), "max": w[-1]}

    def run_config(episodes, model, tmode, *, full: bool):
        per_ep = []
        skipped = 0
        for ep in episodes:
            sim = simulate(ep, books, trades, bk_t, tr_t,
                           horizon=args.horizon, cancel_model=model,
                           through_time=tmode)
            if sim is None:
                skipped += 1
                continue
            rec = {k: ep[k] for k in ("date", "hour", "sess", "side", "tier",
                                      "price", "wall_size", "dist_pts", "spread")}
            rec["wall_broken"] = sim["wall_broken"]
            rec["wall_consume_frac"] = sim["wall_consume_frac"]
            rec["orders"] = {}
            for name, o in sim["orders"].items():
                rr = dict(o)
                rr["markout"] = {}
                rr["adverse"] = {}
                # episode-time-aligned P&L：從 episode 起點 t0 起算固定 N 秒後結算，
                # 不論何時成交（未成交 = 0）。這樣 A / B / C 在同一個 wall-clock
                # 時點被評價，排除「B 較早成交所以看到不同的漂移」這個混淆。
                rr["aligned"] = {}
                for h in horizons:
                    tgt = ep["t"] + h
                    i = bisect.bisect_right(bk_t, tgt) - 1
                    if i < 0 or tgt - bk_t[i] > 15.0:
                        rr["aligned"][h] = None
                    elif not o["filled"] or o["fill_t"] > tgt:
                        rr["aligned"][h] = 0.0
                    else:
                        rr["aligned"][h] = o["sign"] * (books[i]["mid"] - o["price"])
                if o["filled"]:
                    mid0, mids = markout(books, bk_t, o["fill_t"], horizons)
                    for h in horizons:
                        m = mids[h]
                        if m is None:
                            rr["markout"][h] = None
                            rr["adverse"][h] = None
                        else:
                            rr["markout"][h] = o["sign"] * (m - o["price"])
                            rr["adverse"][h] = (o["sign"] * (m - mid0)) if mid0 else None
                rec["orders"][name] = rr
            per_ep.append(rec)

        names = ["A_on_wall_behind", "B_one_tick_inside", "C_on_wall_front"]
        res: dict[str, Any] = {"n_episodes": len(per_ep), "skipped_marketable": skipped,
                               "overall": {}, "by_wall_outcome": {}, "by_session": {},
                               "by_day": {}, "by_dist_bucket": {}}
        for name in names:
            res["overall"][name] = _agg([e["orders"][name] for e in per_ep], name, horizons)
        for broke in (True, False):
            sub = [e for e in per_ep if e["wall_broken"] is broke]
            key = "wall_broken" if broke else "wall_held"
            res["by_wall_outcome"][key] = {
                "n": len(sub),
                **{name: _agg([e["orders"][name] for e in sub], name, horizons) for name in names},
            }
        for sess in ("day", "night"):
            sub = [e for e in per_ep if e["sess"] == sess]
            res["by_session"][sess] = {
                "n": len(sub),
                **{name: _agg([e["orders"][name] for e in sub], name, horizons) for name in names},
            }
        for day in sorted({e["date"] for e in per_ep}):
            sub = [e for e in per_ep if e["date"] == day]
            res["by_day"][day] = {
                "n": len(sub),
                **{name: _agg([e["orders"][name] for e in sub], name, horizons) for name in names},
            }
        for lo, hi in ((0, 2), (2, 4), (4, 6), (6, 12)):
            sub = [e for e in per_ep if lo <= e["dist_pts"] < hi]
            res["by_dist_bucket"][f"{lo}-{hi}pt"] = {
                "n": len(sub),
                **{name: _agg([e["orders"][name] for e in sub], name, horizons) for name in names},
            }
        if full:
            res["bootstrap_ev_diff_B_minus_A"] = {
                str(h): block_bootstrap_diff(per_ep, h, reps=args.bootstrap_reps)
                for h in horizons
            }
        # 條件成交機率（負選擇的核心證據）
        cond = {}
        for name in names:
            nb = [e for e in per_ep if e["wall_broken"]]
            nh = [e for e in per_ep if not e["wall_broken"]]
            cond[name] = {
                "P(fill|wall_broken)": (sum(1 for e in nb if e["orders"][name]["filled"]) / len(nb)) if nb else None,
                "P(fill|wall_held)": (sum(1 for e in nh if e["orders"][name]["filled"]) / len(nh)) if nh else None,
            }
        res["conditional_fill"] = cond
        return res

    for model in models:
        for tmode in through_modes:
            tag = f"cancel_{model}__through_{tmode}"
            res = run_config(episodes, model, tmode, full=True)
            report["results"][tag] = res
            print(f"\n=== {tag} ===  episodes={res['n_episodes']} skipped={res['skipped_marketable']}")
            for name in ("A_on_wall_behind", "B_one_tick_inside", "C_on_wall_front"):
                a = res["overall"][name]
                print(f"  {name:<20} fill={a['fill_rate']:.3f} q0med={a['median_q0']:.0f} "
                      f"mk30={a.get('markout_30s_mean_pts'):+.3f} mk60={a.get('markout_60s_mean_pts'):+.3f} "
                      f"mk300={a.get('markout_300s_mean_pts'):+.3f} ev60={a.get('ev_60s_pts_per_episode'):+.3f}")

    # ---- placebo：非牆（size 貼近自己的 EWMA 基線）的同樣三張單 ----
    placebo_eps, placebo_tiers = detect_walls(
        books, mult_base=args.wall_mult_base, mult_neigh=args.wall_mult_neigh,
        min_lots=args.min_lots, halflife=args.halflife, warmup=args.warmup,
        spacing=args.spacing, max_dist=args.max_dist, mode="normal",
    )
    n_pl_all = len(placebo_eps)
    placebo_eps = [e for e in placebo_eps if tr_lo <= e["t"] <= tr_hi - args.horizon]
    report["placebo_normal_levels"] = {
        "n_detected": n_pl_all, "n_used": len(placebo_eps),
        "tier_distribution": placebo_tiers,
        "median_level_size_lots": float(st.median([e["wall_size"] for e in placebo_eps]))
        if placebo_eps else None,
        "results": {},
    }
    for model in models:
        tag = f"cancel_{model}__through_late"
        pres = run_config(placebo_eps, model, "late", full=False)
        report["placebo_normal_levels"]["results"][tag] = {
            "n_episodes": pres["n_episodes"],
            "overall": pres["overall"],
            "conditional_fill": pres["conditional_fill"],
        }
        print(f"\n=== PLACEBO (normal levels) {tag} === episodes={pres['n_episodes']}")
        for name in ("A_on_wall_behind", "B_one_tick_inside", "C_on_wall_front"):
            a = pres["overall"][name]
            print(f"  {name:<20} fill={a['fill_rate']:.3f} q0med={a['median_q0']:.0f} "
                  f"mk60={a.get('markout_60s_mean_pts'):+.3f} ev60={a.get('ev_60s_pts_per_episode'):+.3f}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
