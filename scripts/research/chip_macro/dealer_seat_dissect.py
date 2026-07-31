"""Dissect the 3 cost-beating 自營 desks: is their edge real skill, or momentum / hedging?

Seats: 2210 群益期貨, 960T 富邦自營, 980T 元大自營 (+ user's 9661/9217 for contrast).

Test A — MOMENTUM CONTROL: naive selection excess vs momentum-matched excess.
   naive sel10 = fwd10 - same-date cross-sectional mean.
   mom-ctrl sel10 = fwd10 - mean(fwd10 of same-date & same trailing-20d-momentum QUINTILE).
   If the edge collapses under mom-control -> it was just "they buy momentum stocks".
Test B — RETURN PATH: mean fwd1/3/5/10/20 on buy-days -> is the +2.6% front-loaded or does it hold to 10d?
Test C — SEAT'S OWN EXIT: after a buy-day, the seat's own mean net on t+1..t+10 (same stock) ->
   do they hold (net stays +) or flip to selling fast? Tells if a 10-day follow matches their behavior.
"""
from __future__ import annotations

import sqlite3
import numpy as np
import pandas as pd

DB = "data/replica/stocks.db"
OUT = "data/research/chip_macro"
SEATS = {"2210": "群益期貨", "960T": "富邦自營", "980T": "元大自營", "9661": "富邦新店", "9217": "凱基松山"}
H = [1, 3, 5, 10, 20]

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
print("1) tape for the 5 seats (all net, buy+sell)...", flush=True)
tape = pd.read_sql_query(
    "select securities_trader_id as seat, trade_date as date, stock_id, net "
    f"from stock_broker_branch_daily where securities_trader_id in ({','.join('?'*len(SEATS))})",
    con, params=tuple(SEATS))
print(f"   rows: {len(tape):,}", flush=True)

univ = tuple(sorted(tape["stock_id"].unique()))
px = pd.read_sql_query(
    "select stock_id, trade_date as date, open, close, adj_close from stock_daily_bars "
    f"where trade_date>='2023-10-01' and stock_id in ({','.join('?'*len(univ))})", con, params=univ)
con.close()

px["adj_close"] = px["adj_close"].fillna(px["close"])
px = px.sort_values(["stock_id", "date"])
g = px.groupby("stock_id", group_keys=False)
adj = px["adj_close"] / px["close"]
px["adj_open"] = px["open"] * adj
entry = g["adj_open"].shift(-1)
for h in H:
    px[f"fwd{h}"] = g["adj_close"].shift(-h) / entry - 1.0
px["mom20"] = g["adj_close"].apply(lambda s: s / s.shift(20) - 1.0)

# selection excess (naive) and momentum-controlled, per (stock,date)
px["xs10"] = px.groupby("date")["fwd10"].transform("mean")
px["sel10_naive"] = px["fwd10"] - px["xs10"]
def mom_ctrl(grp):
    q = pd.qcut(grp["mom20"].rank(method="first"), 5, labels=False, duplicates="drop")
    grp = grp.assign(_q=q)
    grp["_gm"] = grp.groupby("_q")["fwd10"].transform("mean")
    return grp["fwd10"] - grp["_gm"]
px["sel10_mom"] = px.dropna(subset=["mom20", "fwd10"]).groupby("date", group_keys=False).apply(mom_ctrl)

merged = tape.merge(px, on=["stock_id", "date"], how="left")
buy = merged[merged["net"] > 0]

print("\n=== A. MOMENTUM CONTROL (buy-days, fwd10) ===")
print(f"{'seat':14s} {'n':>6s} {'naive_sel10':>12s} {'mom_ctrl_sel10':>15s} {'shrink':>8s}")
for s, nm in SEATS.items():
    d = buy[buy["seat"] == s].dropna(subset=["sel10_naive", "sel10_mom"])
    if len(d) < 50:
        print(f"{nm}({s}): n={len(d)} too few"); continue
    a, b = d["sel10_naive"].mean(), d["sel10_mom"].mean()
    shrink = (1 - b / a) * 100 if a != 0 else float("nan")
    print(f"{nm}({s}):{'':2s} {len(d):>6d} {a*100:>+11.2f}% {b*100:>+14.2f}% {shrink:>7.0f}%")

print("\n=== B. RETURN PATH on buy-days (naive sel vs cross-sec mean, per horizon) ===")
for s, nm in SEATS.items():
    if s not in ("2210", "960T", "980T"):
        continue
    d = buy[buy["seat"] == s]
    row = []
    for h in H:
        xsh = px.groupby("date")[f"fwd{h}"].mean().rename("xsh")
        dh = d.merge(xsh, on="date").dropna(subset=[f"fwd{h}"])
        row.append(f"fwd{h}={(dh[f'fwd{h}']-dh['xsh']).mean()*100:+.2f}%")
    print(f"  {nm}({s}): " + "  ".join(row))

print("\n=== C. SEAT'S OWN EXIT: mean of seat's net over t+1..t+10 after a buy (same stock) ===")
tape_s = tape.sort_values(["seat", "stock_id", "date"])
for s, nm in SEATS.items():
    if s not in ("2210", "960T", "980T"):
        continue
    sub = tape_s[tape_s["seat"] == s].copy()
    # build per (seat,stock) net series, then for each buy-day average next-10-day net sign
    holds = []
    for code, gsub in sub.groupby("stock_id"):
        gsub = gsub.sort_values("date").reset_index(drop=True)
        nets = gsub["net"].values
        for i in range(len(nets)):
            if nets[i] > 0:
                nxt = nets[i+1:i+11]
                if len(nxt):
                    holds.append((nxt > 0).mean())  # fraction of next 10 active days still buying
    if holds:
        print(f"  {nm}({s}): after a buy, next-10-active-day buying-fraction = {np.mean(holds)*100:.0f}%  (low => flips to selling fast)")
print("\ndone.")
