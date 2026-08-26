#!/usr/bin/env python3
"""linear 第七批：對「群數」與「平滑窗長」做同口徑對照。

平滑一律施加在**最終合成分數**上（MA_k 是線性算子，語意仍是純線性相加），
且單因子基準也吃同一個 k —— 否則就是拿加了降噪的組合去比沒降噪的基準。
"""
from __future__ import annotations
import sys
sys.path.insert(0, '/Users/jackm4/goldenstocks/scripts/research')
from importlib.machinery import SourceFileLoader
lab = SourceFileLoader('lab', '/Users/jackm4/goldenstocks/scripts/research/chip_lab.py').load_module()
import numpy as np, pandas as pd

d = lab.load(); d['oc_n'] = pd.read_pickle(lab.DIR / 'oc_n_full.pkl')
F = {n: lab.signed(d, n, 'xs') for n in lab.FACTORS}
MEM = {'A': ['sbl_pct', 'fee', 'retail', 'br_conc'], 'A1': ['sbl_pct'],
       'B': ['sbl_util', 'sbl_volr'],
       'C': ['d_sbl', 'd_util', 'for_1', 'for_5', 'for_20', 'br_diff',
             'br_main', 'br_main5', 'd_retail', 'd_holders'],
       'E': ['itc_1', 'itc_5'], 'F': ['dlr_1']}
ORD = d.sort_values(['stock_id', 'trade_date']).index

def xs_rank(s):
    t = d.assign(_v=s); return (t.groupby('trade_date')._v.rank(pct=True) - 0.5) * 2

def smooth(s, k):
    if k <= 1: return s
    t = d.assign(_v=s).loc[ORD]
    return t.groupby('stock_id')._v.transform(
        lambda x: x.rolling(k, min_periods=max(1, k // 3)).mean()).reindex(d.index)

blk = {b: xs_rank(pd.concat([F[m] for m in v], axis=1).mean(axis=1)) for b, v in MEM.items()}
SETS = [('A1(基準·單因子)', ['A1']), ('A1+E', ['A1', 'E']), ('A1+C', ['A1', 'C']),
        ('A1+C+E', ['A1', 'C', 'E']), ('A1+B+C+E', ['A1', 'B', 'C', 'E']),
        ('A1+B+C+E+F', ['A1', 'B', 'C', 'E', 'F']),
        ('A+B+C+E+F(原5區塊)', ['A', 'B', 'C', 'E', 'F']),
        ('A(基準·4因子區塊)', ['A'])]
RES = []
print(lab.HEADER)
for k in (1, 3, 5, 10, 20, 40, 60, 120):
    for nm, cs in SETS:
        s = smooth(pd.concat([blk[c] for c in cs], axis=1).mean(axis=1), k)
        r = lab.evaluate(d, xs_rank(s)); r['tag'] = f'[{len(cs)}群·MA{k:>3}] {nm}'
        r['k'] = k; r['nset'] = len(cs); RES.append(r)
        print(lab.report(r['tag'], r), flush=True)
pd.DataFrame(RES).to_csv(lab.DIR / 'linear_apples.csv', index=False)
print('saved')
