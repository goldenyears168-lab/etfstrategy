#!/usr/bin/env python3
"""跨群「純線性加權相加」結合（linear 對照組）。

積木 = 結構階段建議的 5 個區塊複合分數；結合層 = Σ w_b·B_b（mask 正規化）。
所有評估一律走 lab.evaluate（凍結協定），oc_n 於全面板預先計算並固定，
使風險中性化的基準宇宙不隨覆蓋率改變。
"""
from __future__ import annotations
import sys, itertools, json, time
sys.path.insert(0, '/Users/jackm4/goldenstocks/scripts/research')
from importlib.machinery import SourceFileLoader
lab = SourceFileLoader('lab', '/Users/jackm4/goldenstocks/scripts/research/chip_lab.py').load_module()
import numpy as np, pandas as pd
from scipy.stats import spearmanr

FR, FORM = lab.FR, 250
BLOCKS = {
    'A_安定水位': ['sbl_pct', 'fee', 'retail', 'br_conc'],
    'B_借券利用率': ['sbl_util', 'sbl_volr'],
    'C_流量': ['d_sbl', 'd_util', 'for_1', 'for_5', 'for_20',
               'br_diff', 'br_main', 'br_main5', 'd_retail', 'd_holders'],
    'E_投信': ['itc_1', 'itc_5'],
    'F_自營': ['dlr_1'],
}
BK = list(BLOCKS)

d = lab.load()
d['oc_n'] = pd.read_pickle(lab.DIR / 'oc_n_full.pkl')
DATES = np.sort(d.trade_date.unique())

BASIS = sys.argv[1] if len(sys.argv) > 1 else 'xs'
F = {n: lab.signed(d, n, BASIS) for n in lab.FACTORS}


def xs_rank(s: pd.Series) -> pd.Series:
    """重排名回 [-1,+1]（複合分數在同一日內比較）。"""
    t = d.assign(_v=s)
    return (t.groupby('trade_date')._v.rank(pct=True) - 0.5) * 2


def daily_ic(s: pd.Series) -> pd.Series:
    """逐日橫斷面 Spearman(score, oc_n)。"""
    t = d.assign(_v=s)[['trade_date', '_v', 'oc_n']].dropna()
    out = {}
    for dt, g in t.groupby('trade_date', sort=True):
        if len(g) >= 120:
            out[dt] = spearmanr(g._v, g.oc_n).statistic
    return pd.Series(out).sort_index()


def daily_spread(s: pd.Series) -> pd.Series:
    """逐日多空腿報酬（給逆波動用）。"""
    t = d.assign(_v=s)[['trade_date', 'stock_id', '_v', 'oc_n']].dropna()
    out = {}
    for dt, g in t.groupby('trade_date', sort=True):
        if len(g) < 120:
            continue
        n = max(3, int(round(len(g) * FR)))
        q = g.sort_values('_v', ascending=False)
        out[dt] = q.oc_n.head(n).mean() - q.oc_n.tail(n).mean()
    return pd.Series(out).sort_index()


def wf(series: pd.Series, how: str) -> pd.Series:
    """walk-forward 統計：rolling 250(min 120) → shift(2)（oc_n 於 T+1 收盤才實現）。"""
    r = series.rolling(250, min_periods=120)
    if how == 'mean':
        v = r.mean()
    elif how == 'ir':
        v = r.mean() / r.std()
    elif how == 'invvol':
        v = 1.0 / r.std()
    return v.shift(2)


def prior(W: pd.DataFrame) -> pd.DataFrame:
    """暖機期／缺 IC 歷史 → 等權先驗，避免整段前期變 NaN 而縮短 OOS 視窗。"""
    rm = W.mean(axis=1)
    W = W.apply(lambda c: c.fillna(rm))
    return W.fillna(1.0)


