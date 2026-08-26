#!/usr/bin/env python3
"""linear 結合的對照組：安慰劑、覆蓋率子宇宙、缺值處理、精簡積木。"""
from __future__ import annotations
import sys, itertools, time
sys.path.insert(0, '/Users/jackm4/goldenstocks/scripts/research')
from importlib.machinery import SourceFileLoader
lab = SourceFileLoader('lab', '/Users/jackm4/goldenstocks/scripts/research/chip_lab.py').load_module()
import numpy as np, pandas as pd
from scipy.stats import spearmanr

d = lab.load()
d['oc_n'] = pd.read_pickle(lab.DIR / 'oc_n_full.pkl')
BASIS = 'xs'
F = {n: lab.signed(d, n, BASIS) for n in lab.FACTORS}
BLOCKS = {
    'A_安定水位': ['sbl_pct', 'fee', 'retail', 'br_conc'],
    'B_借券利用率': ['sbl_util', 'sbl_volr'],
    'C_流量': ['d_sbl', 'd_util', 'for_1', 'for_5', 'for_20',
               'br_diff', 'br_main', 'br_main5', 'd_retail', 'd_holders'],
    'E_投信': ['itc_1', 'itc_5'],
    'F_自營': ['dlr_1'],
}
ALT_A = {'A4_含fee': BLOCKS['A_安定水位'],
         'A3_踢fee': ['sbl_pct', 'retail', 'br_conc'],
         'A2_踢fee踢retail': ['sbl_pct', 'br_conc'],
         'A1_只留sbl_pct': ['sbl_pct']}

def xs_rank(s):
    t = d.assign(_v=s)
    return (t.groupby('trade_date')._v.rank(pct=True) - 0.5) * 2

def blend(parts, w=None):
    M = pd.concat(parts, axis=1); M.columns = range(len(parts))
    W = pd.DataFrame(1.0, index=M.index, columns=M.columns) if w is None else w
    W = W.where(M.notna())
    return (W * M).sum(axis=1, min_count=1) / W.abs().sum(axis=1, min_count=1).replace(0, np.nan)

RES = []
def run(tag, score, universe=None):
    s = score if universe is None else score.where(universe)
    r = lab.evaluate(d, s); r['tag'] = tag
    RES.append(r); print(lab.report(tag, r), flush=True); return r

print(lab.HEADER)
blk = {k: xs_rank(blend([F[m] for m in v])) for k, v in BLOCKS.items()}
avail = pd.concat([blk[k].notna() for k in BLOCKS], axis=1)
k_i = avail.sum(axis=1)

# --- (c) 安慰劑：可得區塊數本身
run('[安慰劑] k_i 可得區塊數', k_i.astype(float))
run('[安慰劑] -k_i', -k_i.astype(float))
run('[安慰劑] 覆蓋率隨機噪音', pd.Series(np.random.default_rng(0).normal(size=len(d)), index=d.index))

# --- A 區塊瘦身
for nm, mem in ALT_A.items():
    blk[nm] = xs_rank(blend([F[m] for m in mem]))
    run(f'[1群] {nm}·EW', blk[nm])

# --- 用瘦身後 A 重跑跨群線性（EW 權重）
for aname in ALT_A:
    for k in (2, 3, 4, 5):
        for combo in itertools.combinations(['B_借券利用率', 'C_流量', 'E_投信', 'F_自營'], k - 1):
            lbl = aname.split('_')[0] + '+' + '+'.join(c.split('_')[0] for c in combo)
            run(f'[{k}群/A變體] {lbl}·EW', xs_rank(blend([blk[aname]] + [blk[c] for c in combo])))

# --- (b) 完整覆蓋子宇宙 + (a) 缺值填 0 對照（對全 5 群 EW）
full5 = xs_rank(blend([blk[k] for k in BLOCKS]))
comp = (k_i == 5)
print(f'\n5 區塊全可得比例 = {comp.mean()*100:.1f}%')
run('[對照b] 全5群EW·僅完整覆蓋子宇宙', full5, universe=comp)
run('[對照b] A1+C+E+F EW·僅完整覆蓋子宇宙',
    xs_rank(blend([blk['A1_只留sbl_pct'], blk['C_流量'], blk['E_投信'], blk['F_自營']])), universe=comp)
M = pd.concat([blk[k] for k in BLOCKS], axis=1)
run('[對照a] 全5群EW·缺值填0（分母固定5）',
    xs_rank(M.fillna(0.0).mean(axis=1).where(M.notna().any(axis=1))))
run('[對照a] 全5群EW·mask正規化（主用）', full5)

pd.DataFrame(RES).to_csv(lab.DIR / 'linear_controls.csv', index=False)
print('saved')
