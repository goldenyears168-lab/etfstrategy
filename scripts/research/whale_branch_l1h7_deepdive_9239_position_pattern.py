"""Local analysis (Book): join mini-derived 9239 daily window + same-day all-branch
data with local stock_daily_bars close prices to classify position-building pattern
per event, and check for a dominant same-day sell-branch counterparty (crossing risk).

Inputs (already scp'd from mini):
- reports/research/branch-footprint-screen/whale_branch_l1h7_deepdive_9239_daily_window.csv
- reports/research/branch-footprint-screen/whale_branch_l1h7_deepdive_9239_sameday_allbranches.csv

Outputs:
- reports/research/branch-footprint-screen/whale_branch_l1h7_deepdive_9239_position_pattern.csv
- reports/research/branch-footprint-screen/whale_branch_l1h7_deepdive_9239_crossing_check.csv
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import pandas as pd
from stock_db import connect, DEFAULT_DB_PATH

con = connect(DEFAULT_DB_PATH)

RPT = os.path.join(ROOT, "reports/research/branch-footprint-screen")

window = pd.read_csv(os.path.join(RPT, "whale_branch_l1h7_deepdive_9239_daily_window.csv"), dtype={"stock_id": str})
sameday = pd.read_csv(os.path.join(RPT, "whale_branch_l1h7_deepdive_9239_sameday_allbranches.csv"), dtype={"stock_id": str, "securities_trader_id": str})

# --- fetch close prices for all (stock_id, trade_date) pairs needed ---
need_pairs = set(zip(window["stock_id"], window["trade_date"]))
need_pairs |= set(zip(sameday["stock_id"], sameday["signal_date"]))

price_map = {}
cur = con.cursor()
for stock_id in sorted(set(p[0] for p in need_pairs)):
    rows = cur.execute(
        "SELECT trade_date, close FROM stock_daily_bars WHERE stock_id=?", (stock_id,)
    ).fetchall()
    for r in rows:
        price_map[(stock_id, r[0] if not hasattr(r, "keys") else r["trade_date"])] = r[1] if not hasattr(r, "keys") else r["close"]

def get_close(stock_id, date):
    return price_map.get((stock_id, date))

window["close"] = [get_close(s, d) for s, d in zip(window["stock_id"], window["trade_date"])]
window["buy_amt"] = window["buy"] * window["close"]
window["sell_amt"] = window["sell"] * window["close"]
window["net_amt"] = window["net"] * window["close"]

missing_close = window["close"].isna().sum()
print(f"window rows: {len(window)}, missing close price: {missing_close}")

window.to_csv(os.path.join(RPT, "whale_branch_l1h7_deepdive_9239_daily_window_amt.csv"), index=False)

# --- classify position-building pattern per event ---
events = window[["signal_date", "stock_id"]].drop_duplicates().values.tolist()

pattern_rows = []
for sig_date, stock_id in events:
    ev = window[(window["signal_date"] == sig_date) & (window["stock_id"] == stock_id)].sort_values("offset_days")
    sig_row = ev[ev["offset_days"] == 0]
    sig_buy_amt = float(sig_row["buy_amt"].iloc[0]) if len(sig_row) else None
    sig_sell_amt = float(sig_row["sell_amt"].iloc[0]) if len(sig_row) else None

    post = ev[(ev["offset_days"] > 0) & (ev["offset_days"] <= 25)]
    pre = ev[(ev["offset_days"] < 0) & (ev["offset_days"] >= -25)]

    post_buy_amt = post["buy_amt"].sum()
    post_sell_amt = post["sell_amt"].sum()
    post_net_amt = post["net_amt"].sum()

    # next-day sell amt (offset_days == 1) as immediate-dump indicator
    next_day = ev[ev["offset_days"] == 1]
    next_day_sell_amt = float(next_day["sell_amt"].iloc[0]) if len(next_day) else 0.0

    # cumulative net position after signal (25 trading-day window), relative to signal-day buy
    if sig_buy_amt and sig_buy_amt > 0:
        next_day_sell_ratio = next_day_sell_amt / sig_buy_amt
        post_sell_ratio = post_sell_amt / sig_buy_amt
        post_net_ratio = post_net_amt / sig_buy_amt
    else:
        next_day_sell_ratio = post_sell_ratio = post_net_ratio = None

    # classification heuristic
    if next_day_sell_ratio is not None and next_day_sell_ratio >= 0.5:
        pattern = "疑似對敲/當沖 (隔日立即大量出清)"
    elif post_sell_ratio is not None and post_sell_ratio >= 0.8:
        pattern = "部分了結 (25日內大部分回吐)"
    elif post_net_ratio is not None and post_net_ratio >= -0.2:
        pattern = "持續累積/續抱 (未顯著回吐)"
    else:
        pattern = "中間型態 (部分了結)"

    pattern_rows.append({
        "signal_date": sig_date, "stock_id": stock_id,
        "sig_buy_amt": sig_buy_amt, "sig_sell_amt": sig_sell_amt,
        "next_day_sell_amt": next_day_sell_amt, "next_day_sell_ratio": next_day_sell_ratio,
        "post25_buy_amt": post_buy_amt, "post25_sell_amt": post_sell_amt, "post25_net_amt": post_net_amt,
        "post25_sell_ratio_vs_sigbuy": post_sell_ratio, "post25_net_ratio_vs_sigbuy": post_net_ratio,
        "pattern": pattern,
    })

pattern_df = pd.DataFrame(pattern_rows)
pattern_df.to_csv(os.path.join(RPT, "whale_branch_l1h7_deepdive_9239_position_pattern.csv"), index=False)
print(pattern_df[["signal_date", "stock_id", "next_day_sell_ratio", "post25_sell_ratio_vs_sigbuy", "post25_net_ratio_vs_sigbuy", "pattern"]].to_string())
print("\nPattern counts:")
print(pattern_df["pattern"].value_counts())

# --- same-day dominant sell-branch (crossing) check ---
sameday["close"] = [get_close(s, d) for s, d in zip(sameday["stock_id"], sameday["signal_date"])]
sameday["sell_amt"] = sameday["sell"] * sameday["close"]
sameday["buy_amt"] = sameday["buy"] * sameday["close"]

cross_rows = []
for sig_date, stock_id in events:
    day = sameday[(sameday["signal_date"] == sig_date) & (sameday["stock_id"] == stock_id)]
    total_sell_amt = day["sell_amt"].sum()
    total_buy_amt = day["buy_amt"].sum()
    others = day[day["securities_trader_id"] != "9239"].sort_values("sell_amt", ascending=False)
    top1 = others.iloc[0] if len(others) else None
    top1_sell_amt = float(top1["sell_amt"]) if top1 is not None else 0.0
    top1_share = top1_sell_amt / total_sell_amt if total_sell_amt else None
    is_dominant = (top1_sell_amt >= 1e8) and (top1_share is not None and top1_share >= 0.5)
    cross_rows.append({
        "signal_date": sig_date, "stock_id": stock_id,
        "total_sell_amt_all_branches": total_sell_amt,
        "top1_other_branch_id": top1["securities_trader_id"] if top1 is not None else None,
        "top1_other_branch_name": top1["securities_trader"] if top1 is not None else None,
        "top1_other_sell_amt": top1_sell_amt,
        "top1_other_sell_share": top1_share,
        "9239_buy_amt_thisday": float(day[day["securities_trader_id"] == "9239"]["buy_amt"].sum()),
        "dominant_sell_counterparty_flag": is_dominant,
    })

cross_df = pd.DataFrame(cross_rows)
cross_df.to_csv(os.path.join(RPT, "whale_branch_l1h7_deepdive_9239_crossing_check.csv"), index=False)
print("\n--- Crossing check ---")
print(cross_df[["signal_date", "stock_id", "top1_other_branch_name", "top1_other_sell_share", "dominant_sell_counterparty_flag"]].to_string())
print("\nDominant sell-counterparty flag count:", cross_df["dominant_sell_counterparty_flag"].sum(), "/", len(cross_df))
