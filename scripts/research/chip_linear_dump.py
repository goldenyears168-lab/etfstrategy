#!/usr/bin/env python3
import sys
sys.path.insert(0, '/Users/jackm4/goldenstocks/scripts/research')
from importlib.machinery import SourceFileLoader
lab = SourceFileLoader('lab', '/Users/jackm4/goldenstocks/scripts/research/chip_lab.py').load_module()
import pandas as pd, numpy as np
SRC = [('主掃描·跨群線性', 'linear_combine_xs.csv'), ('對照組', 'linear_controls.csv'),
       ('換手權重與平滑', 'linear_extra.csv'), ('區塊平滑', 'linear_smooth.csv'),
       ('慢因子窮舉', 'linear_slow.csv'), ('穩健性', 'linear_robust.csv'),
       ('同口徑對照', 'linear_apples.csv'), ('前後半穩定性', 'linear_stability.csv')]
def line(r):
    return (f"{r.tag}|{r.turnover*100:.1f}|{r.long_gross:+.4f}|{r.long_t:+.2f}|"
            f"{r.long_net_ann:+.2f}|{r.spread_gross:+.4f}|{r.spread_t:+.2f}|{int(r.n_days)}")
all_rows = []
for nm, f in SRC:
    p = lab.DIR / f
    if not p.exists():
        print(f'### {nm} — 缺檔 {f}'); continue
    df = pd.read_csv(p)
    df = df[df.tag.notna() & df.long_gross.notna()]
    df['grp'] = nm
    all_rows.append(df)
    print(f'\n### {nm}（{len(df)} 組態） 檔案 {p}')
    if f == 'linear_slow.csv':
        cen = df[df.tag.str.startswith('[單]')]
        print('-- 單因子換手普查 --')
        for _, r in cen.iterrows(): print(line(r))
        cb = df[~df.tag.str.startswith('[單]')]
        cb = cb.assign(k=cb.tag.str.extract(r'\[慢(\d|全)')[0])
        print(f'-- 慢因子等權組合 {len(cb)} 組（分佈） --')
        print(cb.groupby('k')[['turnover','long_gross','long_t','long_net_ann']]
                .agg(['count','median','max']).round(4).to_string())
        pos = cb[cb.long_net_ann > 0].sort_values('long_net_ann', ascending=False)
        print(f'-- 其中淨值>0 的全部 {len(pos)} 組 --')
        for _, r in pos.iterrows(): print(line(r))
        print('-- 淨值最差 5 組 --')
        for _, r in cb.nsmallest(5, 'long_net_ann').iterrows(): print(line(r))
        continue
    for _, r in df.iterrows(): print(line(r))
A = pd.concat(all_rows)
A.to_csv(lab.DIR / 'linear_all_configs.csv', index=False)
print(f'\n### 總計 {len(A)} 組態；淨值>0 共 {(A.long_net_ann>0).sum()} 組')
print('-- 淨值前 15 --')
for _, r in A.nlargest(15, 'long_net_ann').iterrows(): print(line(r))
print('-- 多頭 gross 前 10 --')
for _, r in A.nlargest(10, 'long_gross').iterrows(): print(line(r))
