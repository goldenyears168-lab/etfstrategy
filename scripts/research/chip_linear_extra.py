#!/usr/bin/env python3
"""linear 結合的第三批：換手調整權重 ＋ 分數平滑（皆為線性算子）。"""
from __future__ import annotations
import sys, itertools
sys.path.insert(0, '/Users/jackm4/goldenstocks/scripts/research')
from importlib.machinery import SourceFileLoader
lab = SourceFileLoader('lab', '/Users/jackm4/goldenstocks/scripts/research/chip_lab.py').load_module()
import numpy as np, pandas as pd
from scipy.stats import spearmanr

d = lab.load(); d['oc_n'] = pd.read_pickle(lab.DIR / 'oc_n_full.pkl')
F = {n: lab.signed(d, n, 'xs') for n in lab.FACTORS}
BLOCKS = {
    'A_安定水位': ['sbl_pct', 'fee', 'retail', 'br_conc'],
    'B_借券利用率': ['sbl_util', 'sbl_volr'],
    'C_流量': ['d_sbl', 'd_util', 'for_1', 'for_5', 'for_20',
               'br_diff', 'br_main', 'br_main5', 'd_retail', 'd_holders'],
    'E_投信': ['itc_1', 'itc_5'],
    'F_自營': ['dlr_1'],
}
BK = list(BLOCKS)

def xs_rank(s):
    t = d.assign(_v=s); return (t.groupby('trade_date')._v.rank(pct=True) - 0.5) * 2

def blend(parts, W=None):
    M = pd.concat(parts, axis=1); M.columns = range(len(parts))
    if W is None:
        W = pd.DataFrame(1.0, index=M.index, columns=M.columns)
    else:
        W = W.reindex(d.trade_date.values); W.index = M.index
        rm = W.mean(axis=1); W = W.apply(lambda c: c.fillna(rm)).fillna(1.0)
    W = W.where(M.notna())
    den = W.abs().sum(axis=1, min_count=1)
    return ((W * M).sum(axis=1, min_count=1) / den).where(den > 0, M.mean(axis=1))

def daily_ic(s):
    t = d.assign(_v=s)[['trade_date', '_v', 'oc_n']].dropna(); o = {}
    for dt, g in t.groupby('trade_date', sort=True):
        if len(g) >= 120: o[dt] = spearmanr(g._v, g.oc_n).statistic
    return pd.Series(o).sort_index()

def daily_turn(s):
    """區塊自身多頭腿的逐日換手（給換手調整權重用）。"""
    t = d.assign(_v=s)[['trade_date', 'stock_id', '_v']].dropna(); o = {}; prev = set()
    for dt, g in t.groupby('trade_date', sort=True):
        if len(g) < 120: continue
        n = max(3, int(round(len(g) * lab.FR)))
        L = set(g.nlargest(n, '_v').stock_id)
        if prev: o[dt] = len(L - prev) / n
        prev = L
    return pd.Series(o).sort_index()

blk = {k: xs_rank(blend([F[m] for m in v])) for k, v in BLOCKS.items()}
IC = {k: daily_ic(v).rolling(250, min_periods=120).mean().shift(2) for k, v in blk.items()}
TU = {k: daily_turn(v).rolling(250, min_periods=120).mean().shift(2) for k, v in blk.items()}
print('換手（全期均）：', {k: round(float(daily_turn(v).mean()), 3) for k, v in blk.items()}, flush=True)

RES = []
def run(tag, s):
    r = lab.evaluate(d, s); r['tag'] = tag; RES.append(r)
    print(lab.report(tag, r), flush=True)

print(lab.HEADER)
# ---- 換手調整 IC 權重：w ∝ max(IC,0) / 換手（單位換手能買到多少 IC）
for k in range(2, 6):
    for combo in itertools.combinations(BK, k):
        lbl = '+'.join(c.split('_')[0] for c in combo)
        W = pd.concat([IC[c].clip(lower=0) / TU[c] for c in combo], axis=1)
        W.columns = range(len(combo))
        run(f'[{k}群] {lbl}·ICpos/換手', xs_rank(blend([blk[c] for c in combo], W)))
        W2 = pd.concat([1.0 / TU[c] for c in combo], axis=1); W2.columns = range(len(combo))
        run(f'[{k}群] {lbl}·1/換手', xs_rank(blend([blk[c] for c in combo], W2)))

# ---- 分數平滑（線性算子）：對全 5 群 EW 分數做 k 日移動平均
full5 = blend([blk[c] for c in BK])
tmp = d.assign(_v=full5).sort_values(['stock_id', 'trade_date'])
for w in (1, 5, 10, 20, 60):
    sm = tmp.groupby('stock_id')._v.transform(lambda s: s.rolling(w, min_periods=1).mean())
    run(f'[全5群EW] 分數 {w:>2} 日平滑', xs_rank(sm.reindex(d.index)))
a1 = F['sbl_pct']
tmp2 = d.assign(_v=a1).sort_values(['stock_id', 'trade_date'])
for w in (1, 20):
    sm = tmp2.groupby('stock_id')._v.transform(lambda s: s.rolling(w, min_periods=1).mean())
    run(f'[A1 sbl_pct] 分數 {w:>2} 日平滑', xs_rank(sm.reindex(d.index)))

# ---- sbl_pct 為核心、其餘群當微調（線性小權重）
for c in BK[1:]:
    for a in (0.9, 0.75, 0.5):
        W = pd.DataFrame({0: a, 1: 1 - a}, index=pd.Index(np.sort(d.trade_date.unique())))
        run(f'[2群] A1(sbl_pct)*{a}+{c.split("_")[0]}*{1-a:.2f}',
            xs_rank(blend([F['sbl_pct'], blk[c]], W)))
W = pd.DataFrame({i: (0.9 if i == 0 else 0.025) for i in range(5)},
                 index=pd.Index(np.sort(d.trade_date.unique())))
run('[5群] A1*0.90 + B/C/E/F 各*0.025',
    xs_rank(blend([F['sbl_pct']] + [blk[c] for c in BK[1:]], W)))
pd.DataFrame(RES).to_csv(lab.DIR / 'linear_extra.csv', index=False)
print('saved')
