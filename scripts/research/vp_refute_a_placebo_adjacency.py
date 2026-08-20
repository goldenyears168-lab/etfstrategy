import sys, numpy as np
sys.path.insert(0,'/Users/jackm4/goldenstocks/scripts/research')
import vp_g1a_node_ic as M
dates=sorted(p.stem for p in M.TICK_DIR.glob('*.json'))
seg=dates[-45:]
rows=[]
for i,d in enumerate(dates):
    if d in seg:
        r,_=M.process_day(d, dates[i-1] if i else None); rows.extend(r)
day=[r for r in rows if r['block']=='day' and 'near_node_dist' in r and 'plac_matched_dist' in r]
nd=np.array([r['near_node_dist'] for r in day]); pm=np.array([r['plac_matched_dist'] for r in day])
gap=np.abs(nd-pm)
print("n",len(day))
for k in (0,1,2,3,5):
    print(f"share |node - placebo| <= {k} ticks: {float((gap<=k).mean()):.4f}")
print("p50/p75/p90 gap", np.percentile(gap,[50,75,90]))
# how does the placebo compare to the *actual strategy alternative* (fixed d=22)?
print("share |nd| in [12,33]:", float(((np.abs(nd)>=12)&(np.abs(nd)<=33)).mean()))
print("share |nd| < 12:", float((np.abs(nd)<12).mean()), " >33:", float((np.abs(nd)>33).mean()))
