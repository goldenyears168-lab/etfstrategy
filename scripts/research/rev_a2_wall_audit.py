#!/usr/bin/env python3
"""對抗性複核 A2：牆被吃 vs 被撤。

四個獨立攻擊：
 V0  原始規格重現（TOL=+1s、無方向、全區間流量會計）
 V1  TOL=0（拿掉結束後 1 秒的未來資料）
 V2  方向感知（bid 牆只認賣方主動成交；ask 牆只認買方主動成交）+ TOL=0
 V3  安慰劑：把成交 tape 整體時移 +5s / +60s 後重算（測「只是在量該價位的環境成交量」）
 V4  終端專用指標：只看「最後消失那一刻」的 final_size 與其成交，
     完全不受 anchor 時點影響 → 檢驗「階梯只是 anchor 時點不對稱」的假說
"""
from __future__ import annotations
import bisect, json, pickle, sys
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))
TZ = timezone(timedelta(hours=8))
MAX_AGE = 5.0
BASELINE_N, BASELINE_MIN, MIN_ABS = 500, 50, 10
RATIO_BANDS = ((0.75,1.35,"band1"),(1.35,2.0,"band2"),(2.0,3.0,"band3"),(3.0,5.0,"band4"),(5.0,1e9,"band5"))
GAP_SEC, TOL = 30.0, 1.0
SHIFTS = (5.0, 60.0)
ROOT = Path.home() / "goldenstocks-data" / "cache"


def load_books(day):
    p = ROOT / "tmf_books" / f"tmf_books_{day}.jsonl"
    st = Counter(); streams = {"day": [], "night": []}
    if not p.exists(): return streams, st
    for line in p.open():
        line = line.strip()
        if not line: continue
        st["rows"] += 1
        r = json.loads(line)
        b, a = r.get("bids") or [], r.get("asks") or []
        if len(b) < 5 or len(a) < 5: st["short"] += 1; continue
        wall = datetime.fromisoformat(r["ts"]).timestamp(); bt = r["book_time"]/1e6
        stale = bool(r["stale"]) if "stale" in r else (wall - bt) > MAX_AGE
        if stale: st["zombie"] += 1; continue
        sess = "night" if str(r.get("quote_type","")).endswith("_AH") else "day"
        streams[sess].append({"t": bt,
            "bp":[int(x["price"]) for x in b], "bs":[int(x["size"]) for x in b],
            "ap":[int(x["price"]) for x in a], "asz":[int(x["size"]) for x in a]})
        st[f"live_{sess}"] += 1
    for s, rows in streams.items():
        rows.sort(key=lambda r: r["t"]); d = []
        for r in rows:
            if d and r["t"] == d[-1]["t"]: d[-1] = r; continue
            d.append(r)
        streams[s] = d
    return streams, st


def load_trades(day):
    p = ROOT / "tmf_trades" / f"tmf_trades_{day}.jsonl"
    st = Counter(); raw = {"day": {}, "night": {}}
    if not p.exists(): return raw, st
    for line in p.open():
        line = line.strip()
        if not line: continue
        st["rows"] += 1
        r = json.loads(line)
        wall = datetime.fromisoformat(r["ts"]).timestamp(); tt = r["trade_time"]/1e6
        if (wall - tt) > MAX_AGE: st["zombie"] += 1; continue
        price, size = int(r["price"]), int(r["size"])
        dt = datetime.fromtimestamp(tt, tz=TZ); sec = dt.hour*3600 + dt.minute*60
        sess = "day" if 8*3600+40*60 <= sec <= 13*3600+50*60 else "night"
        bid, ask = r.get("bid"), r.get("ask")
        aggr = 0
        if isinstance(bid,(int,float)) and price <= bid: aggr = -1
        elif isinstance(ask,(int,float)) and price >= ask: aggr = +1
        raw[sess].setdefault(price, []).append((tt, size, aggr))
        st[f"live_{sess}"] += 1
    for s, d in raw.items():
        for pr in d: d[pr].sort()
    return raw, st


class TIdx:
    """支援方向與時移的價位成交索引。"""
    def __init__(self, per_price, shift=0.0):
        self.d = {}
        for price, lst in per_price.items():
            times = [x[0] + shift for x in lst]
            cum = [0]; cbuy = [0]; csell = [0]
            for _, s, a in lst:
                cum.append(cum[-1]+s)
                cbuy.append(cbuy[-1] + (s if a == +1 else 0))
                csell.append(csell[-1] + (s if a == -1 else 0))
            self.d[price] = (times, cum, cbuy, csell)
    def vol(self, price, t0, t1, direction=0):
        e = self.d.get(price)
        if e is None or t1 <= t0: return 0
        times, cum, cbuy, csell = e
        i0 = bisect.bisect_right(times, t0); i1 = bisect.bisect_right(times, t1)
        arr = cum if direction == 0 else (cbuy if direction == +1 else csell)
        return arr[i1] - arr[i0]


def band_of(ratio):
    for lo, hi, tag in RATIO_BANDS:
        if lo <= ratio < hi: return tag
    return None


