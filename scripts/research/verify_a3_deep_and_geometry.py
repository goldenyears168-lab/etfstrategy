#!/usr/bin/env python3
"""對抗性複核 A3 第二輪：
 (a) 極端牆（絕對 >=10 / >=14 口）在 3-6 點外還有沒有效？A3 自承沒跑這個子樣本。
 (b) 幾何：五檔簿子到底看得到多遠？strategy_reach 的 0.003% 是視野極限還是市場事實。
 (c) wall / thin 兩組的實際口數平均，檢查『厚牆只比薄檔多 3 口』的說法。
 (d) 事件是否有差別性流失（wall vs thin 的 forward-window 丟棄率）。
"""
from __future__ import annotations
import json, sys
from collections import defaultdict
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).parent))
from verify_a3_wall_vs_obi import load, fwd, events, diff_by_cell, DAYS, NT  # noqa: E402

S_, stats = load(DAYS)
print("load", stats)

# ---------- (b) 幾何 ----------
geo = {}
tot = 0; anyfar = defaultdict(int)
t4d = []
for s, S in S_.items():
    mid = S["mid"]
    db = mid[:, None] - S["bp"]; da = S["ap"] - mid[:, None]
    tot += 2 * len(mid)
    for X in (6, 8, 10, 12, 24):
        anyfar[X] += int((db >= X).any(axis=1).sum() + (da >= X).any(axis=1).sum())
    t4d.append(np.concatenate([db[:, 4], da[:, 4]]))
t4d = np.concatenate(t4d)
geo["any_level_at_dist_ge_X_share (不管口數)"] = {X: anyfar[X] / tot for X in (6, 8, 10, 12, 24)}
geo["tier5_distance_from_mid_pt"] = {p: float(np.percentile(t4d, p)) for p in (50, 90, 99, 99.9, 100)}
print(json.dumps(geo, ensure_ascii=False, indent=1))

# ---------- (c)(d) 事件層 ----------
H, SP, APPR, BR = 30.0, 5.0, 2.0, 1.0
DEEP = (3.0, 6.0)
res = {"geometry": geo, "deep_extreme": {}, "size_levels": {}, "attrition": {}}

for mode, lo, hi in (("contact", None, None), ("deep", DEEP[0], DEEP[1])):
    for whi, wlo in ((6.0, 3.0), (10.0, 3.0), (14.0, 3.0)):
        allr = []; per = defaultdict(list)
        keep_n = drop_n = 0
        wsz = []; tsz = []
        for sess, S in sorted(S_.items()):
            idx0 = events(S, SP)
            ok, mn, mx, en = fwd(S, idx0, H)
            keep_n += int(ok.sum()); drop_n += int((~ok).sum())
            k = np.nonzero(ok)[0]; idx = idx0[k]; mn, mx, en = mn[k], mx[k], en[k]
            m0 = S["mid"][idx]
            for side in ("bid", "ask"):
                sgn = 1.0 if side == "bid" else -1.0
                P = (S["bp"] if side == "bid" else S["ap"])[idx, :]
                Z = (S["bs"] if side == "bid" else S["asz"])[idx, :]
                ext = mn if side == "bid" else mx
                for tk in range(NT):
                    dist = sgn * (m0 - P[:, tk])
                    sel = (dist > 0) & (dist <= APPR) if mode == "contact" else (dist >= lo) & (dist <= hi)
                    if not sel.any():
                        continue
                    sz = Z[:, tk]
                    grp = np.where(sz >= whi, "wall", np.where(sz <= wlo, "thin", "mid"))
                    pen = sgn * (P[:, tk] - ext)
                    dbin = np.round(dist * 2) / 2.0
                    for j in np.nonzero(sel)[0]:
                        r = dict(sess=sess, side=side, tier=tk, dbin=float(dbin[j]),
                                 grp=str(grp[j]), breach=float(pen[j] >= BR), size=float(sz[j]))
                        allr.append(r); per[sess].append(r)
                        if r["grp"] == "wall":
                            wsz.append(sz[j])
                        elif r["grp"] == "thin":
                            tsz.append(sz[j])
        mc = 20 if whi == 6 else (12 if whi == 10 else 8)
        key = ("sess", "side", "tier", "dbin")
        d = diff_by_cell(allr, key, "breach", mc)
        ps = [None if diff_by_cell(per[s], key, "breach", mc) is None
              else round(diff_by_cell(per[s], key, "breach", mc)["diff"], 4) for s in sorted(per)]
        lab = f"{mode}/wall>={whi:.0f}"
        res["deep_extreme"][lab] = {"pooled": d, "per_session": ps, "min_cell": mc}
        res["size_levels"][lab] = {"wall_mean_lots": float(np.mean(wsz)) if wsz else None,
                                   "wall_median": float(np.median(wsz)) if wsz else None,
                                   "wall_p90": float(np.percentile(wsz, 90)) if wsz else None,
                                   "thin_mean_lots": float(np.mean(tsz)) if tsz else None,
                                   "n_wall_raw": len(wsz), "n_thin_raw": len(tsz)}
        res["attrition"][lab] = {"events_kept": keep_n, "events_dropped": drop_n}
        neg = sum(1 for x in ps if x is not None and x < 0)
        if d:
            msg = (f"cells={d['cells']:>4} nW={d['n_wall']:>6} nT={d['n_thin']:>6} "
                   f"dP(breach)={d['diff']:+.4f} neg {neg}/{sum(x is not None for x in ps)} {ps}")
        else:
            msg = "無足夠 cell"
        print(f"{lab:<22} " + msg)
        print(f"{'':<22} wall 口數 mean={res['size_levels'][lab]['wall_mean_lots']} "
              f"med={res['size_levels'][lab]['wall_median']} p90={res['size_levels'][lab]['wall_p90']} | "
              f"thin mean={res['size_levels'][lab]['thin_mean_lots']}")

Path("/Users/jackm4/goldenstocks/reports/research/channel_lab/verify_a3_deep_and_geometry.json").write_text(
    json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print("wrote verify_a3_deep_and_geometry.json")
