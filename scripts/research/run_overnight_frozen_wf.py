#!/usr/bin/env python3
"""Walk-forward with FROZEN rules (no re-fit) — honest deploy test."""
from __future__ import annotations
import json, importlib.util, sys
from collections import Counter
from datetime import datetime
from pathlib import Path
import numpy as np, pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/"src"), str(ROOT/"scripts"/"research")]
spec = importlib.util.spec_from_file_location("mvp", ROOT/"scripts/research/run_abc_rolling_top10_l1h7_mvp.py")
mvp = importlib.util.module_from_spec(spec); sys.modules["mvp"]=mvp; spec.loader.exec_module(mvp)
CACHE = ROOT/"data/scratch/overnight_cache"
OUT = sorted((ROOT/"reports/research/branch-footprint-screen").glob("overnight_hunt_*"), key=lambda p:p.stat().st_mtime, reverse=True)[0]
WEIGHT = {"2330","2317","2454","2308","2881","2412","3045","2303","2382"}
RULES = [
    ("9217","凱基松山",1.5e8,0.25),
    ("918X","群益金鼎-台北",2e8,0.4),
    ("9661","富邦新店",2e8,0.4),
    ("9875","元大-土城永寧",3e8,0.5),
    ("981j","元大-士林",3e8,0.3),
]

def summarize(legs, min_n=8):
    if len(legs) < min_n:
        return dict(n=len(legs), ok=False, excess_med=np.nan, excess_mean=np.nan, win=np.nan, max1=np.nan, slot_med=np.nan, slot_dd=np.nan)
    xs=np.array([x["excess_pct"] for x in legs]); ctr=Counter(x["stock_id"] for x in legs)
    taken=[]; busy=None
    for lg in sorted(legs, key=lambda z:z["entry_date"]):
        if busy is not None and lg["entry_date"]<=busy: continue
        taken.append(lg); busy=lg["exit_date"]
    txs=np.array([x["excess_pct"] for x in taken]); trs=np.array([x["stock_pct"] for x in taken])/100
    eq=np.cumprod(1+trs); dd=float((eq/np.maximum.accumulate(eq)-1).min()*100)
    return dict(n=len(legs), ok=True, excess_med=float(np.median(xs)), excess_mean=float(np.mean(xs)), win=float((xs>0).mean()*100), max1=max(ctr.values())/len(legs), slot_med=float(np.median(txs)), slot_dd=dd)

def main():
    events=json.loads((CACHE/"events_all.json").read_text()); cal=json.loads((CACHE/"calendar.json").read_text()); di={d:i for i,d in enumerate(cal)}
    conn=mvp.connect_readonly(ROOT/"data/scratch/rolling_mvp_snapshot.db")
    bench=mvp.load_benchmark(conn,cal[0],cal[-1]); ohlc=mvp.load_stock_ohlc(conn,cal[0],cal[-1]); conn.close()
    def oc(sid,d,hold):
        si=di.get(d)
        if si is None: return None
        ei,xi=si+1,si+hold
        if xi>=len(cal): return None
        ed,xd=cal[ei],cal[xi]
        er,xr=ohlc.get((sid,ed)),ohlc.get((sid,xd)); be,bx=bench.get(ed),bench.get(xd)
        if not er or not xr or not be or not bx: return None
        sr=xr[1]/er[0]-1-mvp.COST_RATE; br=bx[1]/be[0]-1
        return dict(signal_date=d,entry_date=ed,exit_date=xd,stock_id=sid,stock_pct=sr*100,excess_pct=(sr-br)*100)

    rows=[]
    # Expand: evaluate frozen rule only on OOS folds (train unused except skipping first train days)
    for hold in (7,10,14,20):
      for bid,name,a,s in RULES:
        dated={e["trade_date"]:e for e in events[bid]}
        dates=[d for d in cal if d in dated]
        for train in (40,60,80):
          oos=[]
          i=train
          while i < len(dates):
            te=set(dates[i:min(i+train,len(dates))])
            for d in sorted(te):
              e=dated[d]
              if e["buy_amt"]<a or e["buy_share"]<s: continue
              if e["stock_id"] in WEIGHT: continue
              o=oc(e["stock_id"],d,hold)
              if o: oos.append(o)
            i+=train
          st=summarize(oos, min_n=8)
          rows.append(dict(branch=name,hold=hold,train=train,abs=a,share=s,**st))
          print(f"frozenWF {name} H{hold} tr{train}: n={st['n']} med={st['excess_med']:+.2f} win={st['win']:.0f}", flush=True)

    # Combo: OR maxamt among rules, frozen, WF folds
    for hold in (10,14,20):
      for train in (60,80):
        dates=sorted({e["trade_date"] for bid,_,_,_ in RULES for e in events[bid]})
        oos=[]
        i=train
        while i < len(dates):
          te=set(dates[i:min(i+train,len(dates))])
          by={}
          for bid,name,a,s in RULES:
            for e in events[bid]:
              if e["trade_date"] not in te: continue
              if e["buy_amt"]<a or e["buy_share"]<s: continue
              if e["stock_id"] in WEIGHT: continue
              o=oc(e["stock_id"],e["trade_date"],hold)
              if not o: continue
              row={**o,"buy_amt":e["buy_amt"]}
              cur=by.get(e["trade_date"])
              if cur is None or row["buy_amt"]>cur["buy_amt"]: by[e["trade_date"]]=row
          oos.extend(by[d] for d in sorted(by))
          i+=train
        st=summarize(oos,min_n=10)
        rows.append(dict(branch="COMBO_OR",hold=hold,train=train,abs="",share="",**st))
        print(f"frozenWF COMBO H{hold} tr{train}: n={st['n']} med={st['excess_med']:+.2f}", flush=True)

    df=pd.DataFrame(rows).sort_values(["excess_med","n"], ascending=[False,False])
    df.to_csv(OUT/"frozen_rule_walkforward.csv", index=False)
    ge8=df[(df.ok==True)&(df.n>=20)]
    md=["# Frozen-rule walk-forward (no re-fit)\n\n","Rule fixed; first `train` days skipped as burn-in; evaluate subsequent folds.\n\n"]
    md.append("|branch|hold|train|n|excess_med|excess_mean|win|max1|slot_med|slot_dd|\n|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
    for r in ge8.head(25).itertuples():
        md.append(f"|{r.branch}|{r.hold}|{r.train}|{int(r.n)}|{r.excess_med:+.2f}|{r.excess_mean:+.2f}|{r.win:.0f}|{r.max1*100:.0f}%|{r.slot_med:+.2f}|{r.slot_dd:+.0f}|\n")
    ge10=df[(df.ok==True)&(df.excess_med>=10)&(df.n>=15)]
    md.append(f"\n## Hits excess_med>=10 & N>=15: {len(ge10)}\n\n")
    for r in ge10.itertuples():
        md.append(f"- {r.branch} H{r.hold} tr{r.train}: n={int(r.n)} med={r.excess_med:+.2f}\n")
    (OUT/"FROZEN_WF.md").write_text("".join(md), encoding="utf-8")
    print(ge8.head(15).to_string(index=False))
    print("WROTE", OUT/"FROZEN_WF.md")

if __name__=="__main__":
    main()
