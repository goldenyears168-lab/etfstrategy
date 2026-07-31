"""Fetch TEJ EWIFINQ quarterly statement fields for L1 non-revenue factor study.
Reuses the same 90-stock liquid universe (price data already in tej_fundamental_pb.parquet).
Fields (all PIT via a0003 財報發布日):
  ac_3990 每股盈餘 EPS | ac_r103 ROE(A)稅後 | ac_r105 營業毛利率 gross margin
  ac_r106 營業利益率 operating margin | ac_r108 稅後淨利率 net margin
Writes data/research/dashboard/tej_l1_statement.parquet
"""
from __future__ import annotations
import time
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
OUTD = ROOT / "data" / "research" / "dashboard"
UNIVERSE = ['2330','2454','2603','2317','2303','2327','3037','3661','2408','2308',
 '2382','3017','2344','3443','3231','3481','6669','8046','3008','3711','2337','8299',
 '2313','2383','2345','3034','3105','2368','6488','3189','2376','2618','2409','2449',
 '4958','6274','1519','2301','2357','3665','2882','1303','2881','3653','2379','2891',
 '5274','6415','3529','6223','2002','5347','2412','3293','3533','2360','2356','2059',
 '6239','3081','2404','1101','5871','6139','2884','1590','2886','2883','1216','4938',
 '2887','3044','2885','3045','3036','2892','2890','2912','2395','4904','6505','3702',
 '6269','2880','2834','5536','5880','5876','2801','2207']

def load_key():
    for line in (ROOT/'.env').read_text().splitlines():
        if line.strip().startswith('TEJ_API_KEY'):
            return line.split('=',1)[1].strip().strip('"').strip("'")

def main():
    import tejapi
    tejapi.ApiConfig.api_key = load_key()
    cols = ['coid','mdate','a0003','ac_3990','ac_r103','ac_r105','ac_r106','ac_r108']
    frames=[]; miss=[]
    for i,cid in enumerate(UNIVERSE):
        try:
            df=tejapi.get('TWN/EWIFINQ', coid=cid, mdate={'gte':'2021-01-01'},
                          opts={'columns':cols}, paginate=True, chinese_column_name=False)
            if df is None or len(df)==0: miss.append(cid); continue
            frames.append(df)
        except Exception as e:
            miss.append(cid); print(f"  MISS {cid}: {str(e)[:70]}")
        if i%20==0: print(f"  {i+1}/{len(UNIVERSE)} rows={sum(len(f) for f in frames)}")
        time.sleep(0.3)
    out=pd.concat(frames, ignore_index=True).rename(columns={
        'coid':'stock_id','mdate':'period','a0003':'annd','ac_3990':'eps',
        'ac_r103':'roe','ac_r105':'gm','ac_r106':'opm','ac_r108':'npm'})
    for c in ['period','annd']:
        out[c]=pd.to_datetime(out[c]).dt.tz_localize(None).dt.strftime('%Y-%m-%d')
    out=out.sort_values(['stock_id','period']).reset_index(drop=True)
    out.to_parquet(OUTD/'tej_l1_statement.parquet', index=False)
    print(f"wrote {out.stock_id.nunique()} stocks {len(out)} rows miss={len(miss)} "
          f"period {out.period.min()}->{out.period.max()}")

if __name__=='__main__':
    main()
