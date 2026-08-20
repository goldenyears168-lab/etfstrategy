#!/usr/bin/env python3
"""rev_a1 — 對抗性複核 A1「牆偵測器」結論。

獨立重實作偵測器，外加三件原作沒做的事：
  R1 殭屍過濾與資料完整性稽核（含 stale 欄位 vs age 規則一致性、day/night 實際涵蓋時段）
  R2 episode 壽命的存活分析：原作把 35.8% 右設限 episode 當成「已死」丟進同一個中位數，
     這會把壽命往下拉。這裡用 Kaplan–Meier 重估，並掃四種死亡定義。
  R3 原作完全沒做的：牆有沒有預測力？以 episode 起點為事件，量 signed forward mid move
     （bid 牆 +、ask 牆 −）在 5/15/60/300 秒的效果量，並與同 session-day 的
     placebo（隨機時點、同樣本數、同 side 比例）對照。以 session-day 為叢集報告。
"""
from __future__ import annotations
import argparse, json, math, random
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

TZ = timezone(timedelta(hours=8))
MAX_AGE = 5.0
BASE_WINDOW = 300.0
BASE_REFRESH = 5.0
BASE_SUB = 0.2
BASE_MIN_N = 100
BASE_MIN_SPAN = 60.0
GAP_RESET = 120.0
N_WALL = 3.0


def books_dir() -> Path:
    return Path.home() / "goldenstocks-data" / "cache" / "tmf_books"


def load(day: str):
    p = books_dir() / f"tmf_books_{day}.jsonl"
    out, st = [], Counter()
    if not p.exists():
        return out, st
    with p.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            st["rows"] += 1
            r = json.loads(line)
            b, a = r.get("bids") or [], r.get("asks") or []
            if len(b) < 5 or len(a) < 5:
                st["short"] += 1
                continue
            wall = datetime.fromisoformat(str(r["ts"])).astimezone(TZ)
            bt = datetime.fromtimestamp(float(r["book_time"]) / 1e6, tz=TZ)
            drop = bool(r["stale"]) if "stale" in r else ((wall - bt).total_seconds() > MAX_AGE)
            if drop:
                st["zombie"] += 1
                continue
            qt = str(r.get("quote_type") or "")
            sess = "night" if qt.endswith("_AH") else "day"
            st[f"live_{sess}"] += 1
            out.append({
                "t": bt.timestamp(), "dt": bt, "sess": sess,
                "bp": [int(x["price"]) for x in b[:5]], "bs": [int(x["size"]) for x in b[:5]],
                "ap": [int(x["price"]) for x in a[:5]], "asz": [int(x["size"]) for x in a[:5]],
            })
    out.sort(key=lambda r: r["t"])
    return out, st


def skey_of(r):
    d = r["dt"]
    if r["sess"] == "day":
        return f"{d:%Y-%m-%d}-day"
    return f"{d - timedelta(hours=6):%Y-%m-%d}-night"


