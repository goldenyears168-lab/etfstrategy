"""Fetch TEJ EWPRCD adjusted (還原) prices for the RS liquid universe (2021+).

TEJ EWPRCD columns: coid, mdate, open_d/high_d/low_d/close_d (raw),
  open_adj/high_adj/low_adj/close_adj (fully back-adjusted for div+split),
  cdiv_ratio (cumulative cash-dividend adjustment ratio), volume, pe/pb.

Writes: data/research/dashboard/tej_cleanprice_data.parquet
Honest coverage note: TEJ plan data starts 2021 only.
"""
from __future__ import annotations
import time
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data" / "research" / "dashboard" / "tej_cleanprice_data.parquet"

UNIVERSE = ['2330','2454','2603','2317','2303','2327','3037','3661','2408','2308',
 '2382','3017','2344','3443','3231','3481','6669','8046','3008','3711','2337','8299',
 '2313','2383','2345','3034','3105','2368','6488','3189','2376','2618','2409','2449',
 '4958','6274','1519','2301','2357','3665','2882','1303','2881','3653','2379','2891',
 '5274','6415','3529','6223','2002','5347','2412','3293','3533','2360','2356','2059',
 '6239','3081','2404','1101','5871','6139','2884','1590','2886','2883','1216','4938',
 '2887','3044','2885','3045','3036','2892','2890','2912','2395','4904','6505','3702',
 '6269','2880','2834','5536','5880','5876','2801','2207']

def load_key():
    env={}
    for line in (ROOT/'.env').read_text().splitlines():
        line=line.strip()
        if not line or line.startswith('#') or '=' not in line: continue
        k,v=line.split('=',1); env[k.strip()]=v.strip().strip('"').strip("'")
    return env['TEJ_API_KEY']

def main():
    import tejapi
    tejapi.ApiConfig.api_key = load_key()
    frames=[]; miss=[]
    for i,cid in enumerate(UNIVERSE):
        try:
            df=tejapi.get('TWN/EWPRCD', coid=cid, mdate={'gte':'2021-01-01'},
                          opts={'columns':['coid','mdate','close_d','close_adj',
                                           'cdiv_ratio','volume']}, paginate=True)
            if df is None or len(df)==0:
                miss.append(cid); continue
            frames.append(df)
        except Exception as e:
            miss.append(cid); print(f"  MISS {cid}: {e}")
        if i%10==0: print(f"  {i+1}/{len(UNIVERSE)} {cid} rows_so_far={sum(len(f) for f in frames)}")
        time.sleep(0.3)
    all_df=pd.concat(frames, ignore_index=True)
    all_df['mdate']=pd.to_datetime(all_df['mdate']).dt.tz_localize(None).dt.strftime('%Y-%m-%d')
    all_df=all_df.rename(columns={'coid':'stock_id','mdate':'date'})
    all_df.to_parquet(OUT, index=False)
    print(f"\nfetched {all_df['stock_id'].nunique()} stocks, {len(all_df)} rows -> {OUT}")
    print(f"date range {all_df['date'].min()} -> {all_df['date'].max()}")
    if miss: print(f"MISSING ({len(miss)}): {miss}")

if __name__=='__main__':
    main()
