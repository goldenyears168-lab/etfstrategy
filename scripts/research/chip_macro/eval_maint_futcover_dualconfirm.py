#!/usr/bin/env python3
"""雙確認底回測:扣除ETF維持率(價的投降) × 外資期貨 doi 回補(領先確認)。

比較三者事件後 TAIEX 前瞻報酬:
  (A) 維持率-only : 維持率 < X
  (B) 外資回補-only: fut_foreign_doi 連 >=2 日 > 0
  (C) 雙確認       : (A) AND (B) 同日
假設:雙確認避免「維持率斷頭但外資還在殺」的接刀,較 A 單獨更晚但更準。
資料短(2024-07→2026-07,3崩盤),以案例+方向為主;permutation 用同期隨機。
"""
from __future__ import annotations
import numpy as np, pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RNG = np.random.default_rng(20260731); NPERM = 5000


def build():
    ex = pd.read_csv(ROOT / 'data/research/chip_macro/margin_maintenance_exetf.csv')
    if 'maint_ex' not in ex.columns and 'maint' in ex.columns:
        ex = ex.rename(columns={'maint': 'maint_ex'})
    ex['date'] = pd.to_datetime(ex['date'])
    p = pd.read_parquet(ROOT / 'data/research/chip_macro/panel.parquet')[['date', 'ix_close', 'fut_foreign_doi']].copy()
    p['date'] = pd.to_datetime(p['date'])
    df = p.merge(ex[['date', 'maint_ex']], on='date', how='inner').sort_values('date').reset_index(drop=True)
    for h in (1, 3, 5, 10, 20):
        df[f'fwd{h}'] = df['ix_close'].shift(-h) / df['ix_close'] - 1
    # 外資期貨回補連續天數
    pos = df['fut_foreign_doi'] > 0
    streak = np.zeros(len(df), int)
    for i in range(len(df)):
        streak[i] = streak[i-1] + 1 if i > 0 and pos.iloc[i] else (1 if pos.iloc[i] else 0)
    df['cover_streak'] = streak
    df['cover2'] = df['cover_streak'] >= 2
    return df


def perm_p(mask, fwd):
    v = fwd.notna(); pool = fwd[v].values; k = int(mask[v].sum())
    if k < 3: return np.nan
    obs = fwd[v & mask].mean()
    return (sum(RNG.choice(pool, k, replace=False).mean() >= obs for _ in range(NPERM)) + 1) / (NPERM + 1)


def summ(df, m, tag):
    n = int(m.sum())
    if n == 0:
        print(f"  {tag}: 無事件"); return
    line = f"  {tag} (n={n}):"
    for h in (5, 10, 20):
        s = df[f'fwd{h}'][m & df[f'fwd{h}'].notna()]
        line += f" h{h} {s.mean()*100:+.1f}%/命中{(s>0).mean()*100:.0f}%/p={perm_p(m, df[f'fwd{h}']):.3f}"
    print(line)


def main():
    df = build()
    print(f"=== 雙確認底回測 (維持率 × 外資期貨回補) ===  n={len(df)}  {df['date'].min().date()}→{df['date'].max().date()}")
    print(f"Baseline fwd10 = {df['fwd10'].mean()*100:+.2f}% (hit {(df['fwd10']>0).mean()*100:.0f}%)\n")

    for X in (140, 135, 130):
        below = df['maint_ex'] < X
        A = below                                  # 維持率-only (每日在區內)
        C = below & df['cover2']                   # 雙確認
        print(f"[門檻 <{X}%]")
        summ(df, A.fillna(False), f"A 維持率<{X}-only    ")
        summ(df, C.fillna(False), f"C 雙確認(+外資回補≥2)")
    print("[外資回補-only]")
    summ(df, df['cover2'].fillna(False), "B 外資期貨回補≥2日")

    # 三崩盤:維持率入區日 vs 外資首次回補確認日 → 時間差 & 各自進場後報酬
    print("\n[三崩盤 時間差:維持率<140入區 → 外資回補≥2確認]")
    for name, s, e in [('2024/08', '2024-07-25', '2024-08-30'),
                       ('2025/04', '2025-03-25', '2025-05-05'),
                       ('2026/07', '2026-07-10', '2026-07-29')]:
        w = df[(df['date'] >= s) & (df['date'] <= e)].reset_index(drop=True)
        below = w[w['maint_ex'] < 140]
        dual = w[(w['maint_ex'] < 140) & w['cover2']]
        d_in = str(below['date'].iloc[0])[:10] if len(below) else '—'
        maint_min = w['maint_ex'].min()
        d_dual = str(dual['date'].iloc[0])[:10] if len(dual) else '(區間內未雙確認)'
        # 進場後10日
        def fwd10_from(dt):
            idx = df.index[df['date'] == pd.Timestamp(dt)]
            if len(idx) and idx[0]+10 < len(df):
                return df['ix_close'].iloc[idx[0]+10]/df['ix_close'].iloc[idx[0]]-1
            return np.nan
        r_in = fwd10_from(below['date'].iloc[0]) if len(below) else np.nan
        r_dual = fwd10_from(dual['date'].iloc[0]) if len(dual) else np.nan
        print(f"  {name}: 維持率<140入區 {d_in}(谷底{maint_min:.1f}%, 入區後10日 {r_in*100:+.1f}%) | "
              f"雙確認 {d_dual}" + (f"(後10日 {r_dual*100:+.1f}%)" if not np.isnan(r_dual) else ""))

    last = df.iloc[-1]
    d130 = '✓' if last['maint_ex'] < 130 else f'✗(差{last["maint_ex"]-130:.1f}pp)'
    dual_now = '成立' if (last['maint_ex'] < 130 and last['cover2']) else '未成立(外資未回補)'
    print(f"\n[當下 7/29] 維持率 {last['maint_ex']:.1f}% (<140✓ <130 {d130}) "
          f"| 外資回補連續 {int(last['cover_streak'])} 日 → 雙確認 {dual_now}")


if __name__ == '__main__':
    main()
