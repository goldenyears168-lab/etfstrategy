"""Adversarial global-penalty review of the 2 claimed survivors.
Global search space = sum of ALL 5 families' trials = 16+15+55+17+30 = 133.
Sensitivity: also +110 pre-existing stage-1 scan => 243.
DSR recomputed at N in {local, 133, 243}. fwd = t+1 open->close (no lookahead).
"""
import numpy as np, pandas as pd
from scipy.stats import norm

ROOT = "/Users/jackm4/goldenstocks"
PANEL = f"{ROOT}/data/research/chip_macro/panel.parquet"
MACRO = f"{ROOT}/data/research/dashboard/global_macro_data.parquet"
BREADTH = f"{ROOT}/data/research/dashboard/pct_ma200_breadth.parquet"
GAMMA = 0.5772156649
ANN = np.sqrt(252)
COST = 4.0
RNG = np.random.default_rng(7)

GLOBAL_TRIALS = 16 + 15 + 55 + 17 + 30  # 133
KITCHEN = GLOBAL_TRIALS + 110           # 243 (add stage-1 legacy scan)

def rz(s, w):
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()

def psr(sr, n, g3, g4, sr_star):
    denom = np.sqrt(1 - g3 * sr + (g4 - 1) / 4.0 * sr**2)
    return norm.cdf((sr - sr_star) * np.sqrt(n - 1) / denom)

def emax(var, N):
    return np.sqrt(var) * ((1 - GAMMA) * norm.ppf(1 - 1.0/N) + GAMMA * norm.ppf(1 - 1.0/(N*np.e)))

# ------------------------------------------------------------------ load
p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
p["date"] = pd.to_datetime(p["date"])
macro = pd.read_parquet(MACRO)[["date","VIX"]].copy(); macro["date"]=pd.to_datetime(macro["date"])
bd = pd.read_parquet(BREADTH)[["date","pct_above_ma200"]]; bd["date"]=pd.to_datetime(bd["date"])
pg = p.merge(macro,on="date",how="left").merge(bd,on="date",how="inner").sort_values("date").reset_index(drop=True)

# =================================================================== GRID survivor (family 5)
print("="*72)
print("FAMILY 5 survivor: champ+ x above_MA200 x breadth-STRONG  (fwd20 overlap hold)")
print("="*72)
FWD=20
day_ret_g = (pg["ix_close"]/pg["ix_open"]-1.0).shift(-1)
z60 = rz(pg["fut_foreign_oi"],60)
A = z60>0
B = pg["VIX"]>pg["VIX"].rolling(252,min_periods=120).median()
C = pg["pct_above_ma200"]>pg["pct_above_ma200"].rolling(252,min_periods=120).median()
ma200 = pg["ix_close"].rolling(200,min_periods=200).mean()
D = pg["ix_close"]>ma200
valid = A.notna()&B.notna()&C.notna()&D.notna()&z60.notna()&ma200.notna()
idxret = pg["ix_close"]/pg["ix_close"].shift(1)-1.0
def overlap(fire):
    f=fire.astype(float).reindex(pg.index).fillna(0.0)
    pos=f.shift(1).rolling(FWD,min_periods=1).mean()
    r=pos*idxret-pos.diff().abs().fillna(0)*(COST/1e4)
    return r.dropna()
n=len(pg); oos=pd.Series(pg.index>=int(n*0.7),index=pg.index)
tsr=[]
for a in (True,False):
 for b in (True,False):
  for c in (True,False):
   for d in (True,False):
    m=valid&(A==a)&(B==b)&(C==c)&(D==d)
    r=overlap(m); ro=r[oos.reindex(r.index).fillna(False)]
    if len(ro)>20 and ro.std()>0: tsr.append(ro.mean()/ro.std())
var=np.var(tsr,ddof=1)
print(f"trial-dispersion var(daily SR OOS)={var:.3e}  sqrt={np.sqrt(var):.4f}  (n_disp={len(tsr)})")
for Nlbl,N in [("local16",16),("GLOBAL133",GLOBAL_TRIALS),("kitchen243",KITCHEN)]:
    print(f"  SR*_ann(N={N})={emax(var,N)*ANN:+.3f}")
