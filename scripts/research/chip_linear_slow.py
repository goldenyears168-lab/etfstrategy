#!/usr/bin/env python3
"""linear 第五批：先量出每個因子的「換手成本」，再只用慢因子做線性相加。

理由：線性結合的換手約等於成員換手的加權下界；只要混進一個快因子，
整個組合就掉進 0.471% 成本的坑。所以要問的不是「哪些因子有 alpha」，
而是「在換手預算內，哪些因子還買得起」。
"""
from __future__ import annotations
import sys, itertools
sys.path.insert(0, '/Users/jackm4/goldenstocks/scripts/research')
from importlib.machinery import SourceFileLoader
lab = SourceFileLoader('lab', '/Users/jackm4/goldenstocks/scripts/research/chip_lab.py').load_module()
import numpy as np, pandas as pd

d = lab.load(); d['oc_n'] = pd.read_pickle(lab.DIR / 'oc_n_full.pkl')
F = {n: lab.signed(d, n, 'xs') for n in lab.FACTORS}

def xs_rank(s):
    t = d.assign(_v=s); return (t.groupby('trade_date')._v.rank(pct=True) - 0.5) * 2

def mm(parts, w=None):
    M = pd.concat(parts, axis=1); M.columns = range(len(parts))
    if w is None: return M.mean(axis=1)
    W = pd.DataFrame({i: np.full(len(M), w[i]) for i in M.columns}, index=M.index).where(M.notna())
    return (W * M).sum(axis=1, min_count=1) / W.abs().sum(axis=1, min_count=1)

RES = []
def run(tag, s):
    r = lab.evaluate(d, s); r['tag'] = tag; RES.append(r)
    print(lab.report(tag, r), flush=True); return r

print(lab.HEADER)
print('--- 單因子換手普查（23 支） ---')
tau = {}
for n in lab.FACTORS:
    r = run(f'[單] {n}', F[n])
    tau[n] = r.get('turnover', np.nan)
slow = sorted([k for k, v in tau.items() if v == v and v < 0.20], key=lambda k: tau[k])
print('\n慢因子（換手 <20%）：', [(k, round(tau[k] * 100, 1)) for k in slow], flush=True)

print('\n--- 只用慢因子的線性相加（等權；2~全部） ---')
for k in range(2, min(len(slow), 6) + 1):
    for combo in itertools.combinations(slow, k):
        run(f'[慢{k}] ' + '+'.join(combo), xs_rank(mm([F[c] for c in combo])))
if len(slow) > 6:
    run('[慢全] ' + '+'.join(slow), xs_rank(mm([F[c] for c in slow])))

print('\n--- sbl_pct 核心 + 單一慢因子的權重掃描 ---')
for c in [s for s in slow if s != 'sbl_pct']:
    for a in (0.9, 0.8, 0.7, 0.5):
        run(f'[權重] {a:.1f}·sbl_pct + {1-a:.1f}·{c}',
            xs_rank(mm([F['sbl_pct'], F[c]], {0: a, 1: 1 - a})))
pd.DataFrame(RES).to_csv(lab.DIR / 'linear_slow.csv', index=False)
print('saved')
