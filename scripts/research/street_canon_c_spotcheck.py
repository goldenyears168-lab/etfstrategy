#!/usr/bin/env python
"""對抗覆核 spot-check：獨立路徑重算 H-STREET-C 的 N2_h2w 與 N3_h4w（up 組）。

與主腳本差異（刻意獨立）：
- 價格直接查 DB（非 price_panel.pkl），dedup 用 pandas sort+drop_duplicates（非 SQL ROW_NUMBER）。
- streak 用 run-length 分段法（非逐列迴圈）。
- 另做：剔除 phantom day 2026-07-10 的敏感度、benchmark 排除事件股版本、
  事件集中度（股票/月份）、up事件佔宇宙比例。
"""
import sqlite3
import numpy as np
import pandas as pd
from bisect import bisect_right
from pathlib import Path
from stock_db import DEFAULT_DB_PATH

ROOT = Path(__file__).resolve().parents[2]
hold = pd.read_csv(ROOT / "reports/research/chip-overlays/cache/holding_shares_per_futures_universe.csv", dtype={"sid": str})
hold = hold.drop_duplicates(["sid", "d"]).sort_values(["sid", "d"]).reset_index(drop=True)
sids = sorted(hold["sid"].unique())

conn = sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)
cal_df = pd.read_sql_query("SELECT DISTINCT trade_date FROM stock_daily_bars WHERE trade_date>='2024-06-01'", conn)
calendar_full = sorted(cal_df["trade_date"])
px = pd.read_sql_query(
    f"SELECT stock_id, trade_date, open, close, volume, source FROM stock_daily_bars "
    f"WHERE trade_date>='2024-06-01' AND stock_id IN ({','.join('?'*len(sids))})", conn, params=sids)
prio = {"twse_mi_index": 0, "tpex_daily": 1, "finmind": 2}
px["prio"] = px["source"].map(prio).fillna(3)
n_before = len(px)
px = px.sort_values(["stock_id", "trade_date", "prio"]).drop_duplicates(["stock_id", "trade_date"], keep="first")
print(f"price rows {n_before} -> dedup {len(px)}; dup dropped {n_before-len(px)}")

def run(calendar, tag):
    cal_idx = {d: i for i, d in enumerate(calendar)}
    pxc = px[px.trade_date.isin(cal_idx)]
    O = pxc.pivot(index="trade_date", columns="stock_id", values="open").reindex(calendar)
    C = pxc.pivot(index="trade_date", columns="stock_id", values="close").reindex(calendar)
    V = pxc.pivot(index="trade_date", columns="stock_id", values="volume").reindex(calendar)
    V20 = V.rolling(20, min_periods=20).mean()
    O = O.reindex(columns=sids); C = C.reindex(columns=sids); V20 = V20.reindex(columns=sids)

    h2 = hold.copy()
    h2["mid"] = h2.pct_100 - h2.pct_400
    h2["chg"] = h2.groupby("sid")["mid"].diff()
    h2["gapd"] = h2.groupby("sid")["d"].transform(lambda s: pd.to_datetime(s).diff().dt.days)
    # run-length streak：sign 序列，gap>9 或 chg 為 0/NaN 視為斷點
    def rl(g):
        sign = np.where(g.chg > 0, 1, np.where(g.chg < 0, -1, 0))
        sign = np.where(g.gapd.to_numpy() > 9, 0, sign)
        sign = np.where(np.isnan(g.chg.to_numpy()), 0, sign)
        up = np.zeros(len(g), int)
        run = 0; prev = 0
        for i, s in enumerate(sign):
            run = run + 1 if (s == 1 and prev == 1) else (1 if s == 1 else 0)
            up[i] = run; prev = s
        return pd.Series(up, index=g.index)
    h2["up_st"] = h2.groupby("sid", group_keys=False).apply(rl)

    weeks = sorted(h2["d"].unique())
    UP = h2.pivot(index="d", columns="sid", values="up_st").reindex(index=weeks, columns=sids)

    results = {}
    for (n_req, hw, hd) in [(2, 2, 10), (3, 4, 20)]:
        rows_ex, rows_raw, per_event = [], [], []
        n_events = 0
        for d in weeks:
            pos = bisect_right(calendar, d)
            ei = pos + 1
            xi = ei + hd - 1
            if xi >= len(calendar):
                continue
            fpos = pos - 1
            fmask = (C.iloc[fpos] >= 10) & (V20.iloc[fpos] > 300_000)
            o = O.iloc[ei]; c = C.iloc[xi]
            r = (c / o - 1) * 100
            r = r.where(np.isfinite(o) & np.isfinite(c) & (o > 0))
            r_univ = r.where(fmask)
            bench = r_univ.mean()
            evt_mask = (UP.loc[d] >= n_req) & fmask
            r_evt = r_univ.where(evt_mask).dropna()
            if len(r_evt):
                n_events += len(r_evt)
                rows_ex.append((d, r_evt.mean() - bench, len(r_evt)))
                rows_raw.append(r_evt.mean())
                # benchmark 排除事件股版本
                bench_x = r_univ.where(~evt_mask.reindex(r_univ.index).fillna(False)).mean()
                per_event.append((d, r_evt.mean() - bench_x))
        wk = pd.DataFrame(rows_ex, columns=["d", "ex", "n"]).set_index("d")
        ex_excl = pd.DataFrame(per_event, columns=["d", "ex"]).set_index("d")["ex"]
        results[f"N{n_req}_h{hw}w"] = dict(
            n_events=n_events, n_weeks=len(wk),
            mean_excess=wk.ex.mean(), mean_raw=np.mean(rows_raw),
            mean_excess_exclbench=ex_excl.mean(),
        )
    print(f"\n=== {tag} (calendar {len(calendar)} days) ===")
    for k, v in results.items():
        print(k, {kk: round(vv, 4) if isinstance(vv, float) else vv for kk, vv in v.items()})
    return results, UP, weeks, O, C, V20

