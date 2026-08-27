#!/usr/bin/env python
"""B 線對抗覆核：獨立路徑重算 + 集中度 + 分母 floor 敏感度.

獨立性：不用 pandas pivot/rolling/shift；全部用 numpy cumsum 與位置索引重寫。
只讀 cache pickle，不碰 DB。
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import pandas as pd

CACHE = Path("/Users/jackm4/goldenstocks/reports/research/chip-street-canon/cache")
WINDOW_START, WINDOW_END = np.datetime64("2024-07-01"), np.datetime64("2026-08-26")
TAPE_END = np.datetime64("2026-07-16")
HOLDOUT = np.datetime64("2026-01-01")

top = pd.read_pickle(CACHE / "top15_daily.pkl")
px = pd.read_pickle(CACHE / "price_panel.pkl")
top["trade_date"] = pd.to_datetime(top["trade_date"])
px["trade_date"] = pd.to_datetime(px["trade_date"])

# 重複列獨立驗證
print("dup_px =", int(px.duplicated(["stock_id", "trade_date"]).sum()),
      "dup_top =", int(top.duplicated(["stock_id", "trade_date"]).sum()))

cal = np.array(sorted(px["trade_date"].unique()), dtype="datetime64[ns]")
ncal = len(cal)
pos_of = {d: i for i, d in enumerate(cal)}
stocks = sorted(px["stock_id"].unique())
ns = len(stocks)
sid = {s: i for i, s in enumerate(stocks)}

def to_mat(df, col, stock_map, nstock, fill=np.nan):
    m = np.full((ncal, nstock), fill)
    r = df["trade_date"].map(pos_of).to_numpy()
    c = df["stock_id"].map(stock_map).to_numpy()
    m[r, c] = df[col].to_numpy(dtype=float)
    return m

O = to_mat(px, "open", sid, ns)
C = to_mat(px, "close", sid, ns)
V = to_mat(px, "volume", sid, ns)

bstocks = sorted(top["stock_id"].unique())
nb = len(bstocks)
bid = {s: i for i, s in enumerate(bstocks)}
NET = to_mat(top, "top15_net", bid, nb)          # NaN = 該日無分點列
HB = to_mat(top, "n_buy_houses", bid, nb)
HS = to_mat(top, "n_sell_houses", bid, nb)
b_in_px = np.array([sid.get(s, -1) for s in bstocks])
print("branch stocks not in px:", int((b_in_px < 0).sum()))

def roll_sum_strict(m, w):
    """window sum；窗內有 NaN → NaN；前 w-1 列 NaN。"""
    bad = np.isnan(m)
    cs = np.cumsum(np.nan_to_num(m), axis=0)
    cb = np.cumsum(bad, axis=0)
    out = np.full_like(m, np.nan)
    out[w-1:] = cs[w-1:] - np.vstack([np.zeros((1, m.shape[1])), cs[:-w]])[:m.shape[0]-w+1]
    nbad = cb[w-1:] - np.vstack([np.zeros((1, m.shape[1])), cb[:-w]])[:m.shape[0]-w+1]
    out[w-1:][nbad > 0] = np.nan
    return out

def roll_sum_fill0(m, w):
    z = np.nan_to_num(m)
    cs = np.cumsum(z, axis=0)
    out = np.full_like(m, np.nan)
    out[w-1:] = cs[w-1:] - np.vstack([np.zeros((1, m.shape[1])), cs[:-w]])[:m.shape[0]-w+1]
    return out

# 宇宙：close>=10 且 20 日均量>300k（窗內 20 筆全有 → strict）
vol20 = roll_sum_strict(V, 20) / 20.0
UNIV = (C >= 10.0) & (vol20 > 300_000)
UNIV &= ~np.isnan(C) & ~np.isnan(vol20)
print("universe stockdays =", int(UNIV.sum()), "daily median =",
      int(np.median(UNIV.sum(axis=1))))

# cost20（branch 欄位空間）
VWb_full = to_mat(px, "amount", sid, ns) / np.where(V > 0, V, np.nan)
VWb = np.full((ncal, nb), np.nan)
ok = b_in_px >= 0
VWb[:, ok] = VWb_full[:, b_in_px[ok]]
NET0 = np.nan_to_num(NET)
contrib = NET0 * VWb
contrib[(NET0 == 0.0) & np.isnan(contrib)] = 0.0
num20 = roll_sum_strict(contrib, 20)
den20 = roll_sum_fill0(NET0, 20)     # NET0 無 NaN
Vb = np.full((ncal, nb), np.nan)
Vb[:, ok] = V[:, b_in_px[ok]]
vol20lots = roll_sum_fill0(Vb / 1000.0, 20)
first_pos = int(np.searchsorted(cal, WINDOW_START))
first_cost = first_pos + 20 - 1
def make_cost(floor_frac):
    defined = (den20 > 0) & (den20 >= floor_frac * vol20lots)
    defined[:first_cost, :] = False
    cost = np.where(defined & ~np.isnan(num20), num20 / np.where(den20 > 0, den20, np.nan), np.nan)
    cost[~defined] = np.nan
    return cost
cost20 = make_cost(0.005)
print("cost20 defined stockdays =", int(np.sum(~np.isnan(cost20))))
print("first_cost_date =", str(cal[first_cost])[:10])

# 濾網：5 日均家數差<0（5 筆全有）
hd = HB - HS
hd5 = roll_sum_strict(hd, 5) / 5.0
FILT = hd5 < 0

Cb = np.full((ncal, nb), np.nan); Cb[:, ok] = C[:, b_in_px[ok]]
Ob = np.full((ncal, nb), np.nan); Ob[:, ok] = O[:, b_in_px[ok]]
UNIVb = np.zeros((ncal, nb), bool); UNIVb[:, ok] = UNIV[:, b_in_px[ok]]

def events(cost):
    above = Cb > cost
    below = Cb < cost
    bd = np.zeros((ncal, nb), bool)
    bd[1:] = ~np.isnan(cost[1:]) & ~np.isnan(cost[:-1]) & ~np.isnan(Cb[1:]) & ~np.isnan(Cb[:-1])
    up = np.zeros((ncal, nb), bool); dn = np.zeros((ncal, nb), bool)
    up[1:] = below[:-1] & above[1:] & bd[1:]
    dn[1:] = above[:-1] & below[1:] & bd[1:]
    inwin = (cal >= WINDOW_START) & (cal <= TAPE_END)
    up = up & FILT & UNIVb & inwin[:, None]
    dn = dn & FILT & UNIVb & inwin[:, None]
    return up, dn

ev_up, ev_dn = events(cost20)
print("events up/dn (valid tape) =", int(ev_up.sum()), int(ev_dn.sum()))

Opos = np.where(O > 0, O, np.nan)
Cpos = np.where(C > 0, C, np.nan)
entry = np.full_like(O, np.nan); entry[:-1] = Opos[1:]

ev_any_full = np.zeros((ncal, ns), bool)
ev_any_full[:, b_in_px[ok]] = (ev_up | ev_dn)[:, ok]

def cell(ev, h, label, cost_tag=""):
    exitc = np.full_like(C, np.nan); exitc[:-h] = Cpos[h:]
    ret = (exitc / entry - 1.0) * 100.0
    base_mask = UNIV & ~ev_any_full & ~np.isnan(ret)
    daysum = np.where(base_mask, ret, 0.0).sum(axis=1)
    daycnt = base_mask.sum(axis=1)
    daymean = np.where(daycnt > 0, daysum / np.maximum(daycnt, 1), np.nan)
    retb = np.full((ncal, nb), np.nan); retb[:, ok] = ret[:, b_in_px[ok]]
    r, c = np.where(ev)
    raw = retb[r, c]
    exc = raw - daymean[r]
    m = ~np.isnan(raw) & ~np.isnan(exc)
    raw, exc, r, c = raw[m], exc[m], r[m], c[m]
    print(f"{cost_tag}{label}: n={len(exc)} mean_raw={np.mean(raw):+.4f}% "
          f"mean_excess={np.mean(exc):+.4f}%")
    return r, c, exc

r5, c5, e5 = cell(ev_up, 5, "up_h5")
r10, c10, e10 = cell(ev_up, 10, "up_h10")
d5r, d5c, d5e = cell(ev_dn, 5, "dn_h5")
d10r, d10c, d10e = cell(ev_dn, 10, "dn_h10")

# 集中度：up_h10 holdout（唯一亮點）按月/按股分解
hold = cal[r10] >= HOLDOUT
he = e10[hold]; hr = r10[hold]; hc = c10[hold]
print(f"\nholdout up_h10: n={len(he)} mean={np.mean(he):+.4f}%")
mm = pd.Series(he, index=pd.to_datetime(cal[hr])).groupby(lambda d: d.strftime("%Y-%m"))
print("by month (n, mean%, sum-share of total pnl):")
tot = he.sum()
for k, g in mm:
    print(f"  {k}: n={len(g)} mean={g.mean():+.3f} share={g.sum()/tot:+.2%}")
st = pd.Series(he, index=[bstocks[i] for i in hc]).groupby(level=0).agg(["count", "mean", "sum"])
st = st.sort_values("sum", ascending=False)
print("top5 stocks by pnl share:", [(i, int(row["count"]), f"{row['sum']/tot:+.1%}") for i, row in st.head(5).iterrows()])
print("full-window up_h10 top-month share:")
mm2 = pd.Series(e10, index=pd.to_datetime(cal[r10])).groupby(lambda d: d.strftime("%Y-%m")).sum()
print((mm2 / e10.sum()).sort_values(ascending=False).head(3).to_string())

# 敏感度 1：拿掉未預註記的 0.5% 分母 floor
cost_nf = make_cost(0.0)
u2, d2 = events(cost_nf)
print("\n[no-floor sensitivity] events up/dn =", int(u2.sum()), int(d2.sum()))
cell(u2, 5, "up_h5", "[no-floor] ")
cell(u2, 10, "up_h10", "[no-floor] ")
cell(d2, 5, "dn_h5", "[no-floor] ")
cell(d2, 10, "dn_h10", "[no-floor] ")

# 敏感度 2：不截斷退化 tape（把 2026-07-17~08-26 事件加回）
above = Cb > cost20; below = Cb < cost20
bd = np.zeros((ncal, nb), bool)
bd[1:] = ~np.isnan(cost20[1:]) & ~np.isnan(cost20[:-1]) & ~np.isnan(Cb[1:]) & ~np.isnan(Cb[:-1])
upA = np.zeros((ncal, nb), bool); upA[1:] = below[:-1] & above[1:] & bd[1:]
inw = (cal >= WINDOW_START) & (cal <= WINDOW_END)
upA = upA & FILT & UNIVb & inw[:, None]
cell(upA, 10, "up_h10", "[incl degraded tape] ")

# tape 退化獨立驗證
t2 = top.groupby("trade_date")["stock_id"].nunique()
print("\ntape daily stock counts around 2026-07-16:")
print(t2.loc["2026-07-10":"2026-07-24"].to_string())
