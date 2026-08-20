#!/usr/bin/env python3
"""A1 — 站得住腳的「牆（wall / iceberg-ish depth cluster）」定義與完整畫像。

背景：樸素定義「該側最大檔 size >= 該側五檔中位 x3」已知失敗——命中位置單調集中
在第 5 檔，抓到的其實是「離觸價越遠掛越多」的常態斜率，不是異常牆。

本腳本做四件事：
  P1 逐檔（tier 1..5，買賣分開、日盤／夜盤分開）的 size 分布 = 簿子的常態形狀基準線。
  P2 正規化牆偵測器：wall(k) = size_k >= N x rolling_median(size_k over past W sec)
     —— 基準線是「同一檔自己的過去」，因此常態斜率被除掉。嚴格因果：算基準線時
     只用嚴格早於當前列的樣本（append 在 evaluate 之後）。掃 N in {2,3,5,10}。
  P3 牆的畫像：絕對口數、離 mid 幾點、命中檔位分布、買賣對稱性、日夜差、每日次數。
  P4 逐檔報價存續期（tier 1..5），並拆「厚檔 vs 薄檔」；再加「牆 episode」壽命
     ——以價位（price level）為單位追蹤，區分 shrink（被吃掉／撤單）與 out_of_book
     （價格走遠、跑出五檔視窗＝右設限 censoring，不是牆死掉）。

殭屍列過濾（強制）：有 stale 欄位就信它；沒有的舊資料用 ts - book_time > 5s 判定。
session：一律用 quote_type（FUTURE=日盤 / FUTURE_AH=夜盤），該欄所有列都有。

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/wall_a1_definition_profile.py \
      --days 2026-08-14 2026-08-15 2026-08-17 2026-08-18 2026-08-19
"""

from __future__ import annotations

import argparse
import json
import math
from bisect import bisect_left
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TZ = timezone(timedelta(hours=8))
MAX_BOOK_AGE_SEC = 5.0          # 殭屍門檻（沒有 stale 欄位的舊資料用）
BASE_WINDOW_SEC = 300.0         # 滾動基準線視窗（過去 5 分鐘）
BASE_REFRESH_SEC = 5.0          # 基準線重算間隔（省算力；仍嚴格只用過去）
BASE_SUBSAMPLE_SEC = 0.2        # 基準線樣本最小間隔（避免同秒爆量灌爆視窗）
BASE_MIN_SAMPLES = 100          # 樣本不足就不判定
BASE_MIN_SPAN_SEC = 60.0        # 視窗跨度不足就不判定
GAP_RESET_SEC = 120.0           # 資料斷檔／換盤 → 重置所有狀態
NS = (2.0, 3.0, 5.0, 10.0)
WALL_N_PORTRAIT = 3.0           # 畫像／episode 追蹤採用的 N
EPISODE_SHRINK_FRAC = 0.5       # 量掉到起始的一半以下 = 牆死
EPISODE_MISS_SEC = 2.0          # 價位消失超過這麼久 = 跑出視窗（censored）


# ---------------------------------------------------------------- utilities
class QC:
    """counter-based quantiles（避免存 700 萬個 float）。"""

    def __init__(self) -> None:
        self.c: Counter = Counter()

    def add(self, v: float) -> None:
        self.c[v] += 1

    @property
    def n(self) -> int:
        return sum(self.c.values())

    def q(self, p: float) -> float:
        n = self.n
        if not n:
            return float("nan")
        keys = sorted(self.c)
        cum, target = 0, p * (n - 1)
        for k in keys:
            cum += self.c[k]
            if cum > target:
                return float(k)
        return float(keys[-1])

    def mean(self) -> float:
        n = self.n
        return sum(k * v for k, v in self.c.items()) / n if n else float("nan")

    def summary(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "mean": round(self.mean(), 3) if self.n else None,
            "p10": self.q(0.10), "p50": self.q(0.50),
            "p90": self.q(0.90), "p99": self.q(0.99),
            "max": max(self.c) if self.c else None,
        }