print()
for name,fire in [("champ+ only",A&valid),
                  ("champ+ x aMA200 (known)",A&D&valid),
                  ("champ+ x aMA200 x brStr (CANDIDATE)",A&D&C&valid),
                  ("champ+ x aMA200 x brWk (contrast)",A&D&(~C)&valid)]:
    r=overlap(fire); rr=r.values
    s=rr.mean()/rr.std(); g3=pd.Series(rr).skew(); g4=pd.Series(rr).kurtosis()+3
    row=f"  {name:38s} ann.Sh={s*ANN:+.2f}  n={len(rr)}"
    for N in [16,GLOBAL_TRIALS,KITCHEN]:
        d=psr(s,len(rr),g3,g4,emax(var,N)); row+=f"  DSR(N={N})={d:.3f}"
    print(row)

# =================================================================== CAPxCOV survivor (family 3)
print()
print("="*72)
print("FAMILY 3 survivor: CAP(margin_z60<-1.5) x COV(foreign fut doi cover streak>=2)")
print("="*72)
ev = pd.read_parquet(f"{ROOT}/data/research/dashboard/bottom_capcov_events.parquet")
print(f"events n={len(ev)}  champ_off n={int(ev.champ_off.sum())}  champ_on n={int((~ev.champ_off).sum())}")
# ---- honest EVENT-LEVEL per-event Sharpe + DSR (independent unit = event, not overlapping day)
for h in ["fwd5","fwd10","fwd20"]:
    x=ev[h].dropna().values
    s=x.mean()/x.std(ddof=1); g3=pd.Series(x).skew(); g4=pd.Series(x).kurtosis()+3
    # per-event var_trials proxy: sampling var of a per-event Sharpe estimate ~ 1/n_events (canonical)
    var_ev=1.0/len(x)
    row=f"  {h}: mean={x.mean()*100:+.2f}% win={(x>0).mean()*100:.0f}% perEventSharpe={s:+.3f} n={len(x)}"
    for N in [35,GLOBAL_TRIALS,KITCHEN]:
        d=psr(s,len(x),g3,g4,emax(var_ev,N)); row+=f"  DSR(N={N})={d:.3f}"
    print(row)
# ---- overlay Sharpe (their reported metric) recomputed & deflated at global N
# rebuild overlay exposure from event dates on full panel daily close returns
pp=p.copy(); pp["r"]=pp["ix_close"].pct_change()
evd=set(pd.to_datetime(ev.date))
n=len(pp); expo=np.zeros(n)
di={d:i for i,d in enumerate(pp.date)}
for d in evd:
    i=di[d]
    s,e=i+1,min(i+10,n-1)
    if s<=e: expo[s:e+1]+=1
inpos=(expo>0).astype(float); rr=np.nan_to_num(pp.r.values)*inpos
rp=rr[inpos>0]
s=rp.mean()/rp.std(); g3=pd.Series(rp).skew(); g4=pd.Series(rp).kurtosis()+3
var_ov=1.0/len(rp)  # canonical simplified dispersion
row=f"  overlay(h10 hold) ann.Sh={s*ANN:+.2f} n_posdays={len(rp)}"
for N in [35,GLOBAL_TRIALS,KITCHEN]:
    d=psr(s,len(rp),g3,g4,emax(var_ov,N)); row+=f"  DSR(N={N})={d:.3f}"
print(row)

# ---- restatement / residual checks (champ-off orthogonality)
print("\n[restatement check] CAPxCOV within champion-OFF subset (removes champion overlap):")
for h in ["fwd5","fwd10","fwd20"]:
    off=ev[ev.champ_off][h].dropna().values
    on=ev[~ev.champ_off][h].dropna().values
    # permutation of champ_off label within events
    allx=ev[h].dropna(); mask=ev.loc[allx.index,"champ_off"].values.astype(bool)
    k=mask.sum(); pool=allx.values; obs=pool[mask].mean()
    null=np.array([RNG.choice(pool,k,replace=False).mean() for _ in range(5000)])
    pp2=(null>=obs).mean()
    print(f"  {h}: champ_OFF mean={off.mean()*100:+.2f}% (n={len(off)}) vs champ_ON {on.mean()*100:+.2f}% (n={len(on)})  perm p(off>=)={pp2:.3f}")
print(f"  corr(fwd10, CHAMP dummy) = {np.corrcoef(ev.fwd10, ev.CHAMP.astype(float))[0,1]:+.3f}")
