"""Deep-dive probe for branch 9239: name lookup + per-stock daily buy/sell window
around each of the 22 signal events, for position-building / crossing analysis.
Run on mini (has full branch coverage). Writes CSV to reports/research/branch-footprint-screen/.
"""
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import pandas as pd
from stock_db import connect, DEFAULT_DB_PATH

con = connect(DEFAULT_DB_PATH)

# 1. Name
rows = con.execute(
    "SELECT DISTINCT securities_trader FROM stock_broker_branch_daily WHERE securities_trader_id='9239'"
).fetchall()
names = [dict(r)["securities_trader"] if hasattr(r, "keys") else r[0] for r in rows]
print("NAME:", names)

r2 = con.execute(
    "SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM stock_broker_branch_daily "
    "WHERE securities_trader_id='9239' AND source='finmind'"
).fetchone()
print("RANGE:", r2[0], r2[1], r2[2])

# 2. Events list (from Book-local CSV, re-read here isn't available on mini; hardcode signal/stock pairs)
events = [
    ("2024-11-29", "5871"), ("2025-08-19", "8358"), ("2025-09-08", "3376"),
    ("2025-10-08", "4441"), ("2025-11-07", "5328"), ("2025-12-23", "6517"),
    ("2026-01-05", "8105"), ("2026-01-08", "1714"), ("2026-03-11", "3491"),
    ("2026-03-25", "3167"), ("2026-03-26", "2337"), ("2026-04-02", "6584"),
    ("2026-04-14", "3016"), ("2026-05-05", "3042"), ("2026-05-15", "4979"),
    ("2026-05-21", "6488"), ("2026-06-08", "4919"), ("2026-06-08", "6770"),
    ("2026-06-16", "5468"), ("2026-06-30", "3532"), ("2026-06-30", "1444"),
    ("2026-07-02", "1447"),
]

out_rows = []
for sig_date, stock_id in events:
    q = """
        SELECT trade_date, buy, sell, net
        FROM stock_broker_branch_daily
        WHERE securities_trader_id='9239' AND stock_id=? AND source='finmind'
        ORDER BY trade_date
    """
    df = pd.read_sql_query(q, con, params=(stock_id,))
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    sig = pd.to_datetime(sig_date)
    window = df[(df["trade_date"] >= sig - pd.Timedelta(days=25)) & (df["trade_date"] <= sig + pd.Timedelta(days=25))]
    for _, r in window.iterrows():
        out_rows.append({
            "signal_date": sig_date, "stock_id": stock_id,
            "trade_date": r["trade_date"].strftime("%Y-%m-%d"),
            "buy": r["buy"], "sell": r["sell"], "net": r["net"],
            "offset_days": (r["trade_date"] - sig).days,
        })
    # also: how many total distinct trading days did 9239 ever trade this stock (all history)?
    total_days = len(df)
    total_net = df["net"].sum()
    print(f"{stock_id} sig={sig_date}: total_days_ever_traded={total_days}, lifetime_net_shares={total_net}")

out = pd.DataFrame(out_rows)
out_path = os.path.join(ROOT, "reports/research/branch-footprint-screen/whale_branch_l1h7_deepdive_9239_daily_window.csv")
out.to_csv(out_path, index=False)
print("WROTE", out_path, len(out))

# 3. Also check for crossing pairs (buy 9239 same day/stock as heavy sell from another branch, or vice versa)
q2 = """
    SELECT stock_id, trade_date, securities_trader_id, securities_trader, buy, sell, net
    FROM stock_broker_branch_daily
    WHERE stock_id IN ({}) AND trade_date IN ({}) AND source='finmind' AND securities_trader_id != '9239'
    ORDER BY stock_id, trade_date, net DESC
""".format(
    ",".join(f"'{s}'" for _, s in events),
    ",".join(f"'{d}'" for d, _ in events),
)
# This is a loose join (not date-stock paired) just to eyeball; do it properly per event instead.
same_day_rows = []
for sig_date, stock_id in events:
    q = """
        SELECT securities_trader_id, securities_trader, buy, sell, net
        FROM stock_broker_branch_daily
        WHERE stock_id=? AND trade_date=? AND source='finmind'
        ORDER BY net DESC
    """
    entry_date_guess = sig_date  # signal date itself; entry is next trading day but let's check signal day too
    df2 = pd.read_sql_query(q, con, params=(stock_id, sig_date))
    df2["signal_date"] = sig_date
    df2["stock_id"] = stock_id
    same_day_rows.append(df2)

sd = pd.concat(same_day_rows, ignore_index=True) if same_day_rows else pd.DataFrame()
sd_path = os.path.join(ROOT, "reports/research/branch-footprint-screen/whale_branch_l1h7_deepdive_9239_sameday_allbranches.csv")
sd.to_csv(sd_path, index=False)
print("WROTE", sd_path, len(sd))
