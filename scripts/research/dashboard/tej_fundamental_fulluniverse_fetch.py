"""Fetch TEJ EWSALE (月營收 PIT) + EWPRCD (還原價) for a BROAD liquidity-ranked
universe (top ~300 by turnover) to validate rev_yoy_3m beyond the original 90.

Budget-aware: EWPRCD (daily) is the row hog. Fetch EWPRCD first in turnover order,
stopping gracefully before the TEJ daily row cap; then EWSALE only for covered stocks.
Honest coverage recorded to a manifest CSV.

Writes (all under data/research/dashboard/):
  tej_fund_full_prcd.parquet   daily close_adj/close_d/pb_ratio/volume
  tej_fund_full_sale.parquet   monthly rev_yoy + annd (公告日 PIT)
  tej_fund_full_manifest.csv   per-stock coverage / stop reason
"""
from __future__ import annotations
import time, sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
OUTD = ROOT / "data" / "research" / "dashboard"
SCRATCH = Path("/private/tmp/claude-501/-Users-jackm4-Documents-ETF-----/452df84e-ae89-4b92-9258-802727472d3b/scratchpad")
ROW_SAFE_CAP = 470_000   # stop EWPRCD before TEJ daily 500k row cap
SINCE = '2021-01-01'


def load_key():
    for line in (ROOT/'.env').read_text().splitlines():
        line = line.strip()
        if line.startswith('TEJ_API_KEY'):
            return line.split('=', 1)[1].strip().strip('"').strip("'")


def main():
    import tejapi
    tejapi.ApiConfig.api_key = load_key()

    univ = [l.strip() for l in (SCRATCH/'univ300.txt').read_text().splitlines() if l.strip()]
    print(f"universe {len(univ)} stocks (turnover-ranked)")

    def today_rows():
        try:
            return tejapi.ApiConfig.info()['todayRows']
        except Exception:
            return None

    start_rows = today_rows()
    print(f"TEJ todayRows at start: {start_rows}")

    # ---- EWPRCD first (row-limited) ----
    prcd_frames = []; manifest = []
    covered = []
    for i, cid in enumerate(univ):
        tr = today_rows()
        if tr is not None and tr >= ROW_SAFE_CAP:
            print(f"STOP EWPRCD at {i}/{len(univ)}: todayRows={tr} >= cap {ROW_SAFE_CAP}")
            for c in univ[i:]:
                manifest.append(dict(stock_id=c, prcd_rows=0, stop='row_cap'))
            break
        try:
            df = tejapi.get('TWN/EWPRCD', coid=cid, mdate={'gte': SINCE},
                            opts={'columns': ['coid','mdate','close_d','close_adj','pb_ratio','volume']},
                            paginate=True, chinese_column_name=False)
            n = 0 if df is None else len(df)
            if n > 0:
                prcd_frames.append(df); covered.append(cid)
            manifest.append(dict(stock_id=cid, prcd_rows=n, stop=''))
        except Exception as e:
            manifest.append(dict(stock_id=cid, prcd_rows=0, stop=f'err:{str(e)[:40]}'))
            print(f"  MISS prcd {cid}: {str(e)[:60]}")
        if i % 20 == 0:
            print(f"  [EWPRCD] {i+1}/{len(univ)} covered={len(covered)} rows_today={tr}")
        time.sleep(0.3)

    prcd = pd.concat(prcd_frames, ignore_index=True)
    prcd = prcd.rename(columns={'coid':'stock_id','mdate':'date'})
    prcd['date'] = pd.to_datetime(prcd['date']).dt.tz_localize(None).dt.strftime('%Y-%m-%d')
    prcd.to_parquet(OUTD/'tej_fund_full_prcd.parquet', index=False)
    print(f"EWPRCD: {prcd['stock_id'].nunique()} stocks {len(prcd)} rows")

    # ---- EWSALE for covered stocks (cheap) ----
    sale_frames = []
    for i, cid in enumerate(covered):
        try:
            df = tejapi.get('TWN/EWSALE', coid=cid, mdate={'gte': SINCE},
                            opts={'columns': ['coid','mdate','annd_s','d0001','d0003']},
                            paginate=True, chinese_column_name=False)
            if df is not None and len(df) > 0:
                sale_frames.append(df)
        except Exception as e:
            print(f"  MISS sale {cid}: {str(e)[:60]}")
        if i % 40 == 0:
            print(f"  [EWSALE] {i+1}/{len(covered)} rows_today={today_rows()}")
        time.sleep(0.3)

    sale = pd.concat(sale_frames, ignore_index=True)
    sale = sale.rename(columns={'coid':'stock_id','mdate':'period','annd_s':'annd',
                                'd0001':'rev','d0003':'rev_yoy'})
    for c in ['period','annd']:
        sale[c] = pd.to_datetime(sale[c]).dt.tz_localize(None).dt.strftime('%Y-%m-%d')
    sale.to_parquet(OUTD/'tej_fund_full_sale.parquet', index=False)
    print(f"EWSALE: {sale['stock_id'].nunique()} stocks {len(sale)} rows")

    pd.DataFrame(manifest).to_csv(OUTD/'tej_fund_full_manifest.csv', index=False)
    end_rows = today_rows()
    print(f"TEJ todayRows end: {end_rows}  (used ~{(end_rows or 0)-(start_rows or 0)})")
    print(f"FINAL covered universe (price+sale): {len(set(covered)&set(sale['stock_id'].unique()))}")


if __name__ == '__main__':
    main()
