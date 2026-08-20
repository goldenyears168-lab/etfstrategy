#!/usr/bin/env python3
"""對抗性複核 A3：牆的『擋價』效應是不是只是已知的 OBI（盤口失衡）換個名字？

獨立重寫載入與檢定，並加三個 A3 沒做的控制：
  1. 固定絕對門檻（wall>=WHI, thin<=WLO），完全不用 session 分位 → 徹底消除
     『分組門檻用了 t0 之後資料』的疑慮。
  2. **真正的 OBI 控制**：把 OBI=(bid1-ask1)/(bid1+ask1) 分箱進 cell key。
     A3 只固定對側量體再掃自側量體，那不是控制 OBI，而是直接在造 OBI 變異。
  3. 對稱門檻 placebo：breach 與 away 用同一個位移門檻（dist+breach_pt），
     A3 的 breach 需要走 dist+1 點、away 只要 1 點，兩者不可直接相減。
"""
from __future__ import annotations

import argparse, json, math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np

TZ = timezone(timedelta(hours=8))
DAYS = ["2026-08-14", "2026-08-15", "2026-08-17", "2026-08-18", "2026-08-19"]
NT = 5
GAP_MAX = 60.0
END_TOL = 15.0


def load(days):
    stats = Counter(); raw = defaultdict(list)
    d = Path.home() / "goldenstocks-data" / "cache" / "tmf_books"
    for day in days:
        p = d / f"tmf_books_{day}.jsonl"
        if not p.exists():
            continue
        for line in p.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            stats["rows"] += 1
            r = json.loads(line)
            b, a = r.get("bids") or [], r.get("asks") or []
            if len(b) < NT or len(a) < NT:
                stats["short"] += 1; continue
            w = datetime.fromisoformat(str(r["ts"])).astimezone(TZ)
            bt = datetime.fromtimestamp(float(r["book_time"]) / 1e6, tz=TZ)
            stale = bool(r["stale"]) if "stale" in r else (w - bt).total_seconds() > 5.0
            if stale:
                stats["zombie"] += 1; continue
            qt = str(r.get("quote_type") or "")
            if qt == "FUTURE":
                s = f"day/{bt.date()}"
            elif qt == "FUTURE_AH":
                s = f"night/{bt.date() if bt.hour >= 12 else (bt - timedelta(days=1)).date()}"
            else:
                stats["badqt"] += 1; continue
            stats["live"] += 1
            raw[s].append((bt.timestamp(),
                           [float(x["price"]) for x in b[:NT]], [float(x["size"]) for x in b[:NT]],
                           [float(x["price"]) for x in a[:NT]], [float(x["size"]) for x in a[:NT]]))
    out = {}
    for s, rows in raw.items():
        rows.sort(key=lambda x: x[0])
        ded = []
        for r in rows:
            if ded and r[0] == ded[-1][0]:
                ded[-1] = r
            else:
                ded.append(r)
        if len(ded) < 500:
            continue
        t = np.array([r[0] for r in ded])
        out[s] = dict(t=t,
                      bp=np.array([r[1] for r in ded]), bs=np.array([r[2] for r in ded]),
                      ap=np.array([r[3] for r in ded]), asz=np.array([r[4] for r in ded]))
        out[s]["mid"] = (out[s]["bp"][:, 0] + out[s]["ap"][:, 0]) / 2.0
        out[s]["gapfix"] = np.concatenate([[0], np.cumsum(np.diff(t) > GAP_MAX)])
    return out, dict(stats)


def fwd(S, idx, H):
    t, mid = S["t"], S["mid"]
    t0 = t[idx]
    j0 = np.searchsorted(t, t0, side="right"); j1 = np.searchsorted(t, t0 + H, side="right")
    ok = j1 > j0
    last = t[np.clip(j1 - 1, 0, len(t) - 1)]
    ok &= (t0 + H - last) <= END_TOL
    gf = S["gapfix"]; ok &= (gf[np.clip(j1 - 1, 0, len(t) - 1)] - gf[idx]) == 0
    mn = np.full(len(idx), np.nan); mx = np.full(len(idx), np.nan); en = np.full(len(idx), np.nan)
    for k in np.nonzero(ok)[0]:
        seg = mid[j0[k]:j1[k]]
        mn[k] = seg.min(); mx[k] = seg.max(); en[k] = seg[-1]
    return ok, mn, mx, en


def events(S, spacing):
    t = S["t"]; keep = [0]; last = t[0]
    for i in range(1, len(t)):
        if t[i] - last >= spacing:
            keep.append(i); last = t[i]
    return np.array(keep, dtype=np.int64)