def run_stream(snaps, ti, plc, day, sess, eps):
    baseline = defaultdict(lambda: deque(maxlen=BASELINE_N))
    active = {}; prev_t = None; first = True

    def close(state, fate, end_t, final_size):
        for tag, a in state["anchors"].items():
            rec = dict(a); rec.update(day=day, sess=sess, side=state["side"], price=state["price"],
                                      tag=tag, fate=fate, end_t=end_t, final_size=final_size,
                                      life=end_t - a["anchor_t"])
            if fate == "REMOVED" and final_size > 0:
                p = state["price"]; lt = a["last_t"]; dirn = -1 if state["side"] == "bid" else +1
                v1 = ti.vol(p, lt, end_t + TOL); v0 = ti.vol(p, lt, end_t)
                vd = ti.vol(p, lt, end_t, dirn)
                rec["removed"] += final_size
                rec["ta1"] += min(v1, final_size); rec["ta0"] += min(v0, final_size)
                rec["tad"] += min(vd, final_size)
                for k, pi in plc.items():
                    rec[f"tp{k:g}"] += min(pi.vol(p, lt, end_t), final_size)
                rec["term_v0"] = v0; rec["term_v1"] = v1; rec["term_vd"] = vd
                rec["term_vp"] = {f"{k:g}": plc[k].vol(p, lt, end_t) for k in plc}
            else:
                rec["term_v0"] = rec["term_v1"] = rec["term_vd"] = 0
                rec["term_vp"] = {f"{k:g}": 0 for k in plc}
            eps.append(rec)

    for snap in snaps:
        t = snap["t"]
        if prev_t is not None and (t - prev_t) > GAP_SEC:
            for s in active.values(): close(s, "TRUNCATED", prev_t, s.get("size", 0))
            active.clear(); first = True
        mid = (snap["bp"][0] + snap["ap"][0]) / 2.0
        seen = set()
        for side, pk, sk in (("bid","bp","bs"), ("ask","ap","asz")):
            prices, sizes = snap[pk], snap[sk]
            dirn = -1 if side == "bid" else +1
            for i in range(5):
                p, s = prices[i], sizes[i]
                key = (side, p); seen.add(key)
                base = baseline[(side, i)]
                med = None
                if len(base) >= BASELINE_MIN:
                    sb = sorted(base); med = sb[len(sb)//2]
                base.append(s)
                st_ = active.get(key)
                if st_ is None:
                    st_ = {"side": side, "price": p, "size": s, "t": t, "t0": t, "nobs": 0, "anchors": {}}
                    active[key] = st_
                else:
                    delta = s - st_["size"]
                    if st_["anchors"]:
                        rem = max(0, -delta)
                        if rem > 0:
                            v = ti.vol(p, st_["t"], t); vd = ti.vol(p, st_["t"], t, dirn)
                            pv = {k: plc[k].vol(p, st_["t"], t) for k in plc}
                            for a in st_["anchors"].values():
                                a["removed"] += rem
                                a["ta1"] += min(v, rem); a["ta0"] += min(v, rem)
                                a["tad"] += min(vd, rem)
                                for k in plc: a[f"tp{k:g}"] += min(pv[k], rem)
                                a["nint"] += 1
                        for a in st_["anchors"].values():
                            a["last_t"] = t
                    st_["size"] = s; st_["t"] = t
                st_["nobs"] += 1
                if med and med > 0:
                    ratio = s / med
                    tag = band_of(ratio) if s >= MIN_ABS else None
                    tags = []
                    if tag and tag not in st_["anchors"]: tags.append((tag, ratio))
                    if 0.75 <= ratio <= 1.35 and "ctrl" not in st_["anchors"]: tags.append(("ctrl", ratio))
                    for tg, rv in tags:
                        a = {"anchor_t": t, "last_t": t, "anchor_size": s, "level": i+1,
                             "dist": (mid-p) if side=="bid" else (p-mid), "ratio": rv,
                             "age": t - st_["t0"], "removed": 0, "ta1": 0, "ta0": 0, "tad": 0,
                             "nint": 0}
                        for k in plc: a[f"tp{k:g}"] = 0
                        st_["anchors"][tg] = a
                st_["size"] = s; st_["t"] = t
        if not first:
            for key in [k for k in active if k not in seen]:
                st_ = active.pop(key); side, p = key
                if side == "bid":
                    fate = "OUT_OF_VIEW" if p < snap["bp"][4] else "REMOVED"
                else:
                    fate = "OUT_OF_VIEW" if p > snap["ap"][4] else "REMOVED"
                if st_["anchors"]: close(st_, fate, t, st_["size"])
        first = False; prev_t = t
    for s in active.values():
        if s["anchors"]: close(s, "TRUNCATED", prev_t or 0.0, s["size"])


def main():
    days = sys.argv[1:] or ["2026-08-17", "2026-08-18", "2026-08-19"]
    eps = []; stats = {}
    for day in days:
        books, bst = load_books(day); trades, tst = load_trades(day)
        for sess in ("day", "night"):
            snaps = books.get(sess) or []
            if not snaps or not trades.get(sess): continue
            ti = TIdx(trades[sess])
            plc = {sh: TIdx(trades[sess], shift=sh) for sh in SHIFTS}
            run_stream(snaps, ti, plc, day, sess, eps)
        stats[day] = {"books": dict(bst), "trades": dict(tst)}
        print(f"[{day}] eps={len(eps)}", flush=True)
    out = Path("/Users/jackm4/goldenstocks/reports/research/channel_lab/rev_a2_episodes.pkl")
    with out.open("wb") as f: pickle.dump({"eps": eps, "stats": stats}, f, protocol=4)
    print("wrote", out, len(eps))


if __name__ == "__main__":
    main()