class Base:
    __slots__ = ("buf", "ls", "lc", "value")

    def __init__(self):
        self.buf = deque(); self.ls = -1e18; self.lc = -1e18; self.value = None

    def reset(self):
        self.buf.clear(); self.ls = -1e18; self.lc = -1e18; self.value = None

    def refresh(self, t):
        if t - self.lc < BASE_REFRESH:
            return
        self.lc = t
        cut = t - BASE_WINDOW
        while self.buf and self.buf[0][0] < cut:
            self.buf.popleft()
        if len(self.buf) < BASE_MIN_N or (t - self.buf[0][0]) < BASE_MIN_SPAN:
            self.value = None; return
        v = sorted(s for _, s in self.buf); m = len(v)
        self.value = float(v[m // 2]) if m % 2 else 0.5 * (v[m // 2 - 1] + v[m // 2])

    def add(self, t, s):
        if t - self.ls >= BASE_SUB:
            self.buf.append((t, s)); self.ls = t


def km_median(durs, events):
    """Kaplan-Meier: durs 秒, events 1=死 0=右設限。回傳 (median, S(t) at 1/2/5/10/30s)."""
    order = np.argsort(durs, kind="mergesort")
    d = np.asarray(durs)[order]; e = np.asarray(events)[order]
    n = len(d); S = 1.0; surv_t, surv_s = [], []
    i = 0; at_risk = n
    while i < n:
        j = i
        while j < n and d[j] == d[i]:
            j += 1
        deaths = int(e[i:j].sum())
        if deaths:
            S *= (1.0 - deaths / at_risk)
        surv_t.append(d[i]); surv_s.append(S)
        at_risk -= (j - i)
        i = j
    surv_t = np.array(surv_t); surv_s = np.array(surv_s)
    med = float("nan")
    idx = np.where(surv_s <= 0.5)[0]
    if len(idx):
        med = float(surv_t[idx[0]])
    pts = {}
    for thr in (1, 2, 5, 10, 30, 60):
        k = np.searchsorted(surv_t, thr, side="right") - 1
        pts[f"S({thr}s)"] = round(float(surv_s[k]) if k >= 0 else 1.0, 4)
    return med, pts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="+", default=["2026-08-14", "2026-08-15", "2026-08-17",
                                                  "2026-08-18", "2026-08-19"])
    ap.add_argument("--out", default="reports/research/channel_lab/wall_rev_a1_audit.json")
    args = ap.parse_args()

    rows, load_stats = [], {}
    for day in args.days:
        rr, st = load(day)
        load_stats[day] = dict(st)
        rows.extend(rr)
        print(f"[{day}] {dict(st)}")
    rows.sort(key=lambda r: r["t"])
    print(f"live={len(rows)}")

    # ---- 逐 session-day 切片 -------------------------------------------------
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[skey_of(r)].append(r)

    payload: dict[str, Any] = {"schema": "tmf-wall-rev-a1-audit-v1",
                               "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
                               "days": args.days, "load_stats": load_stats,
                               "n_live": len(rows)}

    # 資料涵蓋稽核
    cov = {}
    for k, g in sorted(groups.items()):
        ts = [r["t"] for r in g]
        cov[k] = {"n": len(g), "first": f"{g[0]['dt']:%m-%d %H:%M:%S}",
                  "last": f"{g[-1]['dt']:%m-%d %H:%M:%S}",
                  "span_min": round((ts[-1] - ts[0]) / 60, 1),
                  "median_gap_ms": round(float(np.median(np.diff(ts))) * 1000, 1) if len(ts) > 2 else None,
                  "gaps_gt_60s": int(sum(1 for x in np.diff(ts) if x > 60))}
    payload["coverage"] = cov
    print(json.dumps(cov, ensure_ascii=False, indent=1))

    # ---- 偵測 + episode（四種死亡定義）+ 事件 -------------------------------
    DEATHS = ("shrink50", "shrink20", "below_wall", "level_gone")
    eps: dict[str, list] = {d: [] for d in DEATHS}
    events = []          # (skey, t, side, price, size0, dist_mid0, tier0)
    eval_n = Counter(); hit_n = Counter()
    dist_hist = Counter()
    far_book = Counter()  # 簿子最遠檔離 mid 的分布

    for skey, g in sorted(groups.items()):
        base = {(s, k): Base() for s in ("bid", "ask") for k in range(1, 6)}
        active = {d: {} for d in DEATHS}
        prev_t = g[0]["t"]
        for r in g:
            t = r["t"]
            if t - prev_t > GAP_RESET:
                for b in base.values():
                    b.reset()
                for d in DEATHS:
                    for kk in list(active[d]):
                        ep = active[d].pop(kk)
                        ep["dur"] = ep["last_seen"] - ep["t0"]; ep["event"] = 0
                        eps[d].append(ep)
            prev_t = t
            mid = 0.5 * (r["bp"][0] + r["ap"][0])
            far_book[round(max(abs(r["bp"][4] - mid), abs(r["ap"][4] - mid)) * 2) / 2] += 1
            for b in base.values():
                b.refresh(t)
            ready = all(base[(s, k)].value for s in ("bid", "ask") for k in range(1, 6))
            if ready:
                eval_n[skey] += 1
                for side, sizes, prices in (("bid", r["bs"], r["bp"]), ("ask", r["asz"], r["ap"])):
                    for k in range(1, 6):
                        bv = base[(side, k)].value
                        if sizes[k - 1] >= N_WALL * bv:
                            hit_n[skey] += 1
                            price = prices[k - 1]
                            dm = abs(price - mid)
                            dist_hist[dm] += 1
                            ek = (side, price)
                            fresh = ek not in active["shrink50"]
                            for d in DEATHS:
                                if ek not in active[d]:
                                    active[d][ek] = {"t0": t, "skey": skey, "side": side,
                                                     "price": price, "size0": sizes[k - 1],
                                                     "base0": bv, "tier0": k, "dist0": dm,
                                                     "last_seen": t}
                            if fresh:
                                events.append((skey, t, side, price, sizes[k - 1], dm, k, r["sess"]))
            for side, sizes in (("bid", r["bs"]), ("ask", r["asz"])):
                for k in range(1, 6):
                    base[(side, k)].add(t, sizes[k - 1])
            # episode 維護
            for d in DEATHS:
                for ek in list(active[d]):
                    side, price = ek
                    prices = r["bp"] if side == "bid" else r["ap"]
                    sizes = r["bs"] if side == "bid" else r["asz"]
                    ep = active[d][ek]
                    if price in prices:
                        idx = prices.index(price)
                        ep["last_seen"] = t
                        sz = sizes[idx]
                        dead = ((d == "shrink50" and sz < 0.5 * ep["size0"]) or
                                (d == "shrink20" and sz < 0.2 * ep["size0"]) or
                                (d == "below_wall" and sz < N_WALL * ep["base0"]) or
                                (d == "level_gone" and False))
                        if dead:
                            active[d].pop(ek); ep["dur"] = t - ep["t0"]; ep["event"] = 1
                            eps[d].append(ep)
                    elif t - ep["last_seen"] > 2.0:
                        active[d].pop(ek)
                        ep["dur"] = ep["last_seen"] - ep["t0"]
                        ep["event"] = 1 if d == "level_gone" else 0
                        eps[d].append(ep)
        for d in DEATHS:
            for ek in list(active[d]):
                ep = active[d].pop(ek)
                ep["dur"] = ep["last_seen"] - ep["t0"]; ep["event"] = 0
                eps[d].append(ep)

    # 命中率／畫像
    per_day = {}
    for k in sorted(eval_n):
        per_day[k] = {"evaluable": eval_n[k], "tier_hits_N3": hit_n[k],
                      "hit_rate_pct": round(100.0 * hit_n[k] / (10 * eval_n[k]), 3)}
    payload["per_session_day"] = per_day
    print(json.dumps(per_day, ensure_ascii=False, indent=1))

    def qc_q(c: Counter, p: float):
        n = sum(c.values()); cum = 0; tgt = p * (n - 1)
        for kk in sorted(c):
            cum += c[kk]
            if cum > tgt:
                return float(kk)
        return float(max(c))
    payload["wall_dist_from_mid"] = {f"p{int(p*100)}": qc_q(dist_hist, p) for p in (.5, .9, .99, 1.0)}
    payload["wall_dist_from_mid"]["n"] = sum(dist_hist.values())
    payload["wall_dist_ge_12pt_pct"] = round(
        100.0 * sum(v for kk, v in dist_hist.items() if kk >= 12) / max(1, sum(dist_hist.values())), 4)
    payload["book_farthest_level_from_mid"] = {f"p{int(p*100)}": qc_q(far_book, p)
                                               for p in (.5, .9, .99, 1.0)}
    print("wall dist:", payload["wall_dist_from_mid"], "ge12pct", payload["wall_dist_ge_12pt_pct"])
    print("book farthest:", payload["book_farthest_level_from_mid"])

    # ---- R2 存活分析 --------------------------------------------------------
    surv = {}
    for d in DEATHS:
        E = eps[d]
        durs = np.array([e["dur"] for e in E]); ev = np.array([e["event"] for e in E])
        naive_med = float(np.median(durs))
        km, pts = km_median(durs, ev)
        surv[d] = {"n": len(E), "censored_pct": round(100.0 * (1 - ev.mean()), 1),
                   "naive_median_pooled_s": round(naive_med, 3),
                   "km_median_s": round(km, 3) if km == km else None,
                   "km_survival": pts,
                   "naive_gt10s_pct": round(100.0 * float((durs > 10).mean()), 2)}
        # 日夜分開
        for ss in ("day", "night"):
            sub = [e for e in E if e["skey"].endswith(ss)]
            if len(sub) >= 50:
                dd = np.array([e["dur"] for e in sub]); ee = np.array([e["event"] for e in sub])
                m2, p2 = km_median(dd, ee)
                surv[d][ss] = {"n": len(sub), "naive_median": round(float(np.median(dd)), 3),
                               "km_median": round(m2, 3) if m2 == m2 else None,
                               "km_S10s": p2["S(10s)"]}
        print(d, json.dumps(surv[d], ensure_ascii=False))
    payload["survival"] = surv

    # ---- R3 預測力（有對照組）----------------------------------------------
    HORIZ = (5, 15, 60, 300)
    rng = random.Random(20260820)
    pred = {}
    per_day_pred = defaultdict(dict)
    for skey, g in sorted(groups.items()):
        tt = np.array([r["t"] for r in g])
        mid = np.array([0.5 * (r["bp"][0] + r["ap"][0]) for r in g])
        ev = [e for e in events if e[0] == skey]
        if len(ev) < 100:
            continue
        et = np.array([e[1] for e in ev])
        esign = np.array([1.0 if e[2] == "bid" else -1.0 for e in ev])
        i0 = np.searchsorted(tt, et, side="left")
        i0 = np.clip(i0, 0, len(tt) - 1)
        # placebo：同 session-day 隨機時點，同樣本數、同 side 比例
        pidx = np.array(sorted(rng.sample(range(len(tt)), min(len(ev), len(tt)))))
        psign = esign[:len(pidx)].copy()
        rng.shuffle(psign)
        for h in HORIZ:
            j = np.searchsorted(tt, et + h, side="left")
            ok = j < len(tt)
            r_w = (mid[np.clip(j, 0, len(tt) - 1)] - mid[i0]) * esign
            r_w = r_w[ok]
            jp = np.searchsorted(tt, tt[pidx] + h, side="left")
            okp = jp < len(tt)
            r_p = (mid[np.clip(jp, 0, len(tt) - 1)] - mid[pidx]) * psign
            r_p = r_p[okp]
            if len(r_w) < 50 or len(r_p) < 50:
                continue
            per_day_pred[h][skey] = {
                "n_wall": int(len(r_w)), "wall_mean_pts": round(float(r_w.mean()), 4),
                "wall_dir_acc_pct": round(100.0 * float((r_w > 0).mean() + 0.5 * (r_w == 0).mean()), 2),
                "placebo_mean_pts": round(float(r_p.mean()), 4),
                "placebo_dir_acc_pct": round(100.0 * float((r_p > 0).mean() + 0.5 * (r_p == 0).mean()), 2),
                "diff_pts": round(float(r_w.mean() - r_p.mean()), 4),
            }
    for h in HORIZ:
        dd = per_day_pred[h]
        if not dd:
            continue
        diffs = [v["diff_pts"] for v in dd.values()]
        pred[f"h{h}s"] = {"per_session_day": dd,
                          "n_session_days": len(dd),
                          "mean_diff_pts": round(float(np.mean(diffs)), 4),
                          "sign_consistent_days": int(sum(1 for x in diffs if x > 0)),
                          "min_diff": round(min(diffs), 4), "max_diff": round(max(diffs), 4)}
        print(f"h={h}s  mean_diff={np.mean(diffs):+.4f} pts  days+={sum(1 for x in diffs if x>0)}/{len(diffs)}")
        for k, v in dd.items():
            print(f"   {k:<20} wall {v['wall_mean_pts']:+.4f} ({v['wall_dir_acc_pct']:.1f}%) "
                  f"placebo {v['placebo_mean_pts']:+.4f} ({v['placebo_dir_acc_pct']:.1f}%) "
                  f"diff {v['diff_pts']:+.4f}  n={v['n_wall']}")
    payload["prediction"] = pred
    payload["n_events"] = len(events)

    outp = Path(args.out)
    if not outp.is_absolute():
        outp = Path(__file__).resolve().parents[2] / outp
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("wrote", outp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
