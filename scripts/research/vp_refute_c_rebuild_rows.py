import sys, json, numpy as np, datetime as dt
sys.path.insert(0,'/Users/jackm4/goldenstocks/scripts/research')
import vp_g1c_mechanical as G
from pathlib import Path
meta=json.loads((G.CACHE_DIR/"_index.json").read_text())
# contiguous block: last 200 days
sel=list(range(len(meta)-200,len(meta)))
rows=[]
for i in sel:
    r=G.build_day_rows(meta[i]["date"], i, meta[i-1] if i>0 else None)
    if r is not None and len(r): rows.append(r)
X=np.concatenate(rows)
np.save('/tmp/vpref/X200.npy',X)
print("rows",X.shape,"days",len(rows), meta[sel[0]]["date"], meta[sel[-1]]["date"])
