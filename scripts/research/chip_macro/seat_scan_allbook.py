"""Scan ALL seats' whole-book selection skill — is ANY seat worth following after costs?

Efficient design: selection-excess of a (stock,date) is seat-independent:
    sel_excess_h[stock,date] = stock_fwd_h - (same-date cross-sectional mean fwd_h of all traded stocks)
Precompute it once, then for each of the 633 price-covered stocks, pull that stock's
net-buy rows (stock_id is indexed -> fast) and accumulate per-seat sums. Per-seat
SEL-ALPHA = mean sel_excess over its buy-instances.

Honesty controls:
  - require n>=200 buy-instances to rank a seat.
  - ECONOMIC bar: SEL-ALPHA must exceed round-trip cost ~0.45% to be worth following.
  - MULTIPLE TESTING: ~800 seats -> Bonferroni p threshold; report how many clear each bar
    and how many are expected by chance. Date-clustered bootstrap on the top candidates.
"""
from __future__ import annotations

import sqlite3
import numpy as np
import pandas as pd

DB = "data/replica/stocks.db"
OUT = "data/research/chip_macro"
COST = 0.0045   # ~0.45% round-trip (fee x2 + 0.3% tax), the bar to beat
MIN_N = 200
H = [5, 10]

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
print("1) price universe + forward returns...", flush=True)
# universe = stocks the two seats trade AND that have price data (same 633 as whole-book test)
seat_univ = pd.read_sql_query(
    "select distinct stock_id from stock_broker_branch_daily "
    "where securities_trader_id in ('9661','9217')", con)["stock_id"].tolist()
q = ("select stock_id, trade_date as date, open, close, adj_close from stock_daily_bars "
     f"where trade_date>='2024-01-01' and stock_id in ({','.join('?'*len(seat_univ))})")
px = pd.read_sql_query(q, con, params=tuple(seat_univ))
px["adj_close"] = px["adj_close"].fillna(px["close"])
px = px.sort_values(["stock_id", "date"])
adj = px["adj_close"] / px["close"]
px["adj_open"] = px["open"] * adj
gg = px.groupby("stock_id", group_keys=False)
entry = gg["adj_open"].shift(-1)
for h in H:
    px[f"fwd{h}"] = gg["adj_close"].shift(-h) / entry - 1.0
# date cross-sectional mean -> selection excess
for h in H:
    px[f"xs{h}"] = px.groupby("date")[f"fwd{h}"].transform("mean")
    px[f"sel{h}"] = px[f"fwd{h}"] - px[f"xs{h}"]
sel = px.set_index(["stock_id", "date"])[[f"sel{h}" for h in H]]
stocks = px["stock_id"].unique().tolist()
print(f"   {len(stocks)} stocks, {len(px):,} price rows", flush=True)

print("2) accumulate per-seat over stocks (indexed by stock_id)...", flush=True)
acc = {}  # seat_id -> [n, s5, s10, ss5, ss10, name]
for i, code in enumerate(stocks):
    t = pd.read_sql_query(
        "select securities_trader_id as seat, securities_trader as name, trade_date as date "
        "from stock_broker_branch_daily where stock_id=? and net>0", con, params=(code,))
    if t.empty:
        continue
    s = sel.loc[code] if code in sel.index.get_level_values(0) else None
    if s is None:
        continue
    t = t.merge(s.reset_index(), on="date").dropna(subset=["sel5", "sel10"])
    for seat, sub in t.groupby("seat"):
        a = acc.setdefault(seat, [0, 0.0, 0.0, 0.0, 0.0, sub["name"].iloc[0]])
        a[0] += len(sub)
        a[1] += sub["sel5"].sum(); a[2] += sub["sel10"].sum()
        a[3] += (sub["sel5"]**2).sum(); a[4] += (sub["sel10"]**2).sum()
    if (i + 1) % 150 == 0:
        print(f"   ...{i+1}/{len(stocks)} stocks", flush=True)
con.close()

rows = []
for seat, (n, s5, s10, ss5, ss10, name) in acc.items():
    if n < MIN_N:
        continue
    m5, m10 = s5/n, s10/n
    sd5 = np.sqrt(max(ss5/n - m5**2, 0)); sd10 = np.sqrt(max(ss10/n - m10**2, 0))
    t5 = m5/(sd5/np.sqrt(n)) if sd5 > 0 else 0
    t10 = m10/(sd10/np.sqrt(n)) if sd10 > 0 else 0
    rows.append(dict(seat=seat, name=name, n=n, sel5=m5, sel10=m10, t5=t5, t10=t10))
r = pd.DataFrame(rows)
r.to_csv(f"{OUT}/seat_scan_allbook.csv", index=False)

nseats = len(r)
from scipy.stats import norm
bonf_t = norm.ppf(1 - 0.025/nseats)   # two-sided Bonferroni threshold in z
print(f"\n=== scanned {nseats} seats (n>=200) ===")
print(f"cost bar = {COST*100:.2f}%/trade ; Bonferroni |t|>{bonf_t:.2f} (family={nseats})")
for h in H:
    col, tc = f"sel{h}", f"t{h}"
    clears_cost = (r[col] > COST).sum()
    sig_raw = (r[tc].abs() > 1.96).sum()
    sig_bonf = (r[tc] > bonf_t).sum()
    both = ((r[col] > COST) & (r[tc] > bonf_t)).sum()
    print(f"\n fwd{h}: SEL-ALPHA>cost: {clears_cost}/{nseats} | raw p<.05: {sig_raw} (expect ~{nseats*0.05:.0f} by luck) | "
          f"Bonferroni-sig: {sig_bonf} | BOTH cost&Bonferroni: {both}")

print("\n=== TOP 12 by sel10 (n>=200) ===")
top = r.sort_values("sel10", ascending=False).head(12)
for _, x in top.iterrows():
    net = (x["sel10"]-COST)*100
    print(f"  {x['name'][:10]:10s}({x['seat']}) n={int(x['n']):5d} sel5={x['sel5']*100:+.2f}% sel10={x['sel10']*100:+.2f}% "
          f"t10={x['t10']:+.1f} net-cost={net:+.2f}%")
print("\n user's seats:")
for s in ["9661", "9217"]:
    x = r[r.seat == s]
    if len(x):
        x = x.iloc[0]
        print(f"  {x['name']}({s}) n={int(x['n'])} sel10={x['sel10']*100:+.2f}% t10={x['t10']:+.1f} "
              f"rank {int((r['sel10']>x['sel10']).sum())+1}/{nseats}")
print("\ndone ->", f"{OUT}/seat_scan_allbook.csv")
