import pandas as pd
from pathlib import Path
ROOT = Path("/Users/jackm4/Documents/ETF/股票研究")
OUT = ROOT / "reports/research/branch-footprint-screen"

core9 = ["3189","2344","2408","8358","8046","4958","1303","1815","3006"]
names = {"3189":"景碩","2344":"華邦電","2408":"南亞科","8358":"金居","8046":"南電",
         "4958":"臻鼎-KY","1303":"南亞","1815":"富喬","3006":"晶豪科"}

naive = pd.read_csv(OUT/"whale_9661_all_positions.csv", dtype={"stock_id":str}).set_index("stock_id")
raw = pd.read_csv(OUT/"whale_9661_all_stocks_RAW_first_dates.csv", dtype={"stock_id":str}).set_index("stock_id")

rows = []
for sid in core9:
    naive_fd = naive.loc[sid, "first_date"]
    raw_fd = raw.loc[sid, "first_date_raw"]
    n_raw = int(raw.loc[sid, "n_raw"])
    n_naive = int(naive.loc[sid, "n_trade_days"])
    dropped = n_raw - n_naive
    pct_dropped = round(dropped/n_raw*100, 1) if n_raw else None
    mismatch_from_join = naive_fd != raw_fd
    rows.append({
        "stock_id": sid, "stock_name": names[sid],
        "first_date_naive_pricejoin": naive_fd,
        "first_date_RAW_no_join": raw_fd,
        "n_trading_days_RAW": n_raw,
        "n_trading_days_pricejoin": n_naive,
        "days_dropped_by_pricejoin_pct": pct_dropped,
        "pricejoin_gave_false_later_inception": mismatch_from_join,
        "classification": "斷點假象（真實起點左設限於2021-06-28~06-30，實際更早起點未知）",
    })

out = pd.DataFrame(rows)
out.to_csv(OUT/"whale_9661_inception_date_classification_CORRECTED.csv", index=False)
print(out.to_string(index=False))

# universe-wide summary
raw_all = raw.reset_index()
raw_all["first_date_raw_dt"] = pd.to_datetime(raw_all["first_date_raw"])
window = raw_all[(raw_all["first_date_raw_dt"]>="2021-06-25")&(raw_all["first_date_raw_dt"]<="2021-07-10")]
print(f"\nTotal stocks ever traded by 9661: {len(raw_all)}")
print(f"Stocks with RAW first_date in 2021-06-25~07-10 artifact window: {len(window)} ({len(window)/len(raw_all)*100:.1f}%)")
