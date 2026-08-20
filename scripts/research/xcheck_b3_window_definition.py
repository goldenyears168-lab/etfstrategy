#!/usr/bin/env python3
"""對抗性複核 B3：獨立重算 reaction window，並攻擊「最後一段連續可見」這個定義。

三件事：
1. 用完全獨立的 naive 實作重算觸價 index / window（抽樣對照 accumulate 版）。
2. 量「最後 60 秒內累計可見時間」與「固定 20s 時鐘 poller 在最後 60/120 秒內
   至少看到一次 L 的機率」——這是和「最後一段連續可見」對立的定義。
3. 量牆的持續性：t_touch-20s 看到的 size@L 對 t_touch 前最後一刻的 size@L
   有沒有預測力（若有，20 秒舊資訊就不是廢的，B3 的窗口定義就過嚴）。
"""
from __future__ import annotations
import json, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np

TZ = timezone(timedelta(hours=8))
DAYS = ["2026-08-14","2026-08-15","2026-08-17","2026-08-18","2026-08-19"]
DISTANCES = [12,24,33,66]
ANCHOR = 30.0; HORIZON = 1800.0; GAP = 120.0; TAIL = 180.0

def load(day):
    p = Path.home()/"goldenstocks-data"/"cache"/"tmf_books"/f"tmf_books_{day}.jsonl"
    out=[]
    if not p.exists(): return out
    for line in p.open():
        line=line.strip()
        if not line: continue
        r=json.loads(line)
        b,a=r.get("bids") or [], r.get("asks") or []
        if not b or not a: continue
        try:
            wall=datetime.fromisoformat(str(r["ts"])).timestamp(); bt=float(r["book_time"])/1e6
        except Exception: continue
        if (("stale" in r and bool(r["stale"])) or wall-bt>5.0): continue
        out.append((bt,"day" if str(r.get("quote_type"))=="FUTURE" else "night",
                    [float(x["price"]) for x in b],[float(x["size"]) for x in b],
                    [float(x["price"]) for x in a],[float(x["size"]) for x in a]))
    out.sort(key=lambda r:(r[1],r[0]))
    return out

class B:
    def __init__(self, day, rows):
        self.day=day; self.sess=rows[0][1]
        self.t=np.array([r[0] for r in rows])
        self.bb=np.array([r[2][0] for r in rows]); self.ba=np.array([r[4][0] for r in rows])
        self.bmin=np.array([min(r[2]) for r in rows]); self.amax=np.array([max(r[4]) for r in rows])
        self.mid=(self.bb+self.ba)/2
        # size at price grid: build dict per row lazily
        self.rows=rows

def size_at(row, L, side):
    px, sz = (row[4],row[5]) if side=="up" else (row[2],row[3])
    for p,s in zip(px,sz):
        if abs(p-L)<0.5: return s
    return 0.0

def blocks(day, rows):
    out=[];cur=[]
    for r in rows:
        if cur and (r[1]!=cur[-1][1] or r[0]-cur[-1][0]>GAP):
            if len(cur)>50: out.append(B(day,cur))
            cur=[]
        cur.append(r)
    if len(cur)>50: out.append(B(day,cur))
    return out

PHASES=np.linspace(0,1,64,endpoint=False)

