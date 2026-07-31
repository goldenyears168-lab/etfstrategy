"""Fetch TEJ EWSALE (monthly revenue) + EWPRCD (adjusted close) for the wide
top-~420 liquid equity universe, for the L1 revenue-family second-leg study.

EWSALE: d0001=當月營收, d0003=營收YoY%, annd_s=公告日 (PIT announcement date).
EWPRCD: close_adj 還原收盤 (2021+), for adjusted returns.

Writes: data/research/dashboard/tej_revfam_sale.parquet
        data/research/dashboard/tej_revfam_pb.parquet
Honest coverage: TEJ E-SHOP plan starts 2021; universe = top liquid by local db amount.
"""
from __future__ import annotations
import json, time
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
OUTD = ROOT / "data" / "research" / "dashboard"
SCRATCH = Path("/private/tmp/claude-501/-Users-jackm4-Documents-ETF-----/452df84e-ae89-4b92-9258-802727472d3b/scratchpad")
UNIVERSE = json.loads((SCRATCH/'univ420.json').read_text())


def load_key():
    for line in (ROOT/'.env').read_text().splitlines():
        line=line.strip()
        if line.startswith('TEJ_API_KEY'):
            return line.split('=',1)[1].strip().strip('"').strip("'")


def fetch(tbl, cols, since='2021-01-01'):
    import tejapi
    frames=[]; miss=[]
    for i,cid in enumerate(UNIVERSE):
        ok=False
        for attempt in range(5):
            try:
                df=tejapi.get(tbl, coid=cid, mdate={'gte':since},
                              opts={'columns':cols}, paginate=True,
                              chinese_column_name=False)
                if df is None or len(df)==0:
                    break  # genuinely empty, not a retry case
                frames.append(df); ok=True; break
            except Exception as e:
                msg=str(e)
                if 'LREQ02' in msg or '並行' in msg:  # concurrency lock -> back off & retry
                    time.sleep(1.0+attempt*0.8); continue
                print(f"  MISS {cid}: {msg[:80]}", flush=True); break
        if not ok and cid not in miss: miss.append(cid)
        if i%25==0: print(f"  [{tbl}] {i+1}/{len(UNIVERSE)} rows={sum(len(f) for f in frames)} miss={len(miss)}", flush=True)
        time.sleep(0.35)
    out=pd.concat(frames, ignore_index=True)
    return out, miss


def main():
    import tejapi
    tejapi.ApiConfig.api_key=load_key()

    print("== EWSALE monthly revenue ==", flush=True)
    sale,m1=fetch('TWN/EWSALE', ['coid','mdate','annd_s','d0001','d0003'])
    sale=sale.rename(columns={'coid':'stock_id','mdate':'period','annd_s':'annd',
                              'd0001':'rev','d0003':'rev_yoy'})
    for c in ['period','annd']:
        sale[c]=pd.to_datetime(sale[c]).dt.tz_localize(None).dt.strftime('%Y-%m-%d')
    sale.to_parquet(OUTD/'tej_revfam_sale.parquet', index=False)
    print(f"  sale: {sale['stock_id'].nunique()} stocks {len(sale)} rows miss={len(m1)}", flush=True)

    print("== EWPRCD daily close_adj ==", flush=True)
    pb,m3=fetch('TWN/EWPRCD', ['coid','mdate','close_d','close_adj'])
    pb=pb.rename(columns={'coid':'stock_id','mdate':'date'})
    pb['date']=pd.to_datetime(pb['date']).dt.tz_localize(None).dt.strftime('%Y-%m-%d')
    pb.to_parquet(OUTD/'tej_revfam_pb.parquet', index=False)
    print(f"  pb: {pb['stock_id'].nunique()} stocks {len(pb)} rows miss={len(m3)}", flush=True)


if __name__=='__main__':
    main()
