from pathlib import Path
import numpy as np, pandas as pd
DIR=Path("reports/research/chip-signal-daily-horizon")
m=pd.read_pickle(DIR/"branch_1261_joined.pkl")
m=m[m.dt_sh>0].dropna(subset=["buy_vwap","sell_vwap","spread"]).copy()
m["bl"]=m.buy_vol/1000.; m["sl"]=m.sell_vol/1000.
m["lvl_lot"]=(m.buy_vol+m.sell_vol)/1000./m.n_lvl
def row(lab,d):
    v=d.dropna(subset=["buy_pos","sell_pos"]); v=v[v.buy_pos.between(-.1,1.1)&v.sell_pos.between(-.1,1.1)]
    per=d.groupby("trade_date").size(); g,n=d.dt_pnl.sum(),d.dt_noti.sum()
    whole=(np.isclose(d.bl%1,0,atol=1e-6)&np.isclose(d.sl%1,0,atol=1e-6)).mean()*100
    return dict(lab=lab,n=len(d),days=d.trade_date.nunique(),stocks=d.stock_id.nunique(),
        perday=per.mean(),od=per.var()/per.mean(),rt=d.rt.median(),part=d.part.median()*100,
        noti=n/1e8,gross=g/n*100,gross_yi=g/1e8,win=(d.spread>0).mean()*100,
        bp=v.buy_pos.median(),sp=v.sell_pos.median(),whole=whole,
        cv=d.lvl_lot.std()/d.lvl_lot.mean(),net18=(g-n*0.00201)/1e8)
rows=[row("全分點",m),row("rt>0.95 純當沖",m[m.rt>0.95]),row("rt>0.99",m[m.rt>0.99]),
      row("rt<0.3 純方向",m[m.rt<0.3]),row("當沖<=2張",m[m.dt_lot<=2]),
      row("rt>.95&<=2張",m[(m.rt>0.95)&(m.dt_lot<=2)]),row("當沖>20張",m[m.dt_lot>20]),
      row("名目P99+",m[m.dt_noti>=m.dt_noti.quantile(.99)])]
h=f"{'子群':<16}{'n':>7}{'檔':>6}{'日均':>7}{'過離散':>7}{'當沖度':>7}{'參與%':>7}{'名目億':>8}{'毛%':>9}{'毛額億':>8}{'勝率':>7}{'買位':>7}{'賣位':>7}{'整張%':>7}{'檔內CV':>8}{'淨1.8折億':>10}"
print(h); print("-"*len(h))
for r in rows:
    print(f"{r['lab']:<16}{r['n']:>7,}{r['stocks']:>6}{r['perday']:>7.1f}{r['od']:>7.2f}{r['rt']:>7.2f}"
          f"{r['part']:>7.2f}{r['noti']:>8.1f}{r['gross']:>+9.4f}{r['gross_yi']:>+8.2f}{r['win']:>6.1f}%"
          f"{r['bp']:>7.3f}{r['sp']:>7.3f}{r['whole']:>7.1f}{r['cv']:>8.2f}{r['net18']:>+10.2f}")
