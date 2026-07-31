#!/usr/bin/env python3
"""C3 重驗 — 用「扣除ETF」融資維持率 (wantgoo, 2024-07→2026-07, 490天)。

含ETF維持率(FinMind)被槓桿ETF(00631L 等)嚴重灌高;扣除ETF才是散戶個股融資盤的真斷頭溫度計。
本檔:(1)存檔 exclude-ETF維持率 (2)對照含ETF (3)三次崩盤案例 (4)門檻事件研究 (5)當下判讀。
資料短(490天/3崩盤),統計力有限,以案例+方向性為主,permutation 僅供參考。
"""
from __future__ import annotations
import numpy as np, pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RNG = np.random.default_rng(20260731); NPERM = 5000


def load() -> pd.DataFrame:
    ex = pd.read_csv(ROOT / 'data/research/chip_macro/margin_maintenance_exetf.csv')
    if 'maint_ex' not in ex.columns and 'maint' in ex.columns:
        ex = ex.rename(columns={'maint': 'maint_ex'})  # tracker 快取為單欄 date,maint
    ex['date'] = pd.to_datetime(ex['date']); ex['maint_ex'] = ex['maint_ex'].astype(float)
    # panel TAIEX + 含ETF維持率
    p = pd.read_parquet(ROOT / 'data/research/chip_macro/panel.parquet')[['date', 'ix_close', 'fut_foreign_doi']].copy()
    p['date'] = pd.to_datetime(p['date'])
    inc = pd.read_parquet(ROOT / 'data/research/chip_macro/margin_maintenance.parquet')  # 含ETF (FinMind)
    inc['date'] = pd.to_datetime(inc['date']); inc = inc.rename(columns={'maint': 'maint_inc'})
    df = p.merge(ex[['date', 'maint_ex']], on='date', how='inner').merge(inc, on='date', how='left').sort_values('date').reset_index(drop=True)
    for h in (1, 3, 5, 10, 20):
        df[f'fwd{h}'] = df['ix_close'].shift(-h) / df['ix_close'] - 1
    return df


def perm_p(mask, fwd):
    v = fwd.notna(); pool = fwd[v].values; k = int(mask[v].sum())
    if k < 3: return np.nan
    obs = fwd[v & mask].mean()
    return (sum(RNG.choice(pool, k, replace=False).mean() >= obs for _ in range(NPERM)) + 1) / (NPERM + 1)


def main():
    df = load()
    print(f"=== 扣除ETF維持率 C3 重驗 ===  n={len(df)}  {df['date'].min().date()}→{df['date'].max().date()}")
    print(f"扣除ETF維持率 範圍 {df['maint_ex'].min():.1f}~{df['maint_ex'].max():.1f} | 最新(7/29) {df['maint_ex'].iloc[-1]:.1f}")

    # (1) 含ETF vs 扣除ETF 對照
    d2 = df.dropna(subset=['maint_inc'])
    spread = d2['maint_inc'] - d2['maint_ex']
    print(f"\n[含ETF vs 扣除ETF] 平均差(含-扣) = {spread.mean():.1f}pp (範圍 {spread.min():.1f}~{spread.max():.1f})")
    print(f"  最新 含ETF {d2['maint_inc'].iloc[-1]:.1f}% vs 扣除ETF {d2['maint_ex'].iloc[-1]:.1f}% → 差 {spread.iloc[-1]:.1f}pp")
    print(f"  相關(水準) {d2['maint_inc'].corr(d2['maint_ex']):.3f}")

    # (2) 三次崩盤案例:維持率谷底 & 對應指數
    print("\n[三次崩盤 維持率谷底]")
    for name, s, e in [('2024/08 日圓套利', '2024-07-25', '2024-08-15'),
                       ('2025/04 關稅', '2025-03-25', '2025-04-25'),
                       ('2026/07 本次', '2026-07-10', '2026-07-29')]:
        w = df[(df['date'] >= s) & (df['date'] <= e)]
        i = w['maint_ex'].idxmin()
        trough = df.loc[i]
        ix_after10 = df['ix_close'].shift(-10).loc[i] / trough['ix_close'] - 1 if i + 10 < len(df) else np.nan
        print(f"  {name}: 維持率谷底 {trough['maint_ex']:.1f}% @ {str(trough['date'])[:10]} (指數 {trough['ix_close']:.0f})"
              + (f" → 谷底後10日 {ix_after10*100:+.1f}%" if not np.isnan(ix_after10) else " (谷底=現在,未來未知)"))

    # (3) 門檻事件研究:扣除ETF維持率 首次跌破 X (避免連續日重複計數)
    print("\n[門檻事件研究] 扣除ETF維持率 由上而下首次跌破門檻後 TAIEX 前瞻報酬")
    print(f"  Baseline fwd10 = {df['fwd10'].mean()*100:+.2f}% (hit {(df['fwd10']>0).mean()*100:.0f}%)")
    for thr in (140, 135, 130, 125, 120):
        below = df['maint_ex'] < thr
        cross = below & ~below.shift(1, fill_value=False)  # 首次跌破
        m = cross.fillna(False)
        n = int(m.sum())
        if n == 0:
            print(f"  <{thr}%: 無事件"); continue
        line = f"  <{thr}% (n={n}):"
        for h in (5, 10, 20):
            sub = df[f'fwd{h}'][m & df[f'fwd{h}'].notna()]
            line += f" h{h} {sub.mean()*100:+.1f}%/命中{(sub>0).mean()*100:.0f}%/p={perm_p(m, df[f'fwd{h}']):.3f}"
        print(line)

    # 也做「維持率 5日急降 底5%」對照
    df['mex_d5'] = df['maint_ex'].diff(5)
    m = (df['mex_d5'] < df['mex_d5'].quantile(0.05)).fillna(False)
    line = f"  [對照]維持率5日急降底5% (n={int(m.sum())}):"
    for h in (5, 10, 20):
        sub = df[f'fwd{h}'][m & df[f'fwd{h}'].notna()]
        line += f" h{h} {sub.mean()*100:+.1f}%/p={perm_p(m, df[f'fwd{h}']):.3f}"
    print(line)

    # (4) 當下判讀
    last = df.iloc[-1]
    print(f"\n[當下 7/29] 扣除ETF維持率 = {last['maint_ex']:.1f}% (追繳130/斷頭120)")
    print(f"  近5日: {', '.join(f'{r.maint_ex:.1f}' for r in df.tail(5).itertuples())}")
    doi = df['fut_foreign_doi']; streak = 0
    for v in doi.iloc[::-1]:
        if pd.notna(v) and v > 0: streak += 1
        else: break
    print(f"  外資期貨 doi 回補連續 {streak} 日")
    print(f"  對照2025/04:曾殺到117.5%(破斷頭)才見底反彈;本次131.7%尚未到該深度")


if __name__ == '__main__':
    main()
