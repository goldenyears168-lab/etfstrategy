#!/usr/bin/env python3
"""rev_b1 — 對抗性複核：B1「排在牆後面只在輸的分支成交」。

檢查三件事
1. 恆等式檢定：A(隊尾) 成交是否 **蘊含** C(隊頭) 成交？成交瞬間的
   (mid - fill_px) 對 A 是否 **恆負**、對 C 是否 **恆正**？若是，B1 報的
   「1.35 點差幾乎全發生在成交瞬間」就是定義推出來的算術，不是實證發現。
2. aligned EV 差的分解：C-A = P(C成交且A未成交) x E[aligned | 該子集]，
   而該子集在 back 模型下幾乎必然是「牆還撐著」→ 價格 >= 牆價 → 恆正。
3. 攻擊性階梯：把 B 換成 +1/+2/+3 tick（越來越貴、隊列越來越短）。
   若 EV 隨攻擊性單調上升，B>A 就不是「隊列位置」而是「這 2.2 天觸價後會反彈」。
"""
from __future__ import annotations

import bisect
import importlib.util
import json
import statistics as st
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path("/Users/jackm4/goldenstocks")
spec = importlib.util.spec_from_file_location(
    "wb1", ROOT / "scripts/research/wall_b1_queue_adverse_selection.py")
wb1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wb1)

HORIZ = [30, 60, 300]
HORIZON = 120.0


def gen_sim(ep, books, trades, bk_t, tr_t, offsets, *, horizon=HORIZON,
            cancel_model="back"):
    """offsets: list of (name, tick_offset_toward_mid, queue_mode)
    queue_mode: 'visible' 用該價位當下可見量; 'zero' 隊頭; 'wall' 用牆的量。"""
    side = ep["side"]
    is_buy = side == "bid"
    sign = 1.0 if is_buy else -1.0
    t0, t1 = ep["t"], ep["t"] + horizon
    wall_px = ep["price"]
    b0 = books[ep["i"]]
    pk, sk = ("bp", "bs") if is_buy else ("ap", "as")
    opp0 = b0["ap"][0] if is_buy else b0["bp"][0]

    def visible(px):
        for j in range(5):
            if b0[pk][j] == px:
                return float(b0[sk][j])
        return 0.0

    orders = {}
    for name, off, qmode in offsets:
        px = wall_px + off if is_buy else wall_px - off
        if (is_buy and px >= opp0) or ((not is_buy) and px <= opp0):
            continue  # marketable，跳過
        q = 0.0 if qmode == "zero" else (float(ep["wall_size"]) if qmode == "wall"
                                         else visible(px))
        orders[name] = {"px": px, "q": q, "q0": q, "cum": 0.0, "fill_t": None,
                        "through": False}

    lo = bisect.bisect_left(tr_t, t0)
    hi = bisect.bisect_right(tr_t, t1)
    consume = -1 if is_buy else 1
    wall_consumed = 0.0
    wall_broken_t = None
    for ti in range(lo, hi):
        tr = trades[ti]
        px, tt = tr["px"], tr["t"]
        for o in orders.values():
            if o["fill_t"] is not None:
                continue
            through = (px < o["px"]) if is_buy else (px > o["px"])
            if through:
                o["fill_t"] = tt
                o["through"] = True
                continue
            if px == o["px"] and tr["side"] == consume:
                o["cum"] += tr["sz"]
                if o["cum"] >= o["q"] + 1.0:
                    o["fill_t"] = tt
        if wall_broken_t is None:
            if (px < wall_px) if is_buy else (px > wall_px):
                wall_broken_t = tt
            elif px == wall_px and tr["side"] == consume:
                wall_consumed += tr["sz"]
                if wall_consumed >= ep["wall_size"]:
                    wall_broken_t = tt

    out = {"wall_broken": wall_broken_t is not None, "orders": {}}
    for name, o in orders.items():
        rec = {"px": o["px"], "q0": o["q0"], "filled": o["fill_t"] is not None,
               "through": o["through"], "fill_t": o["fill_t"], "sign": sign}
        rec["aligned"] = {}
        for h in HORIZ:
            tgt = t0 + h
            i = bisect.bisect_right(bk_t, tgt) - 1
            if i < 0 or tgt - bk_t[i] > 15.0:
                rec["aligned"][h] = None
            elif o["fill_t"] is None or o["fill_t"] > tgt:
                rec["aligned"][h] = 0.0
            else:
                rec["aligned"][h] = sign * (books[i]["mid"] - o["px"])
        rec["markout"] = {}
        rec["gap_at_fill"] = None
        if o["fill_t"] is not None:
            i0 = bisect.bisect_right(bk_t, o["fill_t"]) - 1
            mid0 = books[i0]["mid"] if i0 >= 0 else None
            if mid0 is not None:
                rec["gap_at_fill"] = sign * (mid0 - o["px"])
            for h in HORIZ:
                tgt = o["fill_t"] + h
                i = bisect.bisect_right(bk_t, tgt) - 1
                rec["markout"][h] = (None if i < 0 or tgt - bk_t[i] > 15.0
                                     else sign * (books[i]["mid"] - o["px"]))
        out["orders"][name] = rec
    return out


