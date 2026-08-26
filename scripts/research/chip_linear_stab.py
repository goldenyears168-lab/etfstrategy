#!/usr/bin/env python3
"""linear 第八批：對候選組態與基準做同口徑的 OOS 前後半穩定性檢定。"""
from __future__ import annotations
import sys
sys.path.insert(0, '/Users/jackm4/goldenstocks/scripts/research')
from importlib.machinery import SourceFileLoader
lab = SourceFileLoader('lab', '/Users/jackm4/goldenstocks/scripts/research/chip_lab.py').load_module()
import numpy as np, pandas as pd

d = lab.load(); d['oc_n'] = pd.read_pickle(lab.DIR / 'oc_n_full.pkl')
F = {n: lab.signed(d, n, 'xs') for n in lab.FACTORS}
MEM = {'A1': ['sbl_pct'], 'A': ['sbl_pct', 'fee', 'retail', 'br_conc'],
       'C': ['d_sbl', 'd_util', 'for_1', 'for_5', 'for_20', 'br_diff',
             'br_main', 'br_main5', 'd_retail', 'd_holders'], 'E': ['itc_1', 'itc_5']}
ORD = d.sort_values(['stock_id', 'trade_date']).index
def xs_rank(s, f=None):
    f = d if f is None else f
    return (f.assign(_v=s).groupby('trade_date')._v.rank(pct=True) - 0.5) * 2
def smooth(s, k):
    if k <= 1: return s
    return d.assign(_v=s).loc[ORD].groupby('stock_id')._v.transform(
        lambda x: x.rolling(k, min_periods=max(1, k // 3)).mean()).reindex(d.index)
blk = {b: xs_rank(pd.concat([F[m] for m in v], axis=1).mean(axis=1)) for b, v in MEM.items()}
dates = np.sort(d.trade_date.unique()); mid = dates[len(dates) // 2 + 125]
SEG = [('全OOS', pd.Series(True, index=d.index)),
       ('前半', d.trade_date <= mid), ('後半', d.trade_date > dates[249])]
RES = []
print(lab.HEADER)
CAND = [('A1(基準)', ['A1']), ('A1+C+E', ['A1', 'C', 'E']),
        ('A1*2+C+E', ['A1', 'A1', 'C', 'E']), ('A(4因子)', ['A'])]
for nm, cs in CAND:
    for k in (1, 20, 40, 60):
        s = xs_rank(smooth(pd.concat([blk[c] for c in cs], axis=1).mean(axis=1), k))
        for sn, msk in SEG:
            f = d[msk]
            if f.trade_date.nunique() < 320: continue
            r = lab.evaluate(f, s.reindex(f.index))
            r['tag'] = f'{nm}·MA{k:>3}·{sn}'; RES.append(r)
            print(lab.report(r['tag'], r), flush=True)
pd.DataFrame(RES).to_csv(lab.DIR / 'linear_stability.csv', index=False)
print('saved')
