#!/usr/bin/env bash
# Chain overnight research after wave-1 hunt exits.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
export PYTHONPATH=src:scripts/research
export PYTHONUNBUFFERED=1
LOG="logs/overnight_chain_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

echo "[chain] start $(date)"

# Wait for any run_overnight_branch_rotation_hunt.py
while pgrep -f 'run_overnight_branch_rotation_hunt.py' >/dev/null 2>&1; do
  echo "[chain] waiting hunt $(date +%H:%M:%S)"
  sleep 60
done
echo "[chain] hunt done"

HUNT_DIR=$(ls -dt reports/research/branch-footprint-screen/overnight_hunt_* | head -1)
export OVERNIGHT_DIR="$ROOT/$HUNT_DIR"
echo "[chain] OVERNIGHT_DIR=$OVERNIGHT_DIR"

.venv/bin/python -u scripts/research/run_overnight_branch_hunt_wave2.py

echo "[chain] wave2 done; launching wave3 stress search"
.venv/bin/python -u - <<'PY'
"""Wave3: focused search for excess_med>=10 with honest constraints."""
from __future__ import annotations
import importlib.util, os, sys
from collections import defaultdict, Counter
from datetime import datetime
from pathlib import Path
import numpy as np, pandas as pd

ROOT=Path('/Users/jackm4/Documents/ETF/股票研究')
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts'/'research')]
spec=importlib.util.spec_from_file_location('mvp', ROOT/'scripts/research/run_abc_rolling_top10_l1h7_mvp.py')
mvp=importlib.util.module_from_spec(spec); sys.modules['mvp']=mvp; spec.loader.exec_module(mvp)
out=Path(os.environ['OVERNIGHT_DIR'])

def log(m):
    line=f"[{datetime.now().strftime('%H:%M:%S')}] W3 {m}"; print(line, flush=True)
    (out/'run.log').open('a').write(line+'\n')

TOP4=[('9875','元大-土城永寧'),('918X','群益金鼎-台北'),('9661','富邦新店'),('981j','元大-士林')]
WEIGHT={'2330','2317','2454','2308','2881','2412','3045'}
db=ROOT/'data/scratch/rolling_mvp_snapshot.db'
ids=[b[0] for b in TOP4]
conn=mvp.connect_readonly(db)
end=str(mvp.query_with_retry(conn,'SELECT MAX(trade_date) FROM stock_daily_bars WHERE source=?',(mvp.SOURCE,))[0][0])
cal=mvp.load_calendar(conn,mvp.WINDOW_START,end)
bench=mvp.load_benchmark(conn,cal[0],cal[-1])
ohlc=mvp.load_stock_ohlc(conn,cal[0],cal[-1])
buys=mvp.load_top_buys(conn,ids,cal[0],cal[-1])
conn.close()
di={d:i for i,d in enumerate(cal)}

def oc(sid,d,hold):
    si=di.get(d)
    if si is None: return None
    ei,xi=si+1,si+hold
    if xi>=len(cal): return None
    ed,xd=cal[ei],cal[xi]
    er,xr=ohlc.get((sid,ed)),ohlc.get((sid,xd))
    be,bx=bench.get(ed),bench.get(xd)
    if not er or not xr or not be or not bx: return None
    sr=xr[1]/er[0]-1-mvp.COST_RATE; br=bx[1]/be[0]-1
    return dict(signal_date=d,entry_date=ed,exit_date=xd,stock_id=sid,stock_pct=sr*100,excess_pct=(sr-br)*100)

def summarize(legs,min_n=8):
    if len(legs)<min_n: return dict(n=len(legs),ok=False,excess_med=np.nan,excess_mean=np.nan,win=np.nan,max1=np.nan)
    xs=np.array([x['excess_pct'] for x in legs]); ctr=Counter(x['stock_id'] for x in legs)
    return dict(n=len(legs),ok=True,excess_med=float(np.median(xs)),excess_mean=float(np.mean(xs)),win=float((xs>0).mean()*100),max1=max(ctr.values())/len(legs))

def save(name,meta,legs,min_n=5):
    st=summarize(legs,min_n)
    row={'hypothesis':name,'wave':3,**meta,**st}
    p=out/'all_results.csv'
    pd.DataFrame([row]).to_csv(p,mode='a',header=not p.exists(),index=False)
    tag=' ★★★' if st['ok'] and st['excess_med']>=10 else (' ★' if st['ok'] and st['excess_med']>=5 else '')
    log(f"{name}: n={st['n']} med={st['excess_med']:+.2f}%{tag}")
    if st['ok'] and st['excess_med']>=5:
        pd.DataFrame(legs).to_csv(out/f'legs_{name}.csv',index=False)

