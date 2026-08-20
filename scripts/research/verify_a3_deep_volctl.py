#!/usr/bin/env python3
"""A3 遠牆(3-6點)檢定：改用固定絕對門檻 + 過去60秒波動控制，看 A3 的『≈0』是否成立。"""
from __future__ import annotations
import json, sys
from collections import defaultdict
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).parent))
from verify_a3_wall_vs_obi import load, fwd, events, diff_by_cell, DAYS, NT  # noqa: E402

S_, _ = load(DAYS)
H, SP, BR = 30.0, 5.0, 1.0
out = {}
for mode, lo, hi in (("deep", 3.0, 6.0), ("contact", None, None)):
    for whi in (6.0, 10.0):
        allr = []; per = defaultdict(list)
        for sess, S in sorted(S_.items()):
            idx0 = events(S, SP)
            ok, mn, mx, en = fwd(S, idx0, H)
            k = np.nonzero(ok)[0]; idx = idx0[k]; mn, mx = mn[k], mx[k]
            m0 = S["mid"][idx]
            t, mid = S["t"], S["mid"]
            a0 = np.searchsorted(t, t[idx] - 60.0, side="left")
            pv = np.array([mid[a:b + 1].max() - mid[a:b + 1].min() if b + 1 - a >= 2 else np.nan
                           for a, b in zip(a0, idx)])
            pvb = np.digitize(np.nan_to_num(pv, nan=-1), [np.nanquantile(pv, .25), np.nanquantile(pv, .5), np.nanquantile(pv, .75)])
            for side in ("bid", "ask"):
                sgn = 1.0 if side == "bid" else -1.0
                P = (S["bp"] if side == "bid" else S["ap"])[idx, :]
                Z = (S["bs"] if side == "bid" else S["asz"])[idx, :]
                ext = mn if side == "bid" else mx
                for tk in range(NT):
                    dist = sgn * (m0 - P[:, tk])
                    sel = (dist > 0) & (dist <= 2.0) if mode == "contact" else (dist >= lo) & (dist <= hi)
                    if not sel.any():
                        continue
                    sz = Z[:, tk]
                    grp = np.where(sz >= whi, "wall", np.where(sz <= 3.0, "thin", "mid"))
                    pen = sgn * (P[:, tk] - ext)
                    dbin = np.round(dist * 2) / 2.0
                    for j in np.nonzero(sel)[0]:
                        r = dict(sess=sess, side=side, tier=tk, dbin=float(dbin[j]), volbin=int(pvb[j]),
                                 grp=str(grp[j]), breach=float(pen[j] >= BR))
                        allr.append(r); per[sess].append(r)
        for lab, key, mc in (("raw", ("sess", "side", "tier", "dbin"), 20),
                             ("volctl4", ("sess", "side", "tier", "dbin", "volbin"), 20)):
            d = diff_by_cell(allr, key, "breach", mc)
            ps = []
            for s in sorted(per):
                rr = diff_by_cell(per[s], key, "breach", mc)
                ps.append(None if rr is None else round(rr["diff"], 4))
            neg = sum(1 for x in ps if x is not None and x < 0)
            name = f"{mode}/wall>={whi:.0f}/{lab}"
            out[name] = {"pooled": d, "per_session": ps}
            print(f"{name:<28} cells={d['cells']:>4} nW={d['n_wall']:>6} nT={d['n_thin']:>6} "
                  f"dP={d['diff']:+.4f} neg {neg}/{sum(x is not None for x in ps)} {ps}")
Path("/Users/jackm4/goldenstocks/reports/research/channel_lab/verify_a3_deep_volctl.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
