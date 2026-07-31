"""Fetch FinMind TaiwanStockPrice Trading_money (成交金額 TWD) for the
418-stock revenue-family universe, 2021+. Drives the liquidity top-N filter,
large-vs-midsmall size split, slippage tiering, and capacity estimate for the
rev-family tradability test. Writes data/research/dashboard/tej_revfam_advalue.parquet
(stock_id, date, tval).  Honest: raw daily 成交金額, no adjustment needed (a value).
"""
from __future__ import annotations
import time
from pathlib import Path
import pandas as pd, requests

ROOT = Path(__file__).resolve().parents[3]
OUTD = ROOT / "data" / "research" / "dashboard"
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
        for attempt in range(5):
            try:
                r=requests.get(URL,params={'dataset':'TaiwanStockPrice','data_id':sid,
                    'start_date':'2021-01-01','end_date':'2026-07-31','token':tok},timeout=40)
                if r.status_code==402:
                    time.sleep(30); continue
                j=r.json(); d=j.get('data',[])
                if d:
                    df=pd.DataFrame(d)[['date','stock_id','Trading_money']].rename(
                        columns={'Trading_money':'tval'})
                    frames.append(df); ok=True
                break
            except Exception:
                time.sleep(2+attempt*2)
        if not ok: miss.append(sid)
        if i%25==0: print(f"  {i+1}/{len(UNIV)} rows={sum(len(f) for f in frames)} miss={len(miss)}",flush=True)
        time.sleep(0.2)
    out=pd.concat(frames,ignore_index=True)
    out.to_parquet(OUTD/'tej_revfam_advalue.parquet',index=False)
    print(f"DONE advalue: {out['stock_id'].nunique()} stocks {len(out)} rows miss={len(miss)} {miss[:20]}",flush=True)

if __name__=='__main__':
    main()