def blend(parts: list[pd.Series], w=None) -> pd.Series:
    """Σ w_i·f_i / Σ_{可得} |w_i| —— mask 重新正規化，缺值不參與也不被填 0。"""
    M = pd.concat(parts, axis=1)
    M.columns = range(len(parts))
    if w is None:
        W = pd.DataFrame(1.0, index=M.index, columns=M.columns)
    else:                                    # 逐日時變權重（index=trade_date）
        W = w.reindex(d.trade_date.values)
        W.index = M.index
    W = prior(W).where(M.notna())
    num = (W * M).sum(axis=1, min_count=1)
    den = W.abs().sum(axis=1, min_count=1)
    out = num / den
    ew = M.mean(axis=1)                      # Σ|w|=0（IC 全負被截斷）→ 退回等權
    return out.where(den > 0, ew)


def fill0(parts: list[pd.Series], w) -> pd.Series:
    """對照：缺值填 0（logit 0.5 的線性類比），分母固定為全部成員。"""
    M = pd.concat(parts, axis=1).fillna(0.0)
    M.columns = range(len(parts))
    ok = pd.concat(parts, axis=1).notna().any(axis=1)
    if w is None:
        s = M.mean(axis=1)
    elif isinstance(w, dict):
        s = sum(w[i] * M[i] for i in M.columns) / sum(abs(v) for v in w.values())
    return s.where(ok)


# ---------------------------------------------------------------- 區塊分數
t0 = time.time()
blocks_ew, blocks_icp = {}, {}
for name, mem in BLOCKS.items():
    parts = [F[m] for m in mem]
    blocks_ew[name] = xs_rank(blend(parts, None))
    if len(mem) == 1:
        blocks_icp[name] = blocks_ew[name]
        continue
    ics = pd.concat([wf(daily_ic(p), 'mean') for p in parts], axis=1)
    ics.columns = range(len(parts))
    blocks_icp[name] = xs_rank(blend(parts, ics.clip(lower=0)))
print(f'區塊分數建好 {time.time()-t0:.0f}s', flush=True)

DONE = set()
import re as _re
try:
    for _ln in open('/tmp/lin_xs.log'):
        _m = _re.match(r'^\s{2}(\S.*?)\s+-?[\d.]+%', _ln)
        if _m: DONE.add(_m.group(1).strip())
except FileNotFoundError:
    pass
print(f'已完成 {len(DONE)} 組態，跳過', flush=True)

RES = []
def run(tag, score, extra=None):
    if tag in DONE:
        return None
    r = lab.evaluate(d, score)
    r['tag'] = tag
    r['basis'] = BASIS
    if extra:
        r.update(extra)
    RES.append(r)
    print(lab.report(tag, r), flush=True)
    return r

print(lab.HEADER)
# --- 積木本身（1 群）
for name in BK:
    run(f'[1群] {name}·EW', blocks_ew[name])
    if len(BLOCKS[name]) > 1:
        run(f'[1群] {name}·ICpos', blocks_icp[name])

# --- 跨群線性結合
for lvl1, BLK in (('ICpos', blocks_icp),):
    ic_b = {k: wf(daily_ic(v), 'mean') for k, v in BLK.items()}
    ir_b = {k: wf(daily_ic(v), 'ir') for k, v in BLK.items()}
    iv_b = {k: wf(daily_spread(v), 'invvol') for k, v in BLK.items()}
    for k in range(2, len(BK) + 1):
        for combo in itertools.combinations(BK, k):
            parts = [BLK[c] for c in combo]
            lbl = '+'.join(c.split('_')[0] for c in combo)
            run(f'[{k}群/{lvl1}] {lbl}·EW', xs_rank(blend(parts, None)))
            for wn, src in (('RP', iv_b), ('IC', ic_b), ('ICpos', ic_b), ('IR', ir_b)):
                W = pd.concat([src[c] for c in combo], axis=1)
                W.columns = range(len(combo))
                if wn == 'ICpos':
                    W = W.clip(lower=0)
                if wn == 'RP':
                    W = W.div(W.sum(axis=1), axis=0)
                run(f'[{k}群/{lvl1}] {lbl}·{wn}', xs_rank(blend(parts, W)))

pd.DataFrame(RES).to_csv(lab.DIR / f'linear_combine_{BASIS}_part2.csv', index=False)
print('saved', flush=True)
