#!/usr/bin/env python3
"""B3 複核第三段：牆資訊的衰減速度。
決定「最後一段連續可見」這個嚴格定義是不是合理——
若 δ 秒前看到的 size@L 對「觸價前最後可行動時刻」的 size@L 幾乎無資訊，
則把 60 秒前的一瞥算成「撈到牆資訊」就是灌水。"""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
import numpy as np

DAYS=["2026-08-14","2026-08-15","2026-08-17","2026-08-18","2026-08-19"]
DISTANCES=[12,24,33,66]; ANCHOR=30.0; HORIZON=1800.0; GAP=120.0
LAGS=[1.0,2.0,5.0,10.0,20.0,40.0]

def load(day):
    p=Path.home()/"goldenstocks-data"/"cache"/"tmf_books"/f"tmf_books_{day}.jsonl"
    out=[]
    if not p.exists(): return out
    for line in p.open():
        line=line.strip()
        if not line: continue
        r=json.loads(line)
        b,a=r.get("bids") or [],r.get("asks") or []
        if not b or not a: continue
        try:
            wall=datetime.fromisoformat(str(r["ts"])).timestamp(); bt=float(r["book_time"])/1e6
        except Exception: continue
        if ("stale" in r and bool(r["stale"])) or wall-bt>5.0: continue
        out.append((bt,"day" if str(r.get("quote_type"))=="FUTURE" else "night",
                    [(float(x["price"]),float(x["size"])) for x in b],
                    [(float(x["price"]),float(x["size"])) for x in a]))
    out.sort(key=lambda r:(r[1],r[0])); return out

def sz(row,L,side):
    for p,s in (row[3] if side=="up" else row[2]):
        if abs(p-L)<0.5: return s
    return 0.0

acc={lg:[] for lg in LAGS}; base=[]
for day in DAYS:
    rows=load(day)
    if not rows: continue
    cur=[];blks=[]
    for r in rows:
        if cur and (r[1]!=cur[-1][1] or r[0]-cur[-1][0]>GAP):
            if len(cur)>50: blks.append(cur)
            cur=[]
        cur.append(r)
    if len(cur)>50: blks.append(cur)
    for bl in blks:
        t=np.array([r[0] for r in bl])
        bb=np.array([r[2][0][0] for r in bl]); ba=np.array([r[3][0][0] for r in bl])
        bmin=np.array([min(p for p,_ in r[2]) for r in bl]); amax=np.array([max(p for p,_ in r[3]) for r in bl])
        mid=(bb+ba)/2; n=len(t)
        anc=np.unique(np.searchsorted(t,np.arange(t[0],t[-1],ANCHOR))); anc=anc[anc<n-5]
        seen=set()
        for i in anc:
            i=int(i); je=int(np.searchsorted(t,t[i]+HORIZON))
            if je<=i+2: continue
            lo=i+1; cmu=np.maximum.accumulate(bb[lo:je]); cmd=-np.minimum.accumulate(ba[lo:je]); m=je-lo
            for D in DISTANCES:
                for side in ("up","dn"):
                    L=float(round(mid[i]+D) if side=="up" else round(mid[i]-D))
                    k=int(np.searchsorted(cmu if side=="up" else cmd,L if side=="up" else -L,"left"))
                    if k>=m: continue
                    jt=lo+k; tt=float(t[jt])
                    key=(D,side,L,round(tt,3))
                    if key in seen: continue
                    seen.add(key)
                    # 參考點：觸價前 1 秒（最後一個現實上還能下動作的讀數）
                    qr=int(np.searchsorted(t,tt-1.0,"right"))-1
                    if qr<0 or qr>=jt: continue
                    ref=sz(bl[qr],L,side); base.append(ref>0)
                    for lg in LAGS:
                        q=int(np.searchsorted(t,tt-lg,"right"))-1
                        if q<0 or q>=jt: continue
                        vis=(amax[q]>=L) if side=="up" else (bmin[q]<=L)
                        if not vis: continue
                        acc[lg].append((sz(bl[q],L,side),ref))

b=np.mean(base)
print(f"對照組（無條件）P(觸價前1秒 L 上仍有量) = {b:.3f}   n={len(base)}")
print(f"{'lag':>5} {'n':>6} {'P(ref>0|可見)':>14} {'lift(pp)':>9} {'spearman':>9} {'ref中位':>7}")
for lg in LAGS:
    a=acc[lg]
    if len(a)<50: print(f"{lg:5.0f} {len(a):6d}  (太少)"); continue
    x=np.array([p[0] for p in a]); y=np.array([p[1] for p in a])
    sp=np.corrcoef(np.argsort(np.argsort(x)),np.argsort(np.argsort(y)))[0,1]
    print(f"{lg:5.0f} {len(a):6d} {np.mean(y>0):14.3f} {100*(np.mean(y>0)-b):9.1f} {sp:9.3f} {np.median(y):7.1f}")