# Walk-forward consensus: every 60d fit abs/share/min_b maximizing train med with n in [5,20], apply next 60d
log('walk-forward consensus among top4')
events={bid:[buys[bid][d] for d in sorted(buys.get(bid,{}))] for bid,_ in TOP4}
dates=sorted({e.trade_date for bid in events for e in events[bid]})
for hold in (5,7,10):
  for train in (40,60):
    oos=[]
    i=train
    while i < len(dates):
      train_set=set(dates[i-train:i]); test_set=set(dates[i:min(i+train,len(dates))])
      last_train=max(train_set)
      best=None
      for min_b in (2,3):
        for a in (5e7,1e8,1.5e8,2e8,3e8,5e8):
          for s in (0.2,0.3,0.4,0.5):
            for xw in (False,True):
              # train signals
              day_map=defaultdict(lambda: defaultdict(set))
              for bid,_ in TOP4:
                for e in events[bid]:
                  if e.trade_date not in train_set: continue
                  if e.buy_amt<a or e.buy_share<s: continue
                  if xw and e.stock_id in WEIGHT: continue
                  day_map[e.trade_date][e.stock_id].add(bid)
              xs=[]
              for d,stocks in day_map.items():
                for sid,bids in stocks.items():
                  if len(bids)<min_b: continue
                  o=oc(sid,d,hold)
                  if o is None or o['exit_date']>last_train: continue
                  xs.append(o['excess_pct'])
              n=len(xs)
              if not (5<=n<=25): continue
              med=float(np.median(xs)); mean=float(np.mean(xs))
              cand=(med,mean,n,min_b,a,s,xw)
              if best is None or cand[:2]>best[:2]: best=cand
      if best is not None:
        _,_,_,min_b,a,s,xw=best
        day_map=defaultdict(lambda: defaultdict(set))
        for bid,_ in TOP4:
          for e in events[bid]:
            if e.trade_date not in test_set: continue
            if e.buy_amt<a or e.buy_share<s: continue
            if xw and e.stock_id in WEIGHT: continue
            day_map[e.trade_date][e.stock_id].add(bid)
        for d,stocks in day_map.items():
          for sid,bids in stocks.items():
            if len(bids)<min_b: continue
            o=oc(sid,d,hold)
            if o: oos.append({**o,'n_branches':len(bids),'rule':f'm{min_b} a{a/1e8:g} s{int(s*100)} xw{xw}'})
      i+=train
    save(f'W3_wf_cons_tr{train}_H{hold}', dict(family='W3_wf_consensus',train=train,hold=hold), oos, min_n=6)

# Pure top-decile buy_amt days across top4 combined
log('cross-section top decile buy days')
for hold in (5,7,10):
  rows=[]
  for bid,_ in TOP4:
    for e in events[bid]:
      o=oc(e.stock_id,e.trade_date,hold)
      if o: rows.append({**o,'buy_amt':e.buy_amt,'buy_share':e.buy_share,'branch_id':bid})
  if not rows: continue
  amts=np.array([r['buy_amt'] for r in rows])
  thr=float(np.quantile(amts,0.9))
  legs=[r for r in rows if r['buy_amt']>=thr]
  # dedupe day keep max amt
  by={}
  for r in legs:
    cur=by.get(r['signal_date'])
    if cur is None or r['buy_amt']>cur['buy_amt']: by[r['signal_date']]=r
  uniq=[by[d] for d in sorted(by)]
  save(f'W3_topdecile_H{hold}', dict(family='W3_topdecile',hold=hold,thr=thr), uniq, min_n=8)

# Refresh ranking
df=pd.read_csv(out/'all_results.csv').sort_values(['excess_med','n'],ascending=[False,False])
df.to_csv(out/'all_results_ranked.csv',index=False)
ge10=df[(df['ok']==True)&(df['excess_med']>=10)&(df['n']>=8)]
ge5=df[(df['ok']==True)&(df['excess_med']>=5)&(df['n']>=10)]
md=[]
md.append('# Overnight FINAL synthesis\n\n')
md.append(f'- generated: {datetime.now().isoformat(timespec="seconds")}\n')
md.append(f'- rows: {len(df)}\n')
md.append(f'- excess_med>=10 & N>=8: **{len(ge10)}**\n')
md.append(f'- excess_med>=5 & N>=10: **{len(ge5)}**\n\n')
md.append('## Best 30\n\n|hypothesis|n|excess_med|excess_mean|win|max1|\n|---|---:|---:|---:|---:|---:|\n')
for r in df[df['ok']==True].head(30).itertuples():
  md.append(f'|{r.hypothesis}|{int(r.n)}|{r.excess_med:+.2f}|{r.excess_mean:+.2f}|{r.win:.0f}|{r.max1*100:.0f}%|\n')
if len(ge10):
  md.append('\n## ★★★ +10% hits\n\n')
  for r in ge10.itertuples():
    md.append(f'- `{r.hypothesis}` n={int(r.n)} med={r.excess_med:+.2f} mean={r.excess_mean:+.2f} win={r.win:.0f}\n')
else:
  md.append('\n## No stable +10% protocol found under N>=8.\n\n')
  md.append('Closest feasible methods are the top rows above. Prefer walk-forward / consensus families over in-sample solo adaptive.\n')
(out/'FINAL_SUMMARY.md').write_text(''.join(md),encoding='utf-8')
log(f'FINAL ge10={len(ge10)} ge5={len(ge5)} -> {out/"FINAL_SUMMARY.md"}')
print(df[df['ok']==True].head(15)[['hypothesis','n','excess_med','excess_mean','win']].to_string(index=False))
PY

echo "[chain] attempting short40 backfill if specialty idle"
if pgrep -f 'backfill_broker_branch_tape.py' >/dev/null 2>&1; then
  echo "[chain] specialty/backfill still running — skip short40 for now"
else
  nohup .venv/bin/python -u scripts/research/backfill_broker_branch_tape.py \
    --universe-json reports/research/branch-footprint-screen/_abc_short40_universe.json \
    --start 2024-07-01 --end 2026-07-17 \
    --progress reports/research/branch-footprint-screen/_abc_short40_2y_progress.json \
    --delay 0.55 --fetch-workers 1 \
    > "logs/backfill_abc_short40_overnight_$(date +%Y%m%d_%H%M%S).log" 2>&1 &
  echo "[chain] short40 pid $!"
fi

echo "[chain] all stages launched/finished $(date)"
