import sys
from pathlib import Path
ROOT = Path("/Users/jackm4/Documents/ETF/股票研究")
sys.path.insert(0, str(ROOT / "src"))
import pandas as pd
import numpy as np
from stock_db import connect, DEFAULT_DB_PATH

TRADER_ID = "9661"
STOCKS = ["3189","2344","2408","8358","8046","4958","1303","1815","3006"]
NAMES = {"3189":"景碩","2344":"華邦電","2408":"南亞科","8358":"金居","8046":"南電",
         "4958":"臻鼎-KY","1303":"南亞","1815":"富喬","3006":"晶豪科"}

conn = connect(DEFAULT_DB_PATH)
rows = []
for sid in STOCKS:
    q = """SELECT trade_date, buy, sell, net FROM stock_broker_branch_daily
           WHERE securities_trader_id=? AND stock_id=? AND source='finmind' ORDER BY trade_date"""
    g = pd.read_sql_query(q, conn, params=(TRADER_ID, sid))
    if g.empty:
        continue
    g["cum_net_shares"] = g["net"].cumsum()
    both = g[(g["buy"]>0)&(g["sell"]>0)].copy()
    if len(both):
        both["mismatch_ratio"] = (both["buy"]-both["sell"]).abs()/(both["buy"]+both["sell"])
        mean_mm = float(both["mismatch_ratio"].mean())
        med_mm = float(both["mismatch_ratio"].median())
        pct_lt1 = float((both["mismatch_ratio"]<0.01).mean()*100)
    else:
        mean_mm=med_mm=pct_lt1=np.nan
    pct_dual = len(both)/len(g)*100 if len(g) else np.nan
    cum = g["cum_net_shares"]
    peak_abs = max(abs(cum.max()), abs(cum.min())) if len(cum) else 0
    final_cum = cum.iloc[-1] if len(cum) else 0
    gross = (g["buy"]+g["sell"]).sum()
    pos_over_gross = peak_abs/gross*100 if gross else np.nan
    final_over_gross = abs(final_cum)/gross*100 if gross else np.nan
    rows.append({
        "stock_id": sid, "stock_name": NAMES.get(sid,""),
        "n_trading_days_RAW": len(g), "pct_dual_side_days": round(pct_dual,1),
        "mean_mismatch_pct": round(mean_mm*100,2) if not np.isnan(mean_mm) else None,
        "median_mismatch_pct": round(med_mm*100,2) if not np.isnan(med_mm) else None,
        "pct_dualside_lt1pct": round(pct_lt1,1) if not np.isnan(pct_lt1) else None,
        "peak_pos_over_gross_pct": round(pos_over_gross,2) if not np.isnan(pos_over_gross) else None,
        "final_pos_over_gross_pct": round(final_over_gross,2) if not np.isnan(final_over_gross) else None,
    })

out = pd.DataFrame(rows)
print(out.to_string(index=False))
out.to_csv(ROOT / "reports/research/branch-footprint-screen/whale_9661_a_marketmaking_check_RAW_nojoinfix.csv", index=False)

# compare n_trading_days vs old joined version to quantify data loss
old = pd.read_csv(ROOT / "reports/research/branch-footprint-screen/whale_9661_a_marketmaking_check.csv", dtype={"stock_id":str})
merged = out.merge(old[["stock_id","n_trading_days"]], on="stock_id")
merged["days_dropped_by_price_join"] = merged["n_trading_days_RAW"] - merged["n_trading_days"]
merged["pct_dropped"] = (merged["days_dropped_by_price_join"]/merged["n_trading_days_RAW"]*100).round(1)
print("\n=== Data loss from INNER JOIN with stock_daily_bars ===")
print(merged[["stock_id","stock_name","n_trading_days_RAW","n_trading_days","days_dropped_by_price_join","pct_dropped"]].to_string(index=False))
merged.to_csv(ROOT / "reports/research/branch-footprint-screen/whale_9661_a_join_dataloss_check.csv", index=False)
conn.close()
