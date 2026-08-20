#!/usr/bin/env python3
"""B3 複核第二段：改用「最後 H 秒內任何一次輪詢看到 L 可見」這個對立定義，
反解達到 50/80/90% 所需的輪詢間隔，並拆日／拆 session。"""
from __future__ import annotations
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import numpy as np

DAYS=["2026-08-14","2026-08-15","2026-08-17","2026-08-18","2026-08-19"]
DISTANCES=[12,24,33,66]; ANCHOR=30.0; HORIZON=1800.0; GAP=120.0; TAIL=180.0
PHASES=np.linspace(0,1,32,endpoint=False)
GRID=[0.25,0.5,1.0,2.0,3.0,5.0,8.0,12.0,20.0,30.0]

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
                    min(x["price"] for x in b),max(x["price"] for x in a),
                    b[0]["price"],a[0]["price"]))
    out.sort(key=lambda r:(r[1],r[0])); return out

res=defaultdict(list)
for day in DAYS:
    rows=load(day)
    if not rows: continue
    cur=[]; blks=[]
    for r in rows:
        if cur and (r[1]!=cur[-1][1] or r[0]-cur[-1][0]>GAP):
            if len(cur)>50: blks.append(cur)
            cur=[]
        cur.append(r)
    if len(cur)>50: blks.append(cur)
    for bl in blks:
        sess=bl[0][1]
        t=np.array([r[0] for r in bl]); bmin=np.array([r[2] for r in bl],float)
        amax=np.array([r[3] for r in bl],float); bb=np.array([r[4] for r in bl],float)
        ba=np.array([r[5] for r in bl],float); mid=(bb+ba)/2; n=len(t)
        anc=np.unique(np.searchsorted(t,np.arange(t[0],t[-1],ANCHOR))); anc=anc[anc<n-5]
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
                    s0=max(lo,int(np.searchsorted(t,tt-TAIL)))
                    if s0>=jt: s0=max(lo,jt-1)
                    v=(amax[s0:jt]>=L) if side=="up" else (bmin[s0:jt]<=L)
                    if v.size==0: continue
                    ts_=t[s0:jt]
                    res[(day,sess)].append((ts_,v,tt,D,side,L))

def cov_alt(eps,delta,H):
    tot=0.0
    for ts_,v,tt,*_ in eps:
        h=0
        for ph in PHASES:
            pts=np.arange(tt-H+ph*delta,tt,delta)
            if pts.size==0: continue
            idx=np.searchsorted(ts_,pts,"right")-1; idx=idx[idx>=0]
            if idx.size and v[idx].any(): h+=1
        tot+=h/len(PHASES)
    return tot/len(eps)

def cov_contig(eps,delta):
    ws=[]
    for ts_,v,tt,*_ in eps:
        if not v[-1]: ws.append(0.0); continue
        inv=np.nonzero(~v)[0]; st=int(inv[-1])+1 if inv.size else 0
        ws.append(tt-float(ts_[st]))
    return float(np.minimum(1,np.array(ws)/delta).mean())

# dedup
allep=[]; 
for k,v in res.items():
    seen=set(); 
    for e in v:
        key=(k,e[3],e[4],e[5],round(e[2],3))
        if key in seen: continue
        seen.add(key); allep.append((k,e))
print("n episodes:",len(allep))
def sub(f): return [e for k,e in allep if f(k)]
sets={"ALL":sub(lambda k:True),"day":sub(lambda k:k[1]=="day"),"night":sub(lambda k:k[1]=="night"),
      "08-17day":sub(lambda k:k==("2026-08-17","day")),"08-18day":sub(lambda k:k==("2026-08-18","day"))}
print(f"{'set':10s} {'n':>6s} | " + " ".join(f"{g:>6}" for g in GRID))
for name,eps in sets.items():
    if not eps: continue
    row_c=[cov_contig(eps,g) for g in GRID]
    row_a=[cov_alt(eps,g,60.0) for g in GRID]
    print(f"{name:10s} {len(eps):6d} | contig " + " ".join(f"{x:6.3f}" for x in row_c))
    print(f"{'':10s} {'':6s} | any60s " + " ".join(f"{x:6.3f}" for x in row_a))

# ---- 上限檢查：加入動作延遲 a 後，理論最高可行動比例 ----
print("\n動作延遲 a -> 可行動上限（Δ→0）")
print(f"{'set':10s} " + " ".join(f"a={a:<5}" for a in (0.0,0.2,0.5,1.0,2.0)))
for name,eps in sets.items():
    if not eps: continue
    c_c=[];c_a=[]
    for a in (0.0,0.2,0.5,1.0,2.0):
        n_c=n_a=0
        for ts_,v,tt,*_ in eps:
            if v[-1]:
                inv=np.nonzero(~v)[0]; st=int(inv[-1])+1 if inv.size else 0
                w=tt-float(ts_[st])
            else: w=0.0
            if w>a: n_c+=1
            mm=(ts_<=tt-a)&(ts_>=tt-60.0)
            if mm.any() and v[mm].any(): n_a+=1
        c_c.append(n_c/len(eps)); c_a.append(n_a/len(eps))
    print(f"{name:10s} contig " + " ".join(f"{x:6.3f}" for x in c_c))
    print(f"{'':10s} any60s " + " ".join(f"{x:6.3f}" for x in c_a))