def main() -> int:
    books, bstats = wb1.load_books(wb1.DAYS)
    trades, tstats = wb1.load_trades(wb1.DAYS)
    bk_t = [b["t"] for b in books]
    tr_t = [t["t"] for t in trades]
    tr_lo, tr_hi = tr_t[0], tr_t[-1]
    print("books", len(books), bstats, "trades", len(trades), tstats)

    eps, tiers = wb1.detect_walls(books, mult_base=3.0, mult_neigh=2.0, min_lots=15,
                                  halflife=2000.0, warmup=5000, spacing=120.0,
                                  max_dist=12.0)
    n_all = len(eps)
    eps = [e for e in eps if tr_lo <= e["t"] <= tr_hi - HORIZON]
    print(f"episodes {n_all} -> {len(eps)}")

    # 距離上限有沒有在綁？
    cap = sum(1 for e in eps if e["dist_pts"] >= 9.0)
    print("episodes with dist>=9pt:", cap)

    offsets = [("A_wall_behind", 0, "wall"), ("C_wall_front", 0, "zero"),
               ("B1_inside", 1, "visible"), ("B2_inside", 2, "visible"),
               ("B3_inside", 3, "visible"), ("O1_outside", -1, "visible"),
               ("O1_outside_front", -1, "zero")]
    names = [o[0] for o in offsets]

    recs = []
    for ep in eps:
        s = gen_sim(ep, books, trades, bk_t, tr_t, offsets)
        if "B1_inside" not in s["orders"]:
            continue  # 與原腳本一致：B marketable 就剔除整個 episode
        recs.append({"ep": ep, "sim": s})
    print("usable episodes", len(recs))

    def m(xs):
        return float(st.mean(xs)) if xs else None

    # ---------- 1. 恆等式檢定 ----------
    impl_ok = impl_bad = 0
    gapA = []
    gapC = []
    for r in recs:
        o = r["sim"]["orders"]
        a, c = o["A_wall_behind"], o["C_wall_front"]
        if a["filled"]:
            if c["filled"]:
                impl_ok += 1
            else:
                impl_bad += 1
        if a["filled"] and a["gap_at_fill"] is not None:
            gapA.append(a["gap_at_fill"])
        if c["filled"] and c["gap_at_fill"] is not None:
            gapC.append(c["gap_at_fill"])
    ident = {
        "A_filled_implies_C_filled": {"ok": impl_ok, "violations": impl_bad},
        "A_gap_at_fill": {"n": len(gapA), "mean": m(gapA),
                          "frac_le_0": sum(1 for x in gapA if x <= 0) / len(gapA),
                          "frac_lt_0": sum(1 for x in gapA if x < 0) / len(gapA),
                          "max": max(gapA)},
        "C_gap_at_fill": {"n": len(gapC), "mean": m(gapC),
                          "frac_ge_0": sum(1 for x in gapC if x >= 0) / len(gapC),
                          "frac_gt_0": sum(1 for x in gapC if x > 0) / len(gapC),
                          "min": min(gapC)},
    }
    print("\n[1] identity check:", json.dumps(ident, ensure_ascii=False, indent=1))

    # ---------- 2. aligned 差的分解 ----------
    decomp = {}
    for h in HORIZ:
        both = neither = conly = aonly = 0
        conly_vals = []
        conly_neg = 0
        for r in recs:
            o = r["sim"]["orders"]
            a, c = o["A_wall_behind"], o["C_wall_front"]
            av, cv = a["aligned"][h], c["aligned"][h]
            if av is None or cv is None:
                continue
            af = a["filled"] and a["fill_t"] <= r["ep"]["t"] + h
            cf = c["filled"] and c["fill_t"] <= r["ep"]["t"] + h
            if af and cf:
                both += 1
            elif cf and not af:
                conly += 1
                conly_vals.append(cv)
                if cv < 0:
                    conly_neg += 1
            elif af and not cf:
                aonly += 1
            else:
                neither += 1
        n = both + neither + conly + aonly
        decomp[h] = {
            "n": n, "both_filled": both, "C_only": conly, "A_only": aonly,
            "neither": neither,
            "P_C_only": conly / n if n else None,
            "E_aligned_given_C_only": m(conly_vals),
            "frac_negative_in_C_only": conly_neg / conly if conly else None,
            "implied_gap_C_minus_A": (conly / n) * m(conly_vals) if conly else None,
        }
    print("\n[2] aligned decomposition:", json.dumps(decomp, ensure_ascii=False, indent=1))

    # ---------- 3. 攻擊性階梯 ----------
    ladder = {}
    for nm in names:
        rows = [r["sim"]["orders"][nm] for r in recs if nm in r["sim"]["orders"]]
        if not rows:
            continue
        filled = [x for x in rows if x["filled"]]
        d = {"n": len(rows), "fill_rate": len(filled) / len(rows),
             "median_q0": float(st.median([x["q0"] for x in rows])),
             "through_share_of_fills": (sum(1 for x in filled if x["through"]) / len(filled))
             if filled else None,
             "gap_at_fill_mean": m([x["gap_at_fill"] for x in filled
                                    if x["gap_at_fill"] is not None])}
        for h in HORIZ:
            al = [x["aligned"][h] for x in rows if x["aligned"][h] is not None]
            mk = [x["markout"][h] for x in filled if x["markout"].get(h) is not None]
            d[f"aligned_ev_{h}"] = m(al)
            d[f"markout_{h}"] = m(mk)
        ladder[nm] = d
    print("\n[3] aggressiveness ladder")
    for nm, d in ladder.items():
        print(f"  {nm:<18} fill={d['fill_rate']:.3f} q0={d['median_q0']:.0f} "
              f"gap@fill={d['gap_at_fill_mean']:+.3f} "
              f"aligned60={d['aligned_ev_60']:+.3f} mk60={d['markout_60']:+.3f}")

    # ---------- 4. 分日 / 分 session 的階梯 ----------
    by_cell = defaultdict(lambda: defaultdict(list))
    for r in recs:
        key = f"{r['ep']['date']}|{r['ep']['sess']}"
        for nm, o in r["sim"]["orders"].items():
            v = o["aligned"][60]
            if v is not None:
                by_cell[key][nm].append(v)
    cells = {}
    for k, dd in sorted(by_cell.items()):
        cells[k] = {"n": len(dd.get("A_wall_behind", []))}
        for nm in names:
            if dd.get(nm):
                cells[k][nm] = round(m(dd[nm]), 3)
    print("\n[4] per day|session aligned EV 60s")
    print(json.dumps(cells, ensure_ascii=False, indent=1))

    # ---------- 5. session 開盤前 5 分鐘的 episode 佔比（EWMA 基線殘留） ----------
    opens = Counter()
    for r in recs:
        import datetime as dt
        t = dt.datetime.fromtimestamp(r["ep"]["t"], tz=wb1.TZ)
        hm = t.hour * 60 + t.minute
        if 8 * 60 + 45 <= hm < 8 * 60 + 50 or 15 * 60 <= hm < 15 * 60 + 5:
            opens["first5min"] += 1
        else:
            opens["rest"] += 1
    print("\n[5] episodes in first 5 min of a session:", dict(opens))

    # ---------- 6. 貢獻集中度 + B vs A 分解 + 逐日 jackknife ----------
    per = []
    for r in recs:
        row = {"date": r["ep"]["date"], "sess": r["ep"]["sess"], "hour": r["ep"]["hour"],
               "t": r["ep"]["t"],
               "dist": r["ep"]["dist_pts"], "wall": r["ep"]["wall_size"]}
        for nm, o in r["sim"]["orders"].items():
            row[nm] = o["aligned"][60]
            row[nm + "_fill"] = bool(o["filled"] and o["fill_t"] <= r["ep"]["t"] + 60)
        per.append(row)
    def gapstats(x, y):
        d = [(p[y] - p[x]) for p in per if p.get(x) is not None and p.get(y) is not None]
        d_sorted = sorted(d, reverse=True)
        tot = sum(d)
        n = len(d)
        return {"n": n, "mean": tot / n,
                "top1_share": d_sorted[0] / tot if tot else None,
                "top5_share": sum(d_sorted[:5]) / tot if tot else None,
                "top10_share": sum(d_sorted[:10]) / tot if tot else None,
                "top20_share": sum(d_sorted[:20]) / tot if tot else None,
                "n_nonzero": sum(1 for v in d if abs(v) > 1e-9),
                "n_negative": sum(1 for v in d if v < -1e-9),
                "median": float(st.median(d))}
    conc = {"C_minus_A": gapstats("A_wall_behind", "C_wall_front"),
            "B1_minus_A": gapstats("A_wall_behind", "B1_inside")}
    # B vs A 四象限
    quad = Counter()
    quad_val = defaultdict(list)
    for p in per:
        if p.get("A_wall_behind") is None or p.get("B1_inside") is None:
            continue
        k = ("B" if p["B1_inside_fill"] else "-") + ("A" if p["A_wall_behind_fill"] else "-")
        quad[k] += 1
        quad_val[k].append(p["B1_inside"] - p["A_wall_behind"])
    bva = {k: {"n": quad[k], "mean_diff": m(quad_val[k]),
               "contrib": sum(quad_val[k]) / len(per)} for k in quad}
    # 逐日 jackknife（丟掉一天）
    jack = {}
    for drop in sorted({p["date"] for p in per}):
        sub = [p for p in per if p["date"] != drop]
        jack[f"drop_{drop}"] = {
            "n": len(sub),
            "C_minus_A": m([p["C_wall_front"] - p["A_wall_behind"] for p in sub
                            if p["C_wall_front"] is not None and p["A_wall_behind"] is not None]),
            "B1_minus_A": m([p["B1_inside"] - p["A_wall_behind"] for p in sub
                             if p["B1_inside"] is not None and p["A_wall_behind"] is not None]),
        }
    print("\n[6] concentration:", json.dumps(conc, ensure_ascii=False, indent=1))
    print("[6] B vs A quadrants:", json.dumps(bva, ensure_ascii=False, indent=1))
    print("[6] leave-one-day-out:", json.dumps(jack, ensure_ascii=False, indent=1))

    out = {
        "per_episode": per, "concentration": conc, "B_vs_A_quadrants": bva, "leave_one_day_out": jack,
        "n_episodes": len(recs),
        "identity_check": ident,
        "aligned_decomposition": decomp,
        "ladder": ladder,
        "per_day_session_aligned60": cells,
        "session_open_concentration": dict(opens),
        "episodes_dist_ge_9pt": cap,
    }
    p = ROOT / "reports/research/channel_lab/rev_b1_wall_queue_refute.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("\nwrote", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
