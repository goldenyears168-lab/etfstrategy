#!/usr/bin/env python3
"""B3 — 「觸價前最後一刻看牆再決定」在工程上追得上嗎？

問題：策略掛單距離 12–33 點（新方向 ×2 = 24–66 點），但五檔簿一側只跨 ~4 點、
離 mid 最遠只看到 6 點。所以掛單價位在絕大多數時間根本不在視野內。唯一能
「看到自己掛單價上的牆」的時機，是價格逼近到該價位進入五檔視野的那一小段時間。
本腳本量測那段時間有多長（reaction window），以及現行 20 秒對帳迴圈
（``ORDER_TMF_CHANNEL_WORKER_INTERVAL`` 預設 20.0）能不能在觸價前撈到那筆資訊。

量測（純唯讀資料分析，不碰 live 程式）：
  R1  reaction window：價位從「進入五檔視野」到「被觸及」的時間分布
  R2  觸價前 20s / 5s / 1s 的那一刻，該價位是否已在視野內
  R3  要讓 X% 的 episode 至少有一次輪詢落在視野窗內，輪詢間隔要壓到幾秒
  R4  真正進入視野時，那個價位上到底有沒有掛量（沒量的話「看牆」是空的）

資料陷阱（一律先套用）：日盤／夜盤 book 交錯寫進同一檔，收盤那側會凍結並隨
session 續約原樣重送。過濾規則：有 ``stale`` 欄位就信它，另外一律再用
``age = ts - book_time > 5s`` 兜底（兩者取聯集）。session 用 ``quote_type``
切（FUTURE=日盤 / FUTURE_AH=夜盤），episode 絕不跨 session、也不跨 >120s 的斷檔。

因果紀律：所有前瞻掃描都只從 anchor 之後的 index 開始；t_touch 找到後只往
「過去」回看，不使用觸價之後的任何資料。

輸出：reports/research/channel_lab/wall_b3_reaction_window.json
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

TZ = timezone(timedelta(hours=8))
MAX_BOOK_AGE_SEC = 5.0
DAYS = ["2026-08-14", "2026-08-15", "2026-08-17", "2026-08-18", "2026-08-19"]
DISTANCES = [12, 20, 24, 30, 33, 66]
VIS_PTS = 6.0            # 已知事實：簿子離 mid 最遠只看到 6.0 點 (p50)
ANCHOR_STEP_SEC = 30.0   # anchor 網格（越密 episode 越重疊，統計不會更獨立）
HORIZON_SEC = 1800.0     # 掛單後多久內沒觸價就視為未觸價（censored）
BLOCK_GAP_SEC = 120.0
TAIL_SEC = 900.0        # 只在觸價前這段尾巴算可見性（window p90 << 此值）    # 資料斷檔超過這個秒數就切成不同 block
LOOKBACKS = [1.0, 5.0, 20.0]
POLL_GRID = [0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 15.0, 20.0, 30.0, 60.0]


def books_dir() -> Path:
    try:
        import stock_db

        return Path(stock_db.DATA_DIR).parent / "cache" / "tmf_books"
    except Exception:  # noqa: BLE001
        return Path.home() / "goldenstocks-data" / "cache" / "tmf_books"


def load_live_books(day: str) -> tuple[list[dict[str, Any]], Counter]:
    """回傳非殭屍列（含 session 標籤）。過濾邏輯見模組 docstring。"""
    path = books_dir() / f"tmf_books_{day}.jsonl"
    stats: Counter = Counter()
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out, stats
    for line in path.open(encoding="utf-8"):
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
        if len(bids) < 1 or len(asks) < 1:
            stats["empty_side"] += 1
            continue
        try:
            wall = datetime.fromisoformat(str(r["ts"])).astimezone(TZ)
            bt = float(r["book_time"]) / 1e6
        except (KeyError, TypeError, ValueError):
            stats["bad_ts"] += 1
            continue
        age = wall.timestamp() - bt
        flagged = bool(r.get("stale")) if "stale" in r else False
        if flagged or age > MAX_BOOK_AGE_SEC:
            stats["stale_zombie"] += 1
            continue
        stats["live"] += 1
        out.append(
            {
                "t": bt,
                "sess": "day" if str(r.get("quote_type")) == "FUTURE" else "night",
                "bp": [float(b["price"]) for b in bids],
                "bs": [float(b["size"]) for b in bids],
                "ap": [float(a["price"]) for a in asks],
                "asz": [float(a["size"]) for a in asks],
            }
        )
    out.sort(key=lambda r: (r["sess"], r["t"]))
    return out, stats


class Block:
    """同一 session、無大斷檔的一段連續 book 序列，全部轉成 numpy 陣列。"""

    __slots__ = ("day", "sess", "t", "bb", "ba", "mid", "bmin", "amax", "rows")

    def __init__(self, day: str, sess: str, rows: list[dict[str, Any]]) -> None:
        self.day, self.sess, self.rows = day, sess, rows
        self.t = np.array([r["t"] for r in rows], dtype=float)
        self.bb = np.array([r["bp"][0] for r in rows], dtype=float)   # best bid
        self.ba = np.array([r["ap"][0] for r in rows], dtype=float)   # best ask
        self.bmin = np.array([min(r["bp"]) for r in rows], dtype=float)  # 第5檔買
        self.amax = np.array([max(r["ap"]) for r in rows], dtype=float)  # 第5檔賣
        self.mid = (self.bb + self.ba) / 2.0

    def __len__(self) -> int:
        return len(self.t)


def build_blocks(day: str, rows: list[dict[str, Any]]) -> list[Block]:
    blocks: list[Block] = []
    cur: list[dict[str, Any]] = []
    for r in rows:
        if cur and (r["sess"] != cur[-1]["sess"] or r["t"] - cur[-1]["t"] > BLOCK_GAP_SEC):
            if len(cur) > 50:
                blocks.append(Block(day, cur[0]["sess"], cur))
            cur = []
        cur.append(r)
    if len(cur) > 50:
        blocks.append(Block(day, cur[0]["sess"], cur))
    return blocks


def size_at(row: dict[str, Any], level: float, side: str) -> float:
    px = row["ap"] if side == "up" else row["bp"]
    sz = row["asz"] if side == "up" else row["bs"]
    for p, s in zip(px, sz):
        if abs(p - level) < 0.5:
            return s
    return 0.0


def scan_block(blk: Block) -> list[dict[str, Any]]:
    """對一個 block 產生所有 episode。每個 episode = 一張掛在 L 的限價單的最後接近過程。

    效能：每個 anchor 只做一次 ``maximum.accumulate`` / ``minimum.accumulate``，
    之後每個距離 D 用 binary search 找觸價 index（accumulate 後單調），
    再只在觸價前 TAIL_SEC 的尾段算可見性——比逐 D 全掃快兩個數量級，結果等價。
    """
    eps: list[dict[str, Any]] = []
    n = len(blk)
    if n < 50:
        return eps
    t, mid, bb, ba, amax, bmin = blk.t, blk.mid, blk.bb, blk.ba, blk.amax, blk.bmin
    grid = np.arange(t[0], t[-1], ANCHOR_STEP_SEC)
    anchors = np.unique(np.searchsorted(t, grid))
    anchors = anchors[anchors < n - 5]
    for i in anchors:
        i = int(i)
        j_end = int(np.searchsorted(t, t[i] + HORIZON_SEC))
        if j_end <= i + 2:
            continue
        lo = i + 1
        cm_up = np.maximum.accumulate(bb[lo:j_end])      # 單調不減
        cm_dn = -np.minimum.accumulate(ba[lo:j_end])     # 單調不減
        m = j_end - lo
        for D in DISTANCES:
            for side in ("up", "dn"):
                L = float(round(mid[i] + D) if side == "up" else round(mid[i] - D))
                k = int(np.searchsorted(cm_up if side == "up" else cm_dn,
                                        L if side == "up" else -L, side="left"))
                if k >= m:
                    eps.append({"day": blk.day, "sess": blk.sess, "D": D, "side": side,
                                "touched": False})
                    continue
                jt = lo + k                                # 全域觸價 index
                t_touch = float(t[jt])
                rec: dict[str, Any] = {
                    "day": blk.day, "sess": blk.sess, "D": D, "side": side,
                    "touched": True, "L": L, "t_anchor": float(t[i]),
                    "t_touch": t_touch, "ttl_sec": t_touch - float(t[i]),
                }
                # 尾段：只看觸價前 TAIL_SEC；p90 window 遠小於此，被截斷會標記
                s0 = max(lo, int(np.searchsorted(t, t_touch - TAIL_SEC)))
                if s0 >= jt:
                    s0 = max(lo, jt - 1)
                seg = slice(s0, jt)                        # 全部 t < t_touch
                if side == "up":
                    vb = amax[seg] >= L
                else:
                    vb = bmin[seg] <= L
                vg = np.abs(mid[seg] - L) <= VIS_PTS
                tseg = t[seg]
                for tag, v in (("book", vb), ("geo", vg)):
                    if v.size == 0 or not bool(v[-1]):
                        rec[f"w_{tag}"] = 0.0
                        rec[f"w_{tag}_censored"] = False
                    else:
                        inv = np.nonzero(~v)[0]
                        st_ = int(inv[-1]) + 1 if inv.size else 0
                        rec[f"w_{tag}"] = t_touch - float(tseg[st_])
                        rec[f"w_{tag}_censored"] = bool(inv.size == 0 and s0 > lo)
                    m60 = tseg >= t_touch - 60.0
                    rec[f"vis_frac60_{tag}"] = float(v[m60].mean()) if m60.any() else 0.0
                # R2：觸價前 Δ 秒的那一刻是否已可見（只回看過去）
                for lb in LOOKBACKS:
                    q = int(np.searchsorted(t, t_touch - lb, side="right")) - 1
                    if q < 0 or q >= jt:
                        rec[f"vis{int(lb)}s_book"] = None
                        rec[f"vis{int(lb)}s_geo"] = None
                        continue
                    rec[f"vis{int(lb)}s_book"] = bool(
                        amax[q] >= L if side == "up" else bmin[q] <= L
                    )
                    rec[f"vis{int(lb)}s_geo"] = bool(abs(mid[q] - L) <= VIS_PTS)
                # R4：首次可見那一刻 L 上有沒有掛量
                if vb.size and bool(vb[-1]):
                    inv = np.nonzero(~vb)[0]
                    st_ = int(inv[-1]) + 1 if inv.size else 0
                    rec["size_at_L_first_vis"] = size_at(blk.rows[s0 + st_], L, side)
                    rec["size_at_L_last"] = size_at(blk.rows[jt - 1], L, side)
                eps.append(rec)
    return eps


def pctl(xs: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(xs, dtype=float), q)) if xs else float("nan")


def coverage(ws: list[float], delta: float) -> float:
    """輪詢間隔 delta、相位均勻隨機時，至少有一次輪詢落在視野窗內的機率期望值。"""
    if not ws:
        return float("nan")
    a = np.asarray(ws, dtype=float)
    return float(np.minimum(1.0, a / delta).mean())


def solve_interval(ws: list[float], target: float) -> float | None:
    lo, hi = 1e-3, 600.0
    if coverage(ws, lo) < target:
        return None
    for _ in range(60):
        m = math.sqrt(lo * hi)
        if coverage(ws, m) >= target:
            lo = m
        else:
            hi = m
    return round(lo, 3)


def summarize(eps: list[dict[str, Any]], key: str) -> dict[str, Any]:
    tou = [e for e in eps if e["touched"]]
    if not tou:
        return {"n_episodes": len(eps), "touch_rate": 0.0}
    wb = [e["w_book"] for e in tou]
    wg = [e["w_geo"] for e in tou]
    out: dict[str, Any] = {
        "key": key,
        "n_episodes": len(eps),
        "n_touched": len(tou),
        "touch_rate": round(len(tou) / len(eps), 4),
        "n_days": len({e["day"] for e in tou}),
        "ttl_to_touch_sec": {q: round(pctl([e["ttl_sec"] for e in tou], p), 1)
                             for q, p in (("p10", 10), ("p50", 50), ("p90", 90))},
        "window_book_sec": {q: round(pctl(wb, p), 3) for q, p in
                            (("p10", 10), ("p25", 25), ("p50", 50), ("p75", 75), ("p90", 90))},
        "window_geo_sec": {q: round(pctl(wg, p), 3) for q, p in
                           (("p10", 10), ("p25", 25), ("p50", 50), ("p75", 75), ("p90", 90))},
        "window_book_mean_sec": round(float(np.mean(wb)), 3),
        "zero_window_book_frac": round(float(np.mean([w <= 0 for w in wb])), 4),
        "zero_window_geo_frac": round(float(np.mean([w <= 0 for w in wg])), 4),
        "censored_window_book_frac": round(
            float(np.mean([bool(e.get("w_book_censored")) for e in tou])), 4),
        "vis_frac_last60s_book_mean": round(
            float(np.mean([e["vis_frac60_book"] for e in tou])), 4),
    }
    for lb in LOOKBACKS:
        for tag in ("book", "geo"):
            vals = [e[f"vis{int(lb)}s_{tag}"] for e in tou
                    if e.get(f"vis{int(lb)}s_{tag}") is not None]
            out[f"visible_{int(lb)}s_before_touch_{tag}"] = (
                round(float(np.mean(vals)), 4) if vals else None
            )
            out[f"n_visible_{int(lb)}s_{tag}"] = len(vals)
    out["coverage_by_poll_interval_book"] = {
        str(d): round(coverage(wb, d), 4) for d in POLL_GRID
    }
    out["coverage_by_poll_interval_geo"] = {
        str(d): round(coverage(wg, d), 4) for d in POLL_GRID
    }
    # 可行動窗口：看到牆之後還要留 a 秒去下 modify/cancel 並讓它到交易所，
    # 所以有效窗口 = max(0, W - a)。a=0 是「零延遲」的理論上限。
    out["actionable_coverage"] = {}
    for a in (0.0, 0.2, 0.5, 1.0, 2.0):
        we = [max(0.0, w - a) for w in wb]
        out["actionable_coverage"][f"latency_{a}s"] = {
            "frac_window_gt_latency": round(float(np.mean([w > 0 for w in we])), 4),
            "cov_20s": round(coverage(we, 20.0), 4),
            "cov_5s": round(coverage(we, 5.0), 4),
            "cov_1s": round(coverage(we, 1.0), 4),
            "interval_for_80pct": solve_interval(we, 0.8),
        }
    out["interval_for_coverage"] = {
        f"{int(tgt*100)}pct_book": solve_interval(wb, tgt) for tgt in (0.5, 0.8, 0.9)
    }
    out["interval_for_coverage"].update(
        {f"{int(tgt*100)}pct_geo": solve_interval(wg, tgt) for tgt in (0.5, 0.8, 0.9)}
    )
    sizes = [e["size_at_L_first_vis"] for e in tou if "size_at_L_first_vis" in e]
    if sizes:
        out["size_at_L_first_visible"] = {
            "n": len(sizes),
            "zero_frac": round(float(np.mean([s <= 0 for s in sizes])), 4),
            "p50": round(pctl(sizes, 50), 1),
            "p90": round(pctl(sizes, 90), 1),
        }
    # per-day（叢集單位＝日：檢查結論是不是被單一天帶動）
    byday: dict[str, list[float]] = defaultdict(list)
    for e in tou:
        byday[e["day"]].append(e["w_book"])
    out["per_day_window_book_p50"] = {d: round(pctl(v, 50), 3) for d, v in sorted(byday.items())}
    out["per_day_n"] = {d: len(v) for d, v in sorted(byday.items())}
    byday_vis: dict[str, list[bool]] = defaultdict(list)
    for e in tou:
        v = e.get("vis20s_book")
        if v is not None:
            byday_vis[e["day"]].append(v)
    out["per_day_visible_20s_before_book"] = {
        d: round(float(np.mean(v)), 4) for d, v in sorted(byday_vis.items()) if v
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", nargs="*", default=DAYS)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    load_stats: dict[str, dict[str, int]] = {}
    all_eps: list[dict[str, Any]] = []
    block_meta: list[dict[str, Any]] = []
    for day in args.days:
        rows, stats = load_live_books(day)
        load_stats[day] = dict(stats)
        if not rows:
            continue
        for blk in build_blocks(day, rows):
            block_meta.append({
                "day": day, "sess": blk.sess, "n": len(blk),
                "start": datetime.fromtimestamp(blk.t[0], tz=TZ).strftime("%m-%d %H:%M:%S"),
                "end": datetime.fromtimestamp(blk.t[-1], tz=TZ).strftime("%m-%d %H:%M:%S"),
                "span_min": round((blk.t[-1] - blk.t[0]) / 60.0, 1),
                "median_gap_sec": round(float(np.median(np.diff(blk.t))), 4) if len(blk) > 1 else None,
            })
            all_eps.extend(scan_block(blk))
        print(f"{day}: live={stats['live']} zombie={stats['stale_zombie']} eps={len(all_eps)}",
              flush=True)

    # 去重：同一天／同 session／同 side／同 D／同 L／同 t_touch 的 episode 是同一個
    # 觸價事件被多個 anchor 重複抓到，只保留一個，避免把自相關當成獨立樣本。
    seen: set[tuple] = set()
    dedup: list[dict[str, Any]] = []
    for e in all_eps:
        if not e["touched"]:
            dedup.append(e)
            continue
        k = (e["day"], e["sess"], e["side"], e["D"], e["L"], round(e["t_touch"], 3))
        if k in seen:
            continue
        seen.add(k)
        dedup.append(e)

    result: dict[str, Any] = {
        "generated_at": datetime.now(tz=TZ).isoformat(timespec="seconds"),
        "question": "20s 對帳迴圈能否在觸價前看到掛單價位上的五檔？",
        "config": {
            "distances_pts": DISTANCES, "anchor_step_sec": ANCHOR_STEP_SEC,
            "horizon_sec": HORIZON_SEC, "vis_pts_geometric": VIS_PTS,
            "stale_filter": "stale flag OR (ts - book_time) > 5s",
            "touch_def": "up: best_bid >= L ; dn: best_ask <= L",
            "visible_book_def": "up: worst_ask_price >= L ; dn: worst_bid_price <= L",
            "worker_interval_sec_current": 20.0,
        },
        "load_stats": load_stats,
        "blocks": block_meta,
        "n_episodes_raw": len(all_eps),
        "n_episodes_dedup": len(dedup),
    }
    result["overall"] = summarize(dedup, "all")
    result["by_distance"] = {
        str(D): summarize([e for e in dedup if e["D"] == D], f"D={D}") for D in DISTANCES
    }
    result["by_session"] = {
        s: summarize([e for e in dedup if e["sess"] == s], f"sess={s}") for s in ("day", "night")
    }
    result["by_distance_session"] = {
        f"D{D}_{s}": summarize([e for e in dedup if e["D"] == D and e["sess"] == s], f"D={D},{s}")
        for D in (12, 24, 33, 66) for s in ("day", "night")
    }
    out = Path(args.out) if args.out else (
        Path(__file__).resolve().parents[2] / "reports" / "research" / "channel_lab"
        / "wall_b3_reaction_window.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    o = result["overall"]
    print(f"overall n={o['n_episodes']} touched={o['n_touched']} "
          f"window_book p50={o['window_book_sec']['p50']}s p90={o['window_book_sec']['p90']}s "
          f"vis20s={o.get('visible_20s_before_touch_book')} "
          f"need_interval_80pct={o['interval_for_coverage']['80pct_book']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
