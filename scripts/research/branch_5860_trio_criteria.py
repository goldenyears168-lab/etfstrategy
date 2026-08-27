import pandas as pd, numpy as np, sys, glob, re
from pathlib import Path
from stock_db import connect_ro
DIR=Path('reports/research/chip-signal-daily-horizon')
c=connect_ro()
px=pd.read_sql_query("SELECT stock_id,trade_date,source,open,high,low,close,volume/1000.0 vol FROM stock_daily_bars WHERE trade_date>='2025-01-01' AND close>0",c)
px['rk']=px.source.map({'finmind':0,'twse_mi_index':1,'tpex_daily':2}).fillna(9)
px=px.sort_values('rk').drop_duplicates(['stock_id','trade_date']).drop(columns=['rk','source'])
bm=pd.read_sql_query("SELECT trade_date,open,close FROM stock_daily_bars WHERE stock_id='0050' AND trade_date>='2025-01-01'",c).drop_duplicates('trade_date')
bm['bm_oc']=(bm.close/bm.open-1)*100; bm=bm[['trade_date','bm_oc']]
nm={}
for dd in ('2026-08-25','2025-06-10'):
    for t,n in c.execute("SELECT DISTINCT securities_trader_id,securities_trader FROM stock_broker_branch_daily WHERE trade_date=?",(dd,)): nm.setdefault(t,n)
def prep(tid):
    f=DIR/f'branch_{tid}_pricelevels.pkl'
    if not f.exists(): return None
    d=pd.read_pickle(f).drop_duplicates(['stock_id','trade_date'])
    d=d[d.trade_date>='2025-01-01'].copy()
    d['bv']=d.buy_amt/d.buy_vol.replace(0,np.nan); d['sv']=d.sell_amt/d.sell_vol.replace(0,np.nan)
    m=d.merge(px,on=['stock_id','trade_date'],how='inner').merge(bm,on='trade_date',how='left')
    m['dt_vol']=m[['buy_vol','sell_vol']].min(axis=1)
    m=m.dropna(subset=['bv','sv']); m=m[m.dt_vol>0].copy()
    m['dt_pnl']=(m.sv-m.bv)*m.dt_vol; m['dt_noti']=m.dt_vol*(m.bv+m.sv)/2
    m['spread']=(m.sv/m.bv-1)*100
    m['rt']=m.dt_vol/m[['buy_vol','sell_vol']].max(axis=1).replace(0,np.nan)
    m['lots']=(m.buy_vol+m.sell_vol)/1000
    m['rng']=(m.high-m.low).replace(0,np.nan)
    m['bp']=(m.bv-m.low)/m.rng; m['sp']=(m.sv-m.low)/m.rng
    return m
def nwm(g,col='spread'): return float(np.average(g[col],weights=g.dt_noti))
rows=[]
for tid in sys.argv[1:]:
    m=prep(tid)
    if m is None or len(m)<300: print(tid,'skip'); continue
    gm=nwm(m)
    s=m[m.rt>0.95]; sub=nwm(s) if len(s)>100 else np.nan
    nl=m[m.n_lvl>=10]; subnl=nwm(nl) if len(nl)>60 else np.nan
    # 判準4：只扣 beta 項，保留 alpha；名目加權
    v=m.dropna(subset=['bm_oc'])
    slope=np.polyfit(v.bm_oc,v.spread,1)[0]
    alpha_nw=nwm(v)-slope*float(np.average(v.bm_oc,weights=v.dt_noti))
    vs=v[v.rt>0.95]
    slope_s=np.polyfit(vs.bm_oc,vs.spread,1)[0] if len(vs)>100 else np.nan
    alpha_s=(nwm(vs)-slope_s*float(np.average(vs.bm_oc,weights=vs.dt_noti))) if len(vs)>100 else np.nan
    rows.append(dict(tid=tid,name=nm.get(tid,'?'),n=len(m),noti=m.dt_noti.sum()/1e8,
        med_lots=m.lots.median(), gross=gm, sub_pure=sub, mult=sub/gm if gm else np.nan,
        sub_nlvl=subnl, mult_nlvl=subnl/gm if gm else np.nan,
        beta=slope, alpha_nw=alpha_nw, alpha_pure=alpha_s,
        net18=gm-0.201, win=(m.spread>0).mean()*100, bp=m.bp.median(), sp=m.sp.median()))
R=pd.DataFrame(rows)
pd.set_option('display.width',260)
print(R.round(4).to_string(index=False))
