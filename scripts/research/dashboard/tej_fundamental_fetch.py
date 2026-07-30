"""Fetch TEJ fundamental data for the 90-stock liquid universe (2021+).

Three sources, all PIT-clean via announcement dates:
  - EWSALE  月營收: d0001=當月營收, d0002=去年同月, d0003=營收YoY%, annd_s=公告日
  - EWIFINQ 財報季: ac_r103=ROE(A)稅後, ac_200d=每股淨值BVPS, ac_r401=營收成長率,
                    ac_3990=EPS, a0003=財報發布日(PIT)
  - EWPRCD  pb_ratio 交易所股價淨值比 (daily, PIT) + close_adj 還原收盤

Writes: data/research/dashboard/tej_fundamental_sale.parquet
        data/research/dashboard/tej_fundamental_fin.parquet
        data/research/dashboard/tej_fundamental_pb.parquet
Honest coverage: TEJ E-SHOP plan data starts 2021 only -> OOS window is thin.
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
        line=line.strip()
        if line.startswith('TEJ_API_KEY'):
            return line.split('=',1)[1].strip().strip('"').strip("'")


def fetch(tbl, cols, since='2021-01-01'):
    import tejapi
    frames=[]; miss=[]
    for i,cid in enumerate(UNIVERSE):
        try:
            df=tejapi.get(tbl, coid=cid, mdate={'gte':since},
                          opts={'columns':cols}, paginate=True,
                          chinese_column_name=False)
            if df is None or len(df)==0:
                miss.append(cid); continue
            frames.append(df)
        except Exception as e:
            miss.append(cid); print(f"  MISS {cid}: {str(e)[:80]}")
        if i%20==0: print(f"  [{tbl}] {i+1}/{len(UNIVERSE)} rows={sum(len(f) for f in frames)}")
        time.sleep(0.3)
    out=pd.concat(frames, ignore_index=True)
    return out, miss


def main():
    import tejapi
    tejapi.ApiConfig.api_key=load_key()

    print("== EWSALE monthly revenue ==")
    sale,m1=fetch('TWN/EWSALE', ['coid','mdate','annd_s','d0001','d0002','d0003'])
    sale=sale.rename(columns={'coid':'stock_id','mdate':'period','annd_s':'annd',
                              'd0001':'rev','d0002':'rev_yoy_base','d0003':'rev_yoy'})
    for c in ['period','annd']:
        sale[c]=pd.to_datetime(sale[c]).dt.tz_localize(None).dt.strftime('%Y-%m-%d')
    sale.to_parquet(OUTD/'tej_fundamental_sale.parquet', index=False)
    print(f"  sale: {sale['stock_id'].nunique()} stocks {len(sale)} rows miss={len(m1)}")

    print("== EWIFINQ quarterly financials ==")
    fin,m2=fetch('TWN/EWIFINQ', ['coid','mdate','a0003','ac_3990','ac_r103',
                                 'ac_200d','ac_r401'])
    fin=fin.rename(columns={'coid':'stock_id','mdate':'period','a0003':'annd',
                            'ac_3990':'eps','ac_r103':'roe','ac_200d':'bvps',
                            'ac_r401':'rev_growth'})
    for c in ['period','annd']:
        fin[c]=pd.to_datetime(fin[c]).dt.tz_localize(None).dt.strftime('%Y-%m-%d')
    fin.to_parquet(OUTD/'tej_fundamental_fin.parquet', index=False)
    print(f"  fin: {fin['stock_id'].nunique()} stocks {len(fin)} rows miss={len(m2)}")

    print("== EWPRCD daily pb_ratio + close_adj ==")
    pb,m3=fetch('TWN/EWPRCD', ['coid','mdate','close_d','close_adj','pb_ratio','pe_ratio'])
    pb=pb.rename(columns={'coid':'stock_id','mdate':'date'})
    pb['date']=pd.to_datetime(pb['date']).dt.tz_localize(None).dt.strftime('%Y-%m-%d')
    pb.to_parquet(OUTD/'tej_fundamental_pb.parquet', index=False)
    print(f"  pb: {pb['stock_id'].nunique()} stocks {len(pb)} rows miss={len(m3)}")


if __name__=='__main__':
    main()
