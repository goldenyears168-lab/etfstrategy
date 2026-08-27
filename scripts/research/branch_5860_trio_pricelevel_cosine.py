"""(d)+(e) 直擊拆單：同一 stock-day 上三分點的**分價量分布**有多像？
基準 = 同一通 API 拿到的當日該檔所有其他分點兩兩配對（完美同日同檔對照）。"""
import pandas as pd, numpy as np, time, sys
from finmind_client import fetch_taiwan_stock_trading_daily_report
TRIO=['5860','5861','5862']
d=pd.read_pickle('/tmp/yy/pool_daily.pkl')
d=d[(d.tid.isin(TRIO))&(d.trade_date>='2025-01-01')].copy()
d['vol']=d.buy+d.sell
w=d.pivot_table(index=['trade_date','stock_id'],columns='tid',values='vol').dropna()
print(f'三分點同日同檔共同出現：{len(w):,} 個 stock-day（{w.index.get_level_values(0).nunique()} 日）',flush=True)
N=int(sys.argv[1]) if len(sys.argv)>1 else 60
cand=w.sample(min(N,len(w)),random_state=5)
rows=[]; t0=time.time()
for i,(dt,sid) in enumerate(cand.index):
    try:
        lv=pd.DataFrame(fetch_taiwan_stock_trading_daily_report(trade_date=dt,data_id=sid))
        for c in ('price','buy','sell'): lv[c]=pd.to_numeric(lv[c],errors='coerce')
        lv=lv.dropna(subset=['price']); lv['v']=lv.buy+lv.sell
        lv=lv[lv.v>0]
        M=lv.pivot_table(index='securities_trader_id',columns='price',values='v',aggfunc='sum').fillna(0)
        M=M[M.sum(axis=1)>0]
        if not all(t in M.index for t in TRIO): continue
        A=M.values.astype(float); nrm=np.sqrt((A*A).sum(1)); C=(A@A.T)/np.outer(nrm,nrm)
        ids=list(M.index); tot=A.sum(1); nlv=(A>0).sum(1)
        ti=[ids.index(t) for t in TRIO]
        iu,ju=np.triu_indices(len(ids),1)
        r=tot[iu]/tot[ju]; sz=np.maximum(r,1/np.where(r==0,np.nan,r))
        yy=np.array([(ids[a] in TRIO and ids[b] in TRIO) for a,b in zip(iu,ju)])
        rows.append(pd.DataFrame(dict(dt=dt,sid=sid,cos=C[iu,ju],sizerat=sz,is_yy=yy,nlv_min=np.minimum(nlv[iu],nlv[ju]),nlv_max=np.maximum(nlv[iu],nlv[ju]))))
    except Exception as e:
        print('  skip',dt,sid,type(e).__name__,flush=True)
    time.sleep(0.3)
    if i%10==9: print(f'  {i+1}/{len(cand)} {(time.time()-t0)/60:.1f}分',flush=True)
R=pd.concat(rows,ignore_index=True); R.to_pickle('/tmp/yy/de_pairs.pkl')
print(f'\n配對數 {len(R):,}（{R.dt.nunique()} 個 stock-day · 盈溢對 {R.is_yy.sum()}）',flush=True)
yyn=R[R.is_yy]
print('盈溢對的價位檔數: min med=%.0f  max med=%.0f'%(yyn.nlv_min.median(),yyn.nlv_max.median()))
lo,hi=yyn.nlv_min.quantile(.25),yyn.nlv_max.quantile(.75)
for lab,sub in [('全部',R),('量級相近(<=4x)',R[R.sizerat<=4]),
                ('價位檔數匹配(兩端都<=%d檔)'%hi, R[(R.nlv_max<=hi)]),
                ('價位檔數匹配+量級<=4x', R[(R.nlv_max<=hi)&(R.sizerat<=4)])]:
    yy=sub[sub.is_yy]; ot=sub[~sub.is_yy]
    if len(yy)<10: continue
    from scipy.stats import mannwhitneyu
    u,p=mannwhitneyu(yy.cos,ot.cos)
    print(f'--- {lab} ---')
    print(f'  盈溢對    n={len(yy):>6} cos med={yy.cos.median():.4f} mean={yy.cos.mean():.4f} >0.9={(yy.cos>0.9).mean()*100:5.1f}%')
    print(f'  其他分點對 n={len(ot):>6} cos med={ot.cos.median():.4f} mean={ot.cos.mean():.4f} >0.9={(ot.cos>0.9).mean()*100:5.1f}%')
    print(f'  Mann-Whitney p={p:.4g}  盈溢中位數在對照分布中的百分位={(ot.cos<yy.cos.median()).mean()*100:.1f}%')
