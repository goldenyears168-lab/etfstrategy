#!/usr/bin/env python3
import sys
sys.path.insert(0, '/Users/jackm4/goldenstocks/scripts/research')
from importlib.machinery import SourceFileLoader
lab = SourceFileLoader('lab', '/Users/jackm4/goldenstocks/scripts/research/chip_lab.py').load_module()
import pandas as pd
pd.set_option('display.width', 200, 'display.max_rows', 400)
fs = ['linear_combine_xs.csv', 'linear_controls.csv', 'linear_extra.csv']
df = pd.concat([pd.read_csv(lab.DIR / f).assign(src=f) for f in fs if (lab.DIR / f).exists()])
df = df[df.tag.notna()]
df['turn%'] = df.turnover * 100
out = df[['tag', 'turn%', 'long_gross', 'long_t', 'long_net_ann',
          'spread_gross', 'spread_t', 'breakeven_cost', 'n_days']].round(4)
out.to_csv(lab.DIR / 'linear_all_configs.csv', index=False)
print(f'總組態 {len(out)}')
print('\n=== 依 淨值/年 排序 前 20 ===')
print(out.sort_values('long_net_ann', ascending=False).head(20).to_string(index=False))
print('\n=== 依 多頭 gross 排序 前 15 ===')
print(out.sort_values('long_gross', ascending=False).head(15).to_string(index=False))
print('\n=== 淨值 > 0 的組態 ===')
print(out[out.long_net_ann > 0].to_string(index=False))
print('\n=== 換手 vs gross（依群數） ===')
df['k'] = df.tag.str.extract(r'\[(\d)群')
print(df.groupby('k')[['turnover', 'long_gross', 'long_net_ann', 'breakeven_cost']].mean().round(4).to_string())