def diff_by_cell(recs, key, field, min_cell):
    cells = defaultdict(lambda: {"wall": [], "thin": []})
    for r in recs:
        if r["grp"] in ("wall", "thin"):
            cells[tuple(r[f] for f in key)][r["grp"]].append(r[field])
    num = den = 0.0; nc = 0; nw = nt = 0
    for g in cells.values():
        if len(g["wall"]) < min_cell or len(g["thin"]) < min_cell:
            continue
        w = min(len(g["wall"]), len(g["thin"]))
        num += w * (float(np.mean(g["wall"])) - float(np.mean(g["thin"]))); den += w
        nc += 1; nw += len(g["wall"]); nt += len(g["thin"])
    if den == 0:
        return None
    return {"cells": nc, "n_wall": nw, "n_thin": nt, "diff": num / den}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=float, default=30.0)
    ap.add_argument("--spacing", type=float, default=5.0)
    ap.add_argument("--approach", type=float, default=2.0)
    ap.add_argument("--breach-pt", type=float, default=1.0)
    ap.add_argument("--wall-abs", type=float, default=6.0)
    ap.add_argument("--thin-abs", type=float, default=3.0)
    ap.add_argument("--min-cell", type=int, default=20)
    ap.add_argument("--out", default="/Users/jackm4/goldenstocks/reports/research/channel_lab/verify_a3_wall_vs_obi.json")
    A = ap.parse_args()

    S_, stats = load(DAYS)
    print("load", stats, {k: len(v["t"]) for k, v in sorted(S_.items())})

    allrecs = []; per_sess_recs = {}
    tier_hist = Counter()
    for sess, S in sorted(S_.items()):
        idx = events(S, A.spacing)
        ok, mn, mx, en = fwd(S, idx, A.horizon)
        k = np.nonzero(ok)[0]; idx = idx[k]; mn, mx, en = mn[k], mx[k], en[k]
        m0 = S["mid"][idx]
        b1 = S["bs"][idx, 0]; a1 = S["asz"][idx, 0]
        obi = (b1 - a1) / np.maximum(b1 + a1, 1e-9)
        tot = b1 + a1
        recs = []
        for side in ("bid", "ask"):
            sgn = 1.0 if side == "bid" else -1.0
            P = (S["bp"] if side == "bid" else S["ap"])[idx, :]
            Z = (S["bs"] if side == "bid" else S["asz"])[idx, :]
            ext = mn if side == "bid" else mx
            oext = mx if side == "bid" else mn
            sobi = obi if side == "bid" else -obi          # 自側為正的失衡
            for tk in range(NT):
                dist = sgn * (m0 - P[:, tk])
                sel = (dist > 0) & (dist <= A.approach)
                if not sel.any():
                    continue
                sz = Z[:, tk]
                grp = np.where(sz >= A.wall_abs, "wall", np.where(sz <= A.thin_abs, "thin", "mid"))
                pen = sgn * (P[:, tk] - ext)               # >0 穿過
                mv = sgn * (en - m0)
                away_raw = sgn * (oext - m0)
                dbin = np.round(dist * 2) / 2.0
                for j in np.nonzero(sel)[0]:
                    tier_hist[tk] += 1
                    recs.append(dict(
                        sess=sess, side=side, tier=tk, dbin=float(dbin[j]), grp=str(grp[j]),
                        # 對稱門檻：兩邊都要求走 dist+breach_pt 點
                        breach=float(pen[j] >= A.breach_pt),
                        away_sym=float(away_raw[j] >= dist[j] + A.breach_pt),
                        away_asym=float(away_raw[j] >= A.breach_pt),
                        mv=float(mv[j]),
                        obibin=int(np.digitize(sobi[j], [-0.34, -0.11, 0.11, 0.34])),
                        depthbin=int(np.digitize(tot[j], np.quantile(tot, [0.33, 0.67]))),
                        size=float(sz[j]),
                    ))
        per_sess_recs[sess] = recs
        allrecs.extend(recs)

    print("contact tier 分布", dict(tier_hist))
    base = ("sess", "side", "tier", "dbin")
    res = {"params": vars(A), "load": stats, "tier_hist": dict(tier_hist), "blocks": {}}
    for label, key in (
        ("A_base (=A3 主結果, 但用固定絕對門檻)", base),
        ("B_true_OBI_controlled (加 OBI 五分箱)", base + ("obibin",)),
        ("C_OBI+total_depth_controlled", base + ("obibin", "depthbin")),
    ):
        for fld in ("breach", "away_sym", "away_asym", "mv"):
            r = diff_by_cell(allrecs, key, fld, A.min_cell)
            per = []
            for s in sorted(per_sess_recs):
                rr = diff_by_cell(per_sess_recs[s], key, fld, A.min_cell)
                per.append(None if rr is None else round(rr["diff"], 4))
            res["blocks"].setdefault(label, {})[fld] = {"pooled": r, "per_session": per}
            if r:
                sgn_cnt = sum(1 for x in per if x is not None and x < 0)
                print(f"{label:<44} {fld:<10} cells={r['cells']:>4} nW={r['n_wall']:>6} "
                      f"nT={r['n_thin']:>6} diff={r['diff']:+.4f}  neg {sgn_cnt}/{sum(x is not None for x in per)}  {per}")
            else:
                print(f"{label:<44} {fld:<10} 無足夠 cell")

    # 純 OBI 效應（不看牆）：把 OBI 高低當分組，cell 只控 session/dbin
    obir = []
    for r in allrecs:
        g = "wall" if r["obibin"] >= 3 else ("thin" if r["obibin"] <= 1 else "mid")
        obir.append({**r, "grp": g})
    for fld in ("breach", "away_asym"):
        rr = diff_by_cell(obir, base, fld, A.min_cell)
        res["blocks"].setdefault("D_pure_OBI_no_wall", {})[fld] = rr
        print(f"{'D_pure_OBI_no_wall':<44} {fld:<10} {rr}")

    Path(A.out).write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("wrote", A.out)


if __name__ == "__main__":
    main()