def run():
    recs=[]
    naive_checks=[]
    for day in DAYS:
        rows=load(day)
        if not rows: continue
        for blk in blocks(day,rows):
            t,mid,bb,ba,amax,bmin=blk.t,blk.mid,blk.bb,blk.ba,blk.amax,blk.bmin
            n=len(t)
            grid=np.arange(t[0],t[-1],ANCHOR)
            anchors=np.unique(np.searchsorted(t,grid)); anchors=anchors[anchors<n-5]
            for i in anchors:
                i=int(i); j_end=int(np.searchsorted(t,t[i]+HORIZON))
                if j_end<=i+2: continue
                lo=i+1
                cmu=np.maximum.accumulate(bb[lo:j_end]); cmd=-np.minimum.accumulate(ba[lo:j_end])
                m=j_end-lo
                for D in DISTANCES:
                    for side in ("up","dn"):
                        L=float(round(mid[i]+D) if side=="up" else round(mid[i]-D))
                        k=int(np.searchsorted(cmu if side=="up" else cmd, L if side=="up" else -L,"left"))
                        if k>=m: continue
                        jt=lo+k; tt=float(t[jt])
                        s0=max(lo,int(np.searchsorted(t,tt-TAIL)))
                        if s0>=jt: s0=max(lo,jt-1)
                        seg=slice(s0,jt)
                        v = (amax[seg]>=L) if side=="up" else (bmin[seg]<=L)
                        ts_=t[seg]
                        if v.size==0: continue
                        # W_contig
                        if not v[-1]: w=0.0
                        else:
                            inv=np.nonzero(~v)[0]; st=int(inv[-1])+1 if inv.size else 0
                            w=tt-float(ts_[st])
                        # 累計可見時間（用快照間隔加權）：最後 60 / 120 秒
                        dt_=np.diff(np.concatenate([ts_,[tt]]))
                        out={"day":day,"sess":blk.sess,"D":D,"side":side,"L":L,"t_touch":round(tt,3),
                             "w":w}
                        for H in (60.0,120.0):
                            mm=ts_>=tt-H
                            out[f"vis_sec_{int(H)}"]=float(dt_[mm][v[mm]].sum()) if mm.any() else 0.0
                        # 固定 20s 時鐘 poller：最後 H 秒內是否至少一次看到可見
                        for H in (60.0,120.0):
                            hits=0
                            for ph in PHASES:
                                pts=np.arange(tt-H+ph*20.0, tt, 20.0)
                                if pts.size==0: continue
                                idx=np.searchsorted(ts_,pts,"right")-1
                                idx=idx[idx>=0]
                                if idx.size and v[idx].any(): hits+=1
                            out[f"poll20_any_{int(H)}"]=hits/len(PHASES)
                        # 牆持續性：t-20s 的 size@L vs 觸價前最後一刻 size@L（都只用過去）
                        q=int(np.searchsorted(t,tt-20.0,"right"))-1
                        if 0<=q<jt:
                            vis20 = (amax[q]>=L) if side=="up" else (bmin[q]<=L)
                            out["vis20"]=bool(vis20)
                            out["sz20"]=size_at(blk.rows[q],L,side) if vis20 else None
                        out["sz_last"]=size_at(blk.rows[jt-1],L,side)
                        recs.append(out)
    # dedup 同 B3
    seen=set(); ded=[]
    for e in recs:
        k=(e["day"],e["sess"],e["side"],e["D"],e["L"],e["t_touch"])
        if k in seen: continue
        seen.add(k); ded.append(e)
    print("touched episodes (my rerun, D in %s):"%DISTANCES, len(ded))
    w=np.array([e["w"] for e in ded])
    print("W_contig p10/p50/p90 = %.3f %.3f %.3f  mean=%.2f"%(*np.percentile(w,[10,50,90]),w.mean()))
    print("analytic coverage @20s (E[min(1,W/20)]) = %.4f"%np.minimum(1,w/20).mean())
    for H in (60,120):
        vs=np.array([e[f"vis_sec_{H}"] for e in ded])
        pa=np.array([e[f"poll20_any_{H}"] for e in ded])
        print(f"  last {H}s: 累計可見秒 p50={np.percentile(vs,50):.2f} mean={vs.mean():.2f} "
              f"| 固定20s時鐘至少看到一次 = {pa.mean():.4f}")
    # per-day
    byday=defaultdict(list)
    for e in ded: byday[e["day"]].append(e["poll20_any_60"])
    print("  per-day poll20_any_60:", {d:round(float(np.mean(v)),3) for d,v in sorted(byday.items())})
    bysess=defaultdict(list)
    for e in ded: bysess[e["sess"]].append(e["poll20_any_60"])
    print("  per-sess poll20_any_60:", {d:round(float(np.mean(v)),3) for d,v in sorted(bysess.items())})
    bysess2=defaultdict(list)
    for e in ded: bysess2[e["sess"]].append(min(1.0,e["w"]/20))
    print("  per-sess analytic contig@20s:", {d:round(float(np.mean(v)),3) for d,v in sorted(bysess2.items())})
    # 牆持續性
    pairs=[(e["sz20"],e["sz_last"]) for e in ded if e.get("vis20") and e.get("sz20") is not None]
    a=np.array([p[0] for p in pairs]); b=np.array([p[1] for p in pairs])
    print("wall persistence n=%d  corr(sz@t-20, sz@last)=%.3f  spearman=%.3f"%(
        len(pairs), np.corrcoef(a,b)[0,1],
        np.corrcoef(np.argsort(np.argsort(a)),np.argsort(np.argsort(b)))[0,1]))
    print("  sz@t-20 p50=%.1f  sz@last p50=%.1f  P(sz_last>0 | vis20)=%.3f"%(
        np.median(a),np.median(b),(b>0).mean()))
    # 對照：無條件（不管 vis20）觸價前最後一刻有量的比例
    allb=np.array([e["sz_last"] for e in ded])
    print("  對照 P(sz_last>0) 全體=%.3f"%(allb>0).mean())
    # 大牆是否可預測：sz20 高分位 vs 低分位 的 sz_last
    hi=a>=np.percentile(a,75); lopct=a<=np.percentile(a,25)
    print("  sz@t-20 top25%% -> sz_last median=%.1f ; bottom25%% -> %.1f"%(np.median(b[hi]),np.median(b[lopct])))

run()
