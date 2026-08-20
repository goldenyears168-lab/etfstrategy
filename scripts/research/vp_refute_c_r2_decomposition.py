import sys, json, numpy as np
sys.path.insert(0,'/Users/jackm4/goldenstocks/scripts/research')
import vp_g1c_mechanical as G
C=G.CIX
X=np.load('/tmp/vpref/X200.npy')
day_idx=X[:,C["day_idx"]].astype(np.int64)
F=G.build_features(X,C)
mask=np.ones(len(X),bool)
lz3=F["NODE"][0][1]
def r2_on(blocks, extra=None):
    cols=[np.ones(len(X))]
    for b in blocks:
        for nm,v in F[b]:
            if extra is not None and nm not in extra and b=="C1_been": continue
            cols.append(v)
    A=np.column_stack(cols)
    beta,*_=np.linalg.lstsq(A,lz3,rcond=None)
    resid=lz3-A@beta
    return 1-resid.var()/lz3.var()
allb=["C3_dist","C2_act","C4_anchor","C1_been"]
print("R2 lz3 ~ ALL controls (incl lc3):", round(r2_on(allb),5))
print("R2 lz3 ~ controls WITHOUT lc3/lc3^2 (keep vis,rec):", round(r2_on(allb,extra={"vis","rec"}),5))
print("R2 lz3 ~ dist+act+anchor only:", round(r2_on(["C3_dist","C2_act","C4_anchor"]),5))
lc3=F["C1_been"][2][1]
print("R2 lz3 ~ lc3 alone:", round(np.corrcoef(lz3,lc3)[0,1]**2,5))
# partial IC on touched
touched=X[:,C["touched"]].astype(np.float64)
z3=X[:,C["z3"]].astype(np.float64); lz=np.log1p(z3)
print("raw IC(lz3, touched)", round(float(np.corrcoef(lz,touched)[0,1]),5))
def partial_ic(blocks, drop_lc3):
    cols=[np.ones(len(X))]
    for b in blocks:
        for nm,v in F[b]:
            if drop_lc3 and nm in ("lc3","lc3_2"): continue
            cols.append(v)
    A=np.column_stack(cols)
    bx,*_=np.linalg.lstsq(A,lz,rcond=None); ex=lz-A@bx
    by,*_=np.linalg.lstsq(A,touched,rcond=None); ey=touched-A@by
    return float(np.corrcoef(ex,ey)[0,1])
print("partial IC (all controls incl lc3):", round(partial_ic(allb,False),5))
print("partial IC (all controls WITHOUT lc3):", round(partial_ic(allb,True),5))
print("partial IC (dist+act+anchor only):", round(partial_ic(["C3_dist","C2_act","C4_anchor"],True),5))
