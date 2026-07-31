import pandas as pd, numpy as np, sqlite3
OUT='data/research/dashboard/asquith/'
# ownership
h1=pd.read_parquet('data/research/dashboard/holder_concentration_data.parquet')
h2=pd.read_parquet('data/research/dashboard/holder_concentration_midsmall_data.parquet')
h=pd.concat([h1,h2],ignore_index=True)
big=h[h.HoldingSharesLevel=='more than 1,000,001'][['date','stock_id','percent']].rename(columns={'percent':'own_1000'})
big_lvls=['400,001-600,000','600,001-800,000','800,001-1,000,000','more than 1,000,001']
b400=h[h.HoldingSharesLevel.isin(big_lvls)].groupby(['date','stock_id'],as_index=False)['percent'].sum().rename(columns={'percent':'own_400'})
tot=h[h.HoldingSharesLevel=='total'][['date','stock_id','unit']].rename(columns={'unit':'shares_out'})
own=big.merge(b400,on=['date','stock_id']).merge(tot,on=['date','stock_id'])
own['date']=pd.to_datetime(own['date'])

con=sqlite3.connect('data/replica/stocks.db')
ids=tuple(sorted(own.stock_id.unique()))
mg=pd.read_sql(f"select stock_id,trade_date,short_balance from stock_margin_daily where stock_id in {ids}",con)
ld=pd.read_sql(f"select stock_id,trade_date,lending_balance from stock_lending_daily where stock_id in {ids}",con)
rawcl=pd.read_sql(f"select stock_id,trade_date,close from stock_daily_bars where stock_id in {ids}",con)
con.close()
mg['trade_date']=pd.to_datetime(mg.trade_date); ld['trade_date']=pd.to_datetime(ld.trade_date); rawcl['trade_date']=pd.to_datetime(rawcl.trade_date)

# clean adjusted price (TEJ)
tp=pd.read_parquet('data/research/dashboard/tej_revfam_pb.parquet').rename(columns={'date':'trade_date','close_adj':'px'})
tp['trade_date']=pd.to_datetime(tp.trade_date)
tp=tp[tp.stock_id.isin(own.stock_id.unique())].sort_values(['stock_id','trade_date']).reset_index(drop=True)
tp['tidx']=tp.groupby('stock_id').cumcount()
idxmap=tp.set_index(['stock_id','tidx'])['px']

def asof(left, right, keep):
    out=[]
    for sid,g in right.groupby('stock_id'):
        l=left[left.stock_id==sid]
        if l.empty: continue
        m=pd.merge_asof(l.sort_values('date'), g.sort_values('trade_date'),
                        left_on='date', right_on='trade_date', by='stock_id', direction='backward')
        out.append(m)
    return pd.concat(out,ignore_index=True)

panel=asof(own, mg, None).drop(columns=['trade_date'])
panel=asof(panel, ld, None).drop(columns=['trade_date'])
panel=asof(panel, rawcl.rename(columns={'close':'cl0'}), None).drop(columns=['trade_date'])
panel=asof(panel, tp.rename(columns={'px':'px0','tidx':'i0'}), None)  # keep trade_date as px date
panel=panel.rename(columns={'trade_date':'pxdate'})

def fwd(r,k):
    try: return idxmap.loc[(r.stock_id,int(r.i0)+k)]/r.px0-1
    except: return np.nan
def past(r,k):
    try: return r.px0/idxmap.loc[(r.stock_id,int(r.i0)-k)]-1
    except: return np.nan
for k in (5,10,20): panel[f'fwd{k}']=panel.apply(lambda r:fwd(r,k),axis=1)
panel['mom20']=panel.apply(lambda r:past(r,20),axis=1)
panel['mom60']=panel.apply(lambda r:past(r,60),axis=1)
panel['si_margin']=panel.short_balance*1000/panel.shares_out
panel['si_lend']=panel.lending_balance*1000/panel.shares_out
panel['si_total']=(panel.short_balance.fillna(0)+panel.lending_balance.fillna(0))*1000/panel.shares_out
panel['mktcap']=panel.shares_out*panel.cl0
panel['size']=np.log(panel.mktcap)
panel=panel[panel.px0.notna()]
panel.to_parquet(OUT+'panel.parquet')
print('PANEL',panel.shape,'stocks',panel.stock_id.nunique(),'weeks',panel.date.nunique())
print('fwd5 cov',panel.fwd5.notna().mean(),'si_margin cov',panel.si_margin.notna().mean(),'si_lend cov',panel.si_lend.notna().mean())
print(panel[['si_margin','si_lend','si_total','own_1000','own_400','fwd5','fwd20','mom20','size']].describe().to_string())