res_full, UP, weeks, O, C, V20 = run(calendar_full, "full calendar (incl 2026-07-10 phantom)")
calendar_nophantom = [d for d in calendar_full if d != "2026-07-10"]
res_np, *_ = run(calendar_nophantom, "no-phantom calendar")

# ---- 集中度（N2_h2w up, full calendar 口徑重建事件明細）----
cal = calendar_full
cal_idx = {d: i for i, d in enumerate(cal)}
rows = []
for d in weeks:
    pos = bisect_right(cal, d); ei = pos + 1; xi = ei + 9
    if xi >= len(cal):
        continue
    fmask = (C.iloc[pos - 1] >= 10) & (V20.iloc[pos - 1] > 300_000)
    o = O.iloc[ei]; c = C.iloc[xi]
    r = ((c / o - 1) * 100).where(np.isfinite(o) & np.isfinite(c) & (o > 0)).where(fmask)
    bench = r.mean()
    ev = r.where((UP.loc[d] >= 2) & fmask).dropna()
    for s, v in ev.items():
        rows.append((d, s, v - bench))
ev_df = pd.DataFrame(rows, columns=["d", "sid", "ex"])
print("\n=== 集中度（N2_h2w up）===")
print("事件數/檔數:", len(ev_df), ev_df.sid.nunique())
top_sid = ev_df.groupby("sid").agg(n=("ex", "size"), sum_ex=("ex", "sum")).sort_values("sum_ex", ascending=False)
tot = ev_df.ex.sum()
print(f"總去均值和 {tot:.1f}；前5檔貢獻 {top_sid.sum_ex.head(5).sum():.1f}；最大單檔事件占比 {top_sid.n.max()/len(ev_df):.2%}")
print(top_sid.head(5).to_string())
ev_df["mon"] = ev_df.d.str[:7]
mon = ev_df.groupby("mon").agg(n=("ex", "size"), mean_ex=("ex", "mean"))
print("\n月份分布（mean_ex 前3/後3）:")
print(mon.sort_values("mean_ex").iloc[list(range(3)) + list(range(-3, 0))].to_string())
# 剔除貢獻最大單一月份後
best_mon = mon.mean_ex.idxmax()
wk_ex = ev_df.groupby("d").ex.mean()
wk_ex_drop = ev_df[ev_df.mon != best_mon].groupby("d").ex.mean()
print(f"\n週組合均值 {wk_ex.mean():.4f}；剔除最佳月 {best_mon} 後 {wk_ex_drop.mean():.4f}")
# up 事件佔過濾宇宙比例
frac = []
for d in weeks:
    pos = bisect_right(cal, d)
    if pos + 10 >= len(cal): continue
    fmask = (C.iloc[pos - 1] >= 10) & (V20.iloc[pos - 1] > 300_000)
    n_u = int(((UP.loc[d] >= 2) & fmask).sum()); n_f = int(fmask.sum())
    if n_f: frac.append(n_u / n_f)
print(f"\nup(N>=2) 事件佔過濾宇宙比例：mean {np.mean(frac):.2%}")
