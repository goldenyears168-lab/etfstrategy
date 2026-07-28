import sys
from pathlib import Path
ROOT = Path("/Users/jackm4/Documents/ETF/股票研究")
sys.path.insert(0, str(ROOT / "src"))
import pandas as pd
from stock_db import connect, DEFAULT_DB_PATH

conn = connect(DEFAULT_DB_PATH)
q = """SELECT stock_id, MIN(trade_date) first_date_raw, MAX(trade_date) last_date_raw, COUNT(*) n_raw
       FROM stock_broker_branch_daily
       WHERE securities_trader_id='9661' AND source='finmind'
       GROUP BY stock_id"""
raw = pd.read_sql_query(q, conn)
conn.close()
raw.to_csv(ROOT / "reports/research/branch-footprint-screen/whale_9661_all_stocks_RAW_first_dates.csv", index=False)
print("total stocks:", len(raw))
raw['first_date_raw_dt'] = pd.to_datetime(raw['first_date_raw'])
window = raw[(raw['first_date_raw_dt']>='2021-06-25')&(raw['first_date_raw_dt']<='2021-07-10')]
print("stocks with RAW first_date in 2021-06-25~2021-07-10 window:", len(window))
core9 = ['3189','2344','2408','8358','8046','4958','1303','1815','3006']
print("core9 raw first dates:")
print(raw[raw['stock_id'].isin(core9)][['stock_id','first_date_raw','n_raw']].to_string(index=False))
print()
print("value counts of exact raw first_date (top 15):")
print(raw['first_date_raw'].value_counts().head(15))
