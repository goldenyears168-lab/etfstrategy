#!/usr/bin/env python3
"""linear 第四批：把高換手區塊先做 k 日移動平均（仍是線性算子）再相加。

動機：A1+C+E 的 gross 是全線最高(+0.104%/日 t=5.20)，死因純粹是 47% 換手。
平滑是唯一不改變「相加」語意、又能直接壓換手的線性手段。
"""
from __future__ import annotations
import sys, itertools
sys.path.insert(0, '/Users/jackm4/goldenstocks/scripts/research')
from importlib.machinery import SourceFileLoader
lab = SourceFileLoader('lab', '/Users/jackm4/goldenstocks/scripts/research/chip_lab.py').load_module()
import numpy as np, pandas as pd

d = lab.load(); d['oc_n'] = pd.read_pickle(lab.DIR / 'oc_n_full.pkl')
F = {n: lab.signed(d, n, 'xs') for n in lab.FACTORS}
BLOCKS = {
    'A': ['sbl_pct', 'fee', 'retail', 'br_conc'],
    'A1': ['sbl_pct'],
    'B': ['sbl_util', 'sbl_volr'],
    'C': ['d_sbl', 'd_util', 'for_1', 'for_5', 'for_20',
          'br_diff', 'br_main', 'br_main5', 'd_retail', 'd_holders'],
    'E': ['itc_1', 'itc_5'],
    'F': ['dlr_1'],
}
ORD = d.sort_values(['stock_id', 'trade_date']).index

def xs_rank(s):
    t = d.assign(_v=s); return (t.groupby('trade_date')._v.rank(pct=True) - 0.5) * 2

def mask_mean(parts):
    M = pd.concat(parts, axis=1); return M.mean(axis=1)

def smooth(s, k):
    if k <= 1: return s
    t = d.assign(_v=s).loc[ORD]
    return t.groupby('stock_id')._v.transform(
        lambda x: x.rolling(k, min_periods=max(1, k // 3)).mean()).reindex(d.index)

blk = {k: xs_rank(mask_mean([F[m] for m in v])) for k, v in BLOCKS.items()}
RES = []
def run(tag, s):
    r = lab.evaluate(d, s); r['tag'] = tag; RES.append(r)
    print(lab.report(tag, r), flush=True)

print(lab.HEADER)
for k in (1, 5, 20, 60, 120):
    for combo in (('A1', 'C', 'E'), ('A1', 'C'), ('A1', 'E'), ('C', 'E'),
                  ('A1', 'B', 'C', 'E', 'F'), ('A', 'B', 'C', 'E', 'F')):
        parts = [blk[c] if c.startswith('A') else smooth(blk[c], k) for c in combo]
        run(f'[{len(combo)}群] {"+".join(combo)}·EW·非A區塊{k:>3}日平滑', xs_rank(mask_mean(parts)))
    for c in ('C', 'E', 'F', 'B', 'A'):
        run(f'[1群] {c}·{k:>3}日平滑', xs_rank(smooth(blk[c], k)))
    # A1 權重加倍（低換手核心加重）
    parts = [blk['A1'], blk['A1'], smooth(blk['C'], k), smooth(blk['E'], k)]
    run(f'[3群] A1*2+C+E·EW·{k:>3}日平滑', xs_rank(mask_mean(parts)))

pd.DataFrame(RES).to_csv(lab.DIR / 'linear_smooth.csv', index=False)
print('saved')
