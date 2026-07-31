"""Fetch FinMind TaiwanStockPriceAdj (還原收盤) for the revenue-family universe.
TEJ daily quota was exhausted on EWPRCD; FinMind adjusted price is the clean
還原價 fallback for returns. One call per stock (full history 2021+).
Writes data/research/dashboard/tej_revfam_pb.parquet (stock_id,date,close_adj).
"""
from __future__ import annotations
import json, time
from pathlib import Path
import pandas as pd, requests

ROOT = Path(__file__).resolve().parents[3]
OUTD = ROOT / "data" / "research" / "dashboard"
SCRATCH = Path("/private/tmp/claude-501/-Users-jackm4-Documents-ETF-----/452df84e-ae89-4b92-9258-802727472d3b/scratchpad")
# universe = stocks present in the fetched EWSALE parquet (they have revenue signal)
sale = pd.read_parquet(OUTD/'tej_revfam_sale.parquet')
UNIV = sorted(sale['stock_id'].astype(str).unique())

def token():
    for line in (ROOT/'.env').read_text().splitlines():
        if line.startswith('FINMIND_TOKEN'):
            return line.split('=',1)[1].strip().strip('"').strip("'")

URL='https://api.finmindtrade.com/api/v4/data'
def main():
    tok=token(); frames=[]; miss=[]
    for i,sid in enumerate(UNIV):
        ok=False
        for attempt in range(4):
            try:
                r=requests.get(URL,params={'dataset':'TaiwanStockPriceAdj','data_id':sid,
                    'start_date':'2021-01-01','end_date':'2026-07-31','token':tok},timeout=40)
                if r.status_code==402:
                    time.sleep(30); continue
                j=r.json(); d=j.get('data',[])
                if d:
                    df=pd.DataFrame(d)[['date','stock_id','close']].rename(columns={'close':'close_adj'})
                    frames.append(df); ok=True
                break
            except Exception as e:
                time.sleep(2+attempt*2)
        if not ok: miss.append(sid)
        if i%25==0: print(f"  {i+1}/{len(UNIV)} rows={sum(len(f) for f in frames)} miss={len(miss)}",flush=True)
        time.sleep(0.2)
    out=pd.concat(frames,ignore_index=True)
    out.to_parquet(OUTD/'tej_revfam_pb.parquet',index=False)
    print(f"DONE pb: {out['stock_id'].nunique()} stocks {len(out)} rows miss={len(miss)} {miss[:20]}",flush=True)

if __name__=='__main__':
    main()
