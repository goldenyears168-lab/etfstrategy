import sys, math, json, statistics as st
from collections import defaultdict
sys.path.insert(0,'/Users/jackm4/goldenstocks/scripts/research')
import vp_g1a_node_ic as M

# contiguous sample of days: last 120 available files (OOS-ish) + 120 mid IS
dates = sorted(p.stem for p in M.TICK_DIR.glob('*.json'))
print("n_files",len(dates), dates[0], dates[-1])
def run(sub):
    pairs=[(d, dates[dates.index(d)-1] if dates.index(d)>0 else None) for d in sub]
    rows=[]
    for d,p in pairs:
        r,_=M.process_day(d,p); rows.extend(r)
    return rows

seg = dates[-45:]
rows = run(seg)
day = [r for r in rows if r['block']=='day']
have=[r for r in day if 'near_node_dist' in r and 'plac_matched_dist' in r]
print("day rows",len(day),"with node+matched",len(have))

import numpy as np
nd=np.array([r['near_node_dist'] for r in have]); pm=np.array([r['plac_matched_dist'] for r in have])
ns=np.array([r['near_node_strength'] for r in have]); ps=np.array([r['plac_matched_strength'] for r in have])
print("abs dist node p10/50/90", np.percentile(np.abs(nd),[10,50,90]))
print("abs dist plac p10/50/90", np.percentile(np.abs(pm),[10,50,90]))
print("mean|nd|",np.abs(nd).mean(),"mean|pm|",np.abs(pm).mean())
print("exact abs-dist equal share", float((np.abs(nd)==np.abs(pm)).mean()))
print("median |abs diff|", float(np.median(np.abs(np.abs(nd)-np.abs(pm)))))
print("sign equal share", float((np.sign(nd)==np.sign(pm)).mean()))
print("corr(nd,pm)", float(np.corrcoef(nd,pm)[0,1]))
print("STRENGTH node p10/50/90", np.percentile(ns,[10,50,90]), "mean",ns.mean())
print("STRENGTH plac p10/50/90", np.percentile(ps,[10,50,90]), "mean",ps.mean())
print("plac strength >=2.0 share", float((ps>=2.0).mean()), ">=2.5", float((ps>=2.5).mean()))
# random placebo distance
hr=[r for r in day if 'plac_random_dist' in r]
pr=np.array([r['plac_random_dist'] for r in hr])
print("abs dist RANDOM plac p10/50/90", np.percentile(np.abs(pr),[10,50,90]), "mean", np.abs(pr).mean())
md=np.array([r['mid_dist'] for r in day]); print("abs mid_dist mean", np.abs(md).mean())
