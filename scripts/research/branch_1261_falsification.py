from pathlib import Path
import numpy as np, pandas as pd
from scipy import stats
DIR = Path("reports/research/chip-signal-daily-horizon")
def prep(tid,w0="2025-01-01"):
    f=DIR/f"branch_{tid}_pricelevels.pkl"
    if not f.exists(): return None
    d=pd.read_pickle(f).drop_duplicates(["stock_id","trade_date"])
    d=d[d.trade_date>=w0].copy()
    d["buy_vwap"]=d.buy_amt/d.buy_vol.replace(0,np.nan); d["sell_vwap"]=d.sell_amt/d.sell_vol.replace(0,np.nan)
    d["bl"]=d.buy_vol/1000.; d["sl"]=d.sell_vol/1000.
    d["mn"]=d[["buy_vol","sell_vol"]].min(axis=1); d["mx"]=d[["buy_vol","sell_vol"]].max(axis=1)
    d["rt"]=d.mn/d.mx.replace(0,np.nan)
    d["spread"]=(d.sell_vwap/d.buy_vwap-1)*100
    d["noti"]=d.mn*(d.buy_vwap+d.sell_vwap)/2
    d["lvl_lot"]=(d.buy_vol+d.sell_vol)/1000./d.n_lvl
    return d[d.mn>0].dropna(subset=["spread"])
print("=== 判準1 統一口徑：純當沖子群 rt>0.99 的整張率（buy&sell 皆整張）與檔內張數 CV ===")
print(f"{'分點':<7}{'n':>8}{'整張%':>8}{'=1張%':>8}{'中位張':>8}{'p90張':>8}{'眾數佔比':>9}{'檔內張CV':>10}{'n_lvl中位':>10}")
for t in ["1261","9661","8888","9225","9217","884M","981M","9B2Y","5110","9268","9800"]:
    d=prep(t)
    if d is None or len(d)<300: print(f"{t:<7} 無/不足"); continue
    p=d[d.rt>0.99]
    if len(p)<100: print(f"{t:<7} 純子群不足 n={len(p)}"); continue
    whole=(np.isclose(p.bl%1,0,atol=1e-6)&np.isclose(p.sl%1,0,atol=1e-6)).mean()*100
    b=p.bl; md=b.mode().iloc[0]
    print(f"{t:<7}{len(p):>8,}{whole:>8.1f}{(b==1).mean()*100:>8.1f}{b.median():>8.1f}"
          f"{b.quantile(.9):>8.1f}{(b==md).mean()*100:>8.1f}%{p.lvl_lot.std()/p.lvl_lot.mean():>10.2f}{p.n_lvl.median():>10.0f}")

print("\n=== 1261 純當沖子群的分割檢定（9225 式可否證設計，4 個非循環切分）===")
d=prep("1261"); pure=d[d.rt>0.99].copy()
print(f"純子群 n={len(pure):,} · {pure.trade_date.nunique()} 日 · {pure.stock_id.nunique()} 檔　"
      f"量加權毛 {(pure.spread*pure.noti).sum()/pure.noti.sum():+.4f}%")
pure["whole"]=np.isclose(pure.bl%1,0,atol=1e-6)&np.isclose(pure.sl%1,0,atol=1e-6)
pure["exact"]=pure.buy_vol==pure.sell_vol
lr=pure.bl.round().astype(int); pure["common_lot"]=lr.isin(set(lr.value_counts().head(5).index))
pure["few_lvl"]=pure.n_lvl<=pure.n_lvl.median()
def wavg(g): return (g.spread*g.noti).sum()/g.noti.sum() if g.noti.sum() else np.nan
def od(g):
    k=g.groupby("trade_date").size(); return k.var()/k.mean() if len(k)>10 else np.nan
dates=np.sort(pure.trade_date.unique()); mid=dates[len(dates)//2]
ALPHA=0.01/4
print(f"{'切分':<14}{'規格化組':>10}{'另一組':>10}{'差距':>9}{'p(MW)':>10}{'過離散(規)':>11}{'過離散(另)':>11}{'n(規)':>8}  判準")
for lab,col in [("S1 整張","whole"),("S2 價位檔數少","few_lvl"),("S3 精確平倉","exact"),("S4 高頻張數","common_lot")]:
    a,b=pure[pure[col]],pure[~pure[col]]
    if len(a)<200 or len(b)<200: print(f"  {lab} 樣本不足 {len(a)}/{len(b)}"); continue
    ma,mb=wavg(a),wavg(b); _,p=stats.mannwhitneyu(a.spread,b.spread,alternative="two-sided")
    h1,h2=pure[pure.trade_date<mid],pure[pure.trade_date>=mid]
    g1=wavg(h1[h1[col]])-wavg(h1[~h1[col]]); g2=wavg(h2[h2[col]])-wavg(h2[~h2[col]])
    oa,ob=od(a),od(b)
    ok=[abs(ma-mb)>0.15, p<ALPHA, oa<ob, np.sign(g1)==np.sign(g2) and abs(g1)>0.05 and abs(g2)>0.05]
    print(f"  {lab:<12}{ma:>+9.4f}%{mb:>+9.4f}%{ma-mb:>+8.4f}{p:>10.2e}{oa:>11.2f}{ob:>11.2f}{len(a):>8,}  "
          +" ".join(f"({c}){'✓' if o else '✗'}" for c,o in zip("abcd",ok))
          +f" 前半{g1:+.3f}/後半{g2:+.3f} → {'★拒絕H0' if all(ok) else '接受H0'}")
