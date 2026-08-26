#!/usr/bin/env python3
"""linear 第六批：對「A1 + 5 日平滑的 C/E」這個唯一淨值為正的組態做穩健性拷問。"""
from __future__ import annotations
import sys
sys.path.insert(0, '/Users/jackm4/goldenstocks/scripts/research')
from importlib.machinery import SourceFileLoader
lab = SourceFileLoader('lab', '/Users/jackm4/goldenstocks/scripts/research/chip_lab.py').load_module()
import numpy as np, pandas as pd

d = lab.load(); d['oc_n'] = pd.read_pickle(lab.DIR / 'oc_n_full.pkl')
F = {n: lab.signed(d, n, 'xs') for n in lab.FACTORS}
BLK = {'C': ['d_sbl', 'd_util', 'for_1', 'for_5', 'for_20', 'br_diff',
             'br_main', 'br_main5', 'd_retail', 'd_holders'],
       'E': ['itc_1', 'itc_5']}
ORD = d.sort_values(['stock_id', 'trade_date']).index

def xs_rank(s, frame=None):
    f = d if frame is None else frame
    t = f.assign(_v=s); return (t.groupby('trade_date')._v.rank(pct=True) - 0.5) * 2

def smooth(s, k):
    if k <= 1: return s
    t = d.assign(_v=s).loc[ORD]
    return t.groupby('stock_id')._v.transform(
        lambda x: x.rolling(k, min_periods=max(1, k // 3)).mean()).reindex(d.index)

base = {b: xs_rank(pd.concat([F[m] for m in v], axis=1).mean(axis=1)) for b, v in BLK.items()}
RES = []
def run(tag, s, frame=None):
    f = d if frame is None else frame
    r = lab.evaluate(f, s.reindex(f.index)); r['tag'] = tag; RES.append(r)
    print(lab.report(tag, r), flush=True); return r

print(lab.HEADER)
print('--- 平滑窗長細掃（A1 + C_sm + E_sm 等權） ---')
S = {}
for k in (1, 2, 3, 4, 5, 7, 10, 15, 20, 30, 40, 60, 90, 120):
    s = xs_rank(pd.concat([F['sbl_pct'], smooth(base['C'], k), smooth(base['E'], k)],
                          axis=1).mean(axis=1))
    S[k] = s
    run(f'[3群] A1+C+E·EW·{k:>3}日平滑', s)

print('\n--- 也把 A1 一起平滑（對照） ---')
for k in (5, 20):
    s = xs_rank(pd.concat([smooth(F['sbl_pct'], k), smooth(base['C'], k),
                           smooth(base['E'], k)], axis=1).mean(axis=1))
    run(f'[3群] 三者全 {k} 日平滑', s)

print('\n--- OOS 期間穩定性（把 OOS 切兩半各自重跑 evaluate） ---')
dates = np.sort(d.trade_date.unique()); mid = dates[len(dates) // 2 + 125]
for k in (1, 5, 20):
    for nm, msk in (('前半', d.trade_date <= mid), ('後半', d.trade_date > dates[249])):
        f = d[msk]
        if f.trade_date.nunique() < 320: continue
        run(f'[3群] A1+C+E {k:>3}日平滑 · {nm}({f.trade_date.min()}~{f.trade_date.max()})',
            S[k], frame=f)

print('\n--- 安慰劑：把 C/E 換成同換手的隨機分數再平滑 ---')
rng = np.random.default_rng(7)
for k in (5,):
    n1 = pd.Series(rng.normal(size=len(d)), index=d.index)
    n2 = pd.Series(rng.normal(size=len(d)), index=d.index)
    run(f'[安慰劑] A1 + 2 支隨機分數({k}日平滑)',
        xs_rank(pd.concat([F['sbl_pct'], smooth(xs_rank(n1), k), smooth(xs_rank(n2), k)],
                          axis=1).mean(axis=1)))
    run(f'[安慰劑] 只有 A1 + 隨機({k}日平滑) 兩支',
        xs_rank(pd.concat([F['sbl_pct'], smooth(xs_rank(n1), k)], axis=1).mean(axis=1)))
pd.DataFrame(RES).to_csv(lab.DIR / 'linear_robust.csv', index=False)
print('saved')