def books_dir() -> Path:
    try:
        import stock_db  # noqa: PLC0415

        return Path(stock_db.DATA_DIR).parent / "cache" / "tmf_books"
    except Exception:  # noqa: BLE001
        return Path.home() / "goldenstocks-data" / "cache" / "tmf_books"


def load_live_books(day: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """與 tmf_book_microstructure_diag.load_live_books 同一套殭屍過濾，外加 session。"""
    path = books_dir() / f"tmf_books_{day}.jsonl"
    stats: Counter = Counter()
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out, dict(stats)
    with path.open(encoding="utf-8") as fh:
        for line in fh:
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
            if len(bids) < 5 or len(asks) < 5:
                stats["short_book"] += 1
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
                continue
            qt = str(r.get("quote_type") or "")
            sess = "night" if qt.endswith("_AH") else "day"
            stats[f"live_{sess}"] += 1
            out.append(
                {
                    "t": book_ts.timestamp(),
                    "dt": book_ts,
                    "sess": sess,
                    "bp": [int(b["price"]) for b in bids[:5]],
                    "bs": [int(b["size"]) for b in bids[:5]],
                    "ap": [int(a["price"]) for a in asks[:5]],
                    "as": [int(a["size"]) for a in asks[:5]],
                }
            )
    out.sort(key=lambda r: r["t"])
    return out, dict(stats)


def session_key(row: dict[str, Any]) -> str:
    """叢集單位：日盤用日期；夜盤跨午夜 → 早於 06:00 的歸前一天晚上。"""
    d = row["dt"]
    if row["sess"] == "day":
        return f"{d:%Y-%m-%d}-day"
    ref = d - timedelta(hours=6)
    return f"{ref:%Y-%m-%d}-night"


# ------------------------------------------------------- rolling baselines
class TierBaseline:
    """單一 (side, tier) 的因果滾動中位數。"""

    __slots__ = ("buf", "last_sample_t", "last_calc_t", "value", "sorted_cache")

    def __init__(self) -> None:
        self.buf: deque = deque()      # (t, size)
        self.last_sample_t = -1e18
        self.last_calc_t = -1e18
        self.value: float | None = None

    def reset(self) -> None:
        self.buf.clear()
        self.last_sample_t = -1e18
        self.last_calc_t = -1e18
        self.value = None

    def refresh(self, t: float) -> None:
        if t - self.last_calc_t < BASE_REFRESH_SEC:
            return
        self.last_calc_t = t
        cut = t - BASE_WINDOW_SEC
        while self.buf and self.buf[0][0] < cut:
            self.buf.popleft()
        if len(self.buf) < BASE_MIN_SAMPLES or (t - self.buf[0][0]) < BASE_MIN_SPAN_SEC:
            self.value = None
            return
        vals = sorted(s for _, s in self.buf)
        m = len(vals)
        self.value = float(vals[m // 2]) if m % 2 else 0.5 * (vals[m // 2 - 1] + vals[m // 2])

    def add(self, t: float, size: int) -> None:
        if t - self.last_sample_t >= BASE_SUBSAMPLE_SEC:
            self.buf.append((t, size))
            self.last_sample_t = t


def main() -> int:
    global BASE_WINDOW_SEC, WALL_N_PORTRAIT  # noqa: PLW0603 — 敏感度測試旋鈕
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", nargs="+", required=True)
    ap.add_argument(
        "--out",
        default="reports/research/channel_lab/wall_a1_definition_profile.json",
    )
    ap.add_argument("--base-window-sec", type=float, default=BASE_WINDOW_SEC,
                    help="滾動基準線視窗（敏感度測試用）")
    ap.add_argument("--portrait-n", type=float, default=WALL_N_PORTRAIT)
    args = ap.parse_args()
    BASE_WINDOW_SEC = args.base_window_sec
    WALL_N_PORTRAIT = args.portrait_n

    rows: list[dict[str, Any]] = []
    load_stats: dict[str, dict[str, int]] = {}
    for day in args.days:
        rr, st_ = load_live_books(day)
        load_stats[day] = st_
        print(f"[{day}] {st_}")
        rows.extend(rr)
    rows.sort(key=lambda r: r["t"])
    if not rows:
        print("no live rows")
        return 1
    print(f"\nlive snapshots: {len(rows)}  {rows[0]['dt']:%F %T} → {rows[-1]['dt']:%F %T}\n")

    # ---------------- P1 逐檔 size 分布 -----------------------------------
    tier_dist: dict[tuple[str, str, int], QC] = defaultdict(QC)
    span_dist: dict[str, QC] = defaultdict(QC)      # 五檔跨幅（點）
    spread_dist: dict[str, QC] = defaultdict(QC)
    for r in rows:
        s = r["sess"]
        for k in range(5):
            tier_dist[(s, "bid", k + 1)].add(r["bs"][k])
            tier_dist[(s, "ask", k + 1)].add(r["as"][k])
        span_dist[s].add(r["bp"][0] - r["bp"][4])
        span_dist[s].add(r["ap"][4] - r["ap"][0])
        spread_dist[s].add(r["ap"][0] - r["bp"][0])

    # ---------------- P2/P3 牆偵測 + 畫像 ---------------------------------
    base: dict[tuple[str, int], TierBaseline] = {
        (side, k): TierBaseline() for side in ("bid", "ask") for k in range(1, 6)
    }
    evaluable = 0                                    # 十個檔位全都有基準線的快照數
    eval_by_sess: Counter = Counter()
    hits: dict[float, Counter] = {n: Counter() for n in NS}          # key: (sess,side,tier)
    hits_any: dict[float, Counter] = {n: Counter() for n in NS}      # key: sess -> 快照有任一命中
    hits_by_day: dict[float, Counter] = {n: Counter() for n in NS}
    eval_by_day: Counter = Counter()
    ratio_dist: dict[tuple[str, int], QC] = defaultdict(QC)          # size/baseline 分布
    # 畫像（N=WALL_N_PORTRAIT）
    port_size: dict[tuple[str, str], QC] = defaultdict(QC)
    port_dist_mid: dict[tuple[str, str], QC] = defaultdict(QC)
    port_ratio: dict[tuple[str, str], QC] = defaultdict(QC)
    port_tier: Counter = Counter()
    # 樸素定義對照（已知失敗版本）
    naive_hits: Counter = Counter()
    naive_tier: Counter = Counter()

    # P4 逐檔存續期
    life: dict[tuple[int, str], QC] = defaultdict(QC)       # (tier, thickclass) -> 秒
    life_all: dict[int, QC] = defaultdict(QC)
    prev_key: dict[tuple[str, int], tuple[Any, float, str]] = {}
    # 牆 episode（以價位追蹤）
    episodes: list[dict[str, Any]] = []
    active: dict[tuple[str, int], dict[str, Any]] = {}

    def close_episode(key: tuple[str, int], t: float, reason: str) -> None:
        ep = active.pop(key, None)
        if ep is None:
            return
        ep["end"] = t
        ep["dur"] = max(0.0, (ep["last_seen"] if reason != "shrink" else t) - ep["t0"])
        ep["reason"] = reason
        episodes.append(ep)

    prev_t = rows[0]["t"]
    prev_sess_key = session_key(rows[0])
    for r in rows:
        t, sess = r["t"], r["sess"]
        skey = session_key(r)
        if t - prev_t > GAP_RESET_SEC or skey != prev_sess_key:
            for b in base.values():
                b.reset()
            prev_key.clear()
            for k in list(active):
                close_episode(k, prev_t, "session_end")
        prev_t, prev_sess_key = t, skey

        mid = 0.5 * (r["bp"][0] + r["ap"][0])

        # --- 樸素定義對照（同一快照內、同側五檔中位 x3）---
        for side, sizes in (("bid", r["bs"]), ("ask", r["as"])):
            srt = sorted(sizes)
            med = srt[2]
            mx = max(sizes)
            if med > 0 and mx >= 3 * med:
                naive_hits[(sess, side)] += 1
                naive_tier[(sess, side, sizes.index(mx) + 1)] += 1
            naive_hits[(sess, side, "denom")] += 1

        # --- 正規化偵測器 ---
        for b in base.values():
            b.refresh(t)
        ready = all(base[(side, k)].value for side in ("bid", "ask") for k in range(1, 6))
        if ready:
            evaluable += 1
            eval_by_sess[sess] += 1
            eval_by_day[skey] += 1
            any_hit = {n: False for n in NS}
            for side, sizes, prices in (("bid", r["bs"], r["bp"]), ("ask", r["as"], r["ap"])):
                for k in range(1, 6):
                    bv = base[(side, k)].value
                    sz = sizes[k - 1]
                    ratio = sz / bv
                    ratio_dist[(sess, k)].add(round(ratio, 2))
                    for n in NS:
                        if ratio >= n:
                            hits[n][(sess, side, k)] += 1
                            hits_by_day[n][skey] += 1
                            any_hit[n] = True
                    if ratio >= WALL_N_PORTRAIT:
                        port_size[(sess, side)].add(sz)
                        port_dist_mid[(sess, side)].add(abs(prices[k - 1] - mid))
                        port_ratio[(sess, side)].add(round(ratio, 1))
                        port_tier[(sess, side, k)] += 1
                        ekey = (side, prices[k - 1])
                        if ekey not in active:
                            active[ekey] = {
                                "t0": t, "sess": sess, "skey": skey, "side": side,
                                "price": prices[k - 1], "size0": sz, "tier0": k,
                                "dist_mid0": abs(prices[k - 1] - mid), "last_seen": t,
                                "ratio0": round(ratio, 2),
                            }
            for n in NS:
                if any_hit[n]:
                    hits_any[n][sess] += 1
        # 樣本一律在「評估之後」才餵進基準線 → 基準線嚴格只含過去的列
        for side, sizes in (("bid", r["bs"]), ("ask", r["as"])):
            for k in range(1, 6):
                base[(side, k)].add(t, sizes[k - 1])

        # --- episode 維護：追蹤價位是否還在、量有沒有縮 ---
        for ekey in list(active):
            side, price = ekey
            prices = r["bp"] if side == "bid" else r["ap"]
            sizes = r["bs"] if side == "bid" else r["as"]
            ep = active[ekey]
            if price in prices:
                idx = prices.index(price)
                ep["last_seen"] = t
                ep["tier_last"] = idx + 1
                if sizes[idx] < EPISODE_SHRINK_FRAC * ep["size0"]:
                    close_episode(ekey, t, "shrink")
            elif t - ep["last_seen"] > EPISODE_MISS_SEC:
                close_episode(ekey, t, "out_of_book")

        # --- P4 逐檔 (price,size) 存續期 ---
        for side, sizes, prices in (("bid", r["bs"], r["bp"]), ("ask", r["as"], r["ap"])):
            for k in range(1, 6):
                key = (prices[k - 1], sizes[k - 1])
                pk = prev_key.get((side, k))
                if pk is None:
                    bv = base[(side, k)].value
                    cls = "unknown" if not bv else ("thick" if sizes[k - 1] >= 3 * bv
                                                    else ("thin" if sizes[k - 1] <= bv else "mid"))
                    prev_key[(side, k)] = (key, t, cls)
                    continue
                if key != pk[0]:
                    dur = round(t - pk[1], 3)
                    life_all[k].add(dur)
                    life[(k, pk[2])].add(dur)
                    bv = base[(side, k)].value
                    cls = "unknown" if not bv else ("thick" if sizes[k - 1] >= 3 * bv
                                                    else ("thin" if sizes[k - 1] <= bv else "mid"))
                    prev_key[(side, k)] = (key, t, cls)
    for k in list(active):
        close_episode(k, prev_t, "session_end")

    # ---------------- 輸出 ------------------------------------------------
    def pctl(vals: list[float], p: float) -> float:
        if not vals:
            return float("nan")
        s = sorted(vals)
        return s[min(len(s) - 1, int(p * (len(s) - 1)))]

    print("=== P1 逐檔 size 分布（口）===")
    print(f"{'sess':<6}{'side':<5}{'tier':<5}{'n':>9}{'mean':>8}{'p10':>6}{'p50':>6}{'p90':>7}{'p99':>7}{'max':>7}")
    p1: dict[str, Any] = {}
    for sess in ("day", "night"):
        for side in ("bid", "ask"):
            for k in range(1, 6):
                qc = tier_dist[(sess, side, k)]
                if not qc.n:
                    continue
                s = qc.summary()
                p1[f"{sess}|{side}|t{k}"] = s
                print(f"{sess:<6}{side:<5}{k:<5}{s['n']:>9}{s['mean']:>8.2f}{s['p10']:>6.0f}"
                      f"{s['p50']:>6.0f}{s['p90']:>7.0f}{s['p99']:>7.0f}{s['max']:>7.0f}")
    for sess in ("day", "night"):
        if spread_dist[sess].n:
            print(f"   [{sess}] spread p50={spread_dist[sess].q(.5):.0f} pts · "
                  f"五檔單側跨幅 p50={span_dist[sess].q(.5):.0f} p90={span_dist[sess].q(.9):.0f} pts")

    print(f"\n=== P2 正規化牆偵測器（基準線 = 該檔過去 {BASE_WINDOW_SEC:.0f}s 中位）===")
    print(f"可判定快照 {evaluable} / {len(rows)}  ({100.0*evaluable/len(rows):.1f}%)  "
          f"day={eval_by_sess['day']} night={eval_by_sess['night']}")
    p2: dict[str, Any] = {}
    print(f"{'N':<5}{'sess':<7}{'側':<5}" + "".join(f"{'t'+str(k):>9}" for k in range(1, 6))
          + f"{'per-tier hit%':>15}{'any-tier snap%':>16}")
    for n in NS:
        for sess in ("day", "night"):
            den = eval_by_sess[sess]
            if not den:
                continue
            for side in ("bid", "ask"):
                cells = [hits[n][(sess, side, k)] for k in range(1, 6)]
                tot = sum(cells)
                shares = [f"{100.0*c/tot:>8.1f}%" if tot else "     n/a" for c in cells]
                p2[f"N{n:g}|{sess}|{side}"] = {
                    "hits_by_tier": cells,
                    "tier_share_pct": [round(100.0 * c / tot, 1) if tot else None for c in cells],
                    "per_tier_hit_rate_pct": round(100.0 * tot / (5 * den), 3),
                }
                print(f"{n:<5g}{sess:<7}{side:<5}" + "".join(shares)
                      + f"{100.0*tot/(5*den):>14.2f}%"
                      + (f"{100.0*hits_any[n][sess]/den:>15.1f}%" if side == "bid" else " " * 16))
    # 樸素對照
    print("\n   [對照] 樸素定義『同一快照最大檔 >= 該側五檔中位 x3』")
    p2_naive: dict[str, Any] = {}
    for sess in ("day", "night"):
        for side in ("bid", "ask"):
            den = naive_hits[(sess, side, "denom")]
            if not den:
                continue
            h = naive_hits[(sess, side)]
            tiers = [naive_tier[(sess, side, k)] for k in range(1, 6)]
            p2_naive[f"{sess}|{side}"] = {"hit_rate_pct": round(100.0 * h / den, 2),
                                          "hit_tier_counts": tiers}
            print(f"   {sess:<6}{side:<5} hit={100.0*h/den:>5.1f}%  命中檔位分布 t1..t5 = {tiers}")

    print(f"\n=== P3 牆的畫像（N={WALL_N_PORTRAIT:g}）===")
    p3: dict[str, Any] = {}
    for sess in ("day", "night"):
        for side in ("bid", "ask"):
            qs, qd, qr = port_size[(sess, side)], port_dist_mid[(sess, side)], port_ratio[(sess, side)]
            if not qs.n:
                continue
            tiers = [port_tier[(sess, side, k)] for k in range(1, 6)]
            p3[f"{sess}|{side}"] = {
                "n_hits": qs.n,
                "size_lots": qs.summary(),
                "dist_from_mid_pts": qd.summary(),
                "ratio_vs_baseline": qr.summary(),
                "hit_tier_counts": tiers,
            }
            tot_h = qs.n
            ge = {f"size_ge_{th}_pct": round(100.0 * sum(v for k2, v in qs.c.items() if k2 >= th) / tot_h, 1)
                  for th in (10, 20, 30, 50)}
            p3[f"{sess}|{side}"]["abs_size_share_pct"] = ge
            print(f"       絕對口數門檻: " + "  ".join(f"{k2}={v}%" for k2, v in ge.items()))
            print(f"{sess:<6}{side:<5} n={qs.n:>7}  size p50={qs.q(.5):>4.0f} p90={qs.q(.9):>5.0f} "
                  f"max={max(qs.c):>4}  離mid p50={qd.q(.5):.1f} p90={qd.q(.9):.1f} pts  "
                  f"ratio p50={qr.q(.5):.1f}  tiers={tiers}")
    # 每個 session-day 的次數（叢集單位）
    per_day = {}
    for skey, den in sorted(eval_by_day.items()):
        n_ep = sum(1 for e in episodes if e["skey"] == skey)
        per_day[skey] = {"evaluable_snaps": den, "wall_tier_hits_N3": hits_by_day[3.0][skey],
                         "wall_episodes": n_ep,
                         "hit_rate_pct": round(100.0 * hits_by_day[3.0][skey] / (10 * den), 3) if den else None}
        print(f"   {skey:<22} evaluable={den:>7}  N3 tier-hits={hits_by_day[3.0][skey]:>7}"
              f"  episodes={n_ep:>5}  hit={per_day[skey]['hit_rate_pct']}%")

    print("\n=== P4a 逐檔報價存續期（(price,size) 不變的秒數）===")
    print(f"{'tier':<6}{'class':<9}{'n':>9}{'p50':>8}{'p75':>8}{'p90':>8}{'p99':>8}{'mean':>8}")
    p4: dict[str, Any] = {}
    for k in range(1, 6):
        for cls in ("all", "thin", "mid", "thick"):
            qc = life_all[k] if cls == "all" else life[(k, cls)]
            if qc.n < 30:
                continue
            p4[f"t{k}|{cls}"] = {"n": qc.n, "p50": qc.q(.5), "p75": qc.q(.75),
                                 "p90": qc.q(.9), "p99": qc.q(.99), "mean": round(qc.mean(), 3)}
            print(f"{k:<6}{cls:<9}{qc.n:>9}{qc.q(.5):>8.3f}{qc.q(.75):>8.3f}"
                  f"{qc.q(.9):>8.3f}{qc.q(.99):>8.3f}{qc.mean():>8.3f}")

    print(f"\n=== P4b 牆 episode 壽命（以價位追蹤 · N={WALL_N_PORTRAIT:g}）===")
    by_reason: dict[str, list[float]] = defaultdict(list)
    for e in episodes:
        by_reason[e["reason"]].append(e["dur"])
    all_dur = [e["dur"] for e in episodes]
    p4b: dict[str, Any] = {"n_episodes": len(episodes)}
    if all_dur:
        print(f"episodes n={len(episodes)}  全體 dur p50={pctl(all_dur,.5):.2f}s "
              f"p75={pctl(all_dur,.75):.2f}s p90={pctl(all_dur,.9):.2f}s max={max(all_dur):.1f}s")
        p4b["all"] = {"n": len(all_dur), "p50": round(pctl(all_dur, .5), 3),
                      "p75": round(pctl(all_dur, .75), 3), "p90": round(pctl(all_dur, .9), 3),
                      "max": round(max(all_dur), 1)}
        for reason, ds in sorted(by_reason.items()):
            print(f"   {reason:<14} n={len(ds):>6} ({100.0*len(ds)/len(all_dur):>4.1f}%)  "
                  f"p50={pctl(ds,.5):>7.2f}s p90={pctl(ds,.9):>8.2f}s")
            p4b[reason] = {"n": len(ds), "share_pct": round(100.0 * len(ds) / len(all_dur), 1),
                           "p50": round(pctl(ds, .5), 3), "p90": round(pctl(ds, .9), 3)}
        # 生存函數
        surv = {}
        for thr in (1, 2, 5, 10, 30, 60, 120):
            surv[f">{thr}s"] = round(100.0 * sum(1 for d in all_dur if d > thr) / len(all_dur), 2)
        p4b["survival_pct"] = surv
        print("   生存率 " + "  ".join(f"{k}:{v}%" for k, v in surv.items()))
        # 只看 shrink（真死）的存活；out_of_book 視為右設限
        shr = by_reason.get("shrink", [])
        if shr:
            print(f"   [只看 shrink] p50={pctl(shr,.5):.2f}s p90={pctl(shr,.9):.2f}s "
                  f"— 其餘 {100.0*(1-len(shr)/len(all_dur)):.1f}% 是 censored（跑出五檔／收盤）")
        for sess in ("day", "night"):
            ds = [e["dur"] for e in episodes if e["sess"] == sess]
            if len(ds) >= 20:
                p4b[f"sess_{sess}"] = {"n": len(ds), "p50": round(pctl(ds, .5), 3),
                                       "p90": round(pctl(ds, .9), 3),
                                       "gt10s_pct": round(100.0 * sum(1 for d in ds if d > 10) / len(ds), 1)}
                print(f"   [{sess}] n={len(ds):>5} p50={pctl(ds,.5):>6.2f}s p90={pctl(ds,.9):>7.2f}s "
                      f">10s={100.0*sum(1 for d in ds if d>10)/len(ds):.1f}%")
        for lab, lo, hi in (("dist<=2pt", 0, 2), ("dist3-4pt", 3, 4), ("dist>=5pt", 5, 99)):
            ds = [e["dur"] for e in episodes if lo <= e["dist_mid0"] <= hi]
            if len(ds) >= 20:
                p4b[lab] = {"n": len(ds), "p50": round(pctl(ds, .5), 3), "p90": round(pctl(ds, .9), 3)}
                print(f"   [{lab}] n={len(ds):>5} p50={pctl(ds,.5):>6.2f}s p90={pctl(ds,.9):>7.2f}s")
        big = [e for e in episodes if e["size0"] >= 20]
        if big:
            bd = [e["dur"] for e in big]
            p4b["size0_ge_20"] = {"n": len(big), "p50": round(pctl(bd, .5), 3),
                                  "p90": round(pctl(bd, .9), 3)}
            print(f"   [起始 >=20 口] n={len(big)} dur p50={pctl(bd,.5):.2f}s p90={pctl(bd,.9):.2f}s")

    payload = {
        "schema": "tmf-wall-a1-definition-profile-v1",
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "days": args.days,
        "params": {
            "base_window_sec": BASE_WINDOW_SEC, "base_refresh_sec": BASE_REFRESH_SEC,
            "base_subsample_sec": BASE_SUBSAMPLE_SEC, "base_min_samples": BASE_MIN_SAMPLES,
            "gap_reset_sec": GAP_RESET_SEC, "wall_N_grid": list(NS),
            "portrait_N": WALL_N_PORTRAIT, "episode_shrink_frac": EPISODE_SHRINK_FRAC,
            "episode_miss_sec": EPISODE_MISS_SEC, "zombie_max_age_sec": MAX_BOOK_AGE_SEC,
        },
        "load_stats": load_stats,
        "n_live_snapshots": len(rows),
        "n_evaluable_snapshots": evaluable,
        "p1_tier_size_dist": p1,
        "p1_spread_p50": {s: spread_dist[s].q(.5) for s in ("day", "night") if spread_dist[s].n},
        "p1_side_span_pts": {s: span_dist[s].summary() for s in ("day", "night") if span_dist[s].n},
        "p2_normalized_detector": p2,
        "p2_any_tier_snapshot_hit_pct": {
            f"N{n:g}|{s}": round(100.0 * hits_any[n][s] / eval_by_sess[s], 2)
            for n in NS for s in ("day", "night") if eval_by_sess[s]
        },
        "p2_naive_reference": p2_naive,
        "p3_portrait": p3,
        "p3_per_session_day": per_day,
        "p4a_tier_quote_lifetime_sec": p4,
        "p4b_wall_episode_lifetime_sec": p4b,
    }
    outp = Path(args.out)
    if not outp.is_absolute():
        outp = Path(__file__).resolve().parents[2] / outp
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
