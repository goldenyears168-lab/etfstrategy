#!/usr/bin/env python3
"""C3 重驗:融資維持率(真實) vs 融資投降(餘額),誰是較好的止跌訊號?

- 維持率來源: FinMind TaiwanTotalExchangeMarginMaintenance(2018→今, 真實官方整戶擔保維持率)。
- 對照: panel.parquet 的 ix_close(TAIEX) + margin_bal(融資餘額張)。
- 方法: 事件後 TAIEX 前瞻報酬(h1/3/5/10/20) + block-permutation p(對照隨機同數量日期) + 多空 regime 分層。
- 嚴防 look-ahead: 用 t 日盤後值 → t+1 起的前瞻報酬。
"""
from __future__ import annotations
import sys, requests
import numpy as np, pandas as pd
from dotenv import dotenv_values

FM = dotenv_values('.env').get('FINMIND_TOKEN', '')
PANEL = 'data/research/chip_macro/panel.parquet'
RNG = np.random.default_rng(20260730)
NPERM = 5000


def fetch_maint() -> pd.DataFrame:
    r = requests.get('https://api.finmindtrade.com/api/v4/data',
                     params={'dataset': 'TaiwanTotalExchangeMarginMaintenance', 'start_date': '2018-01-01', 'token': FM}, timeout=60).json()
    d = pd.DataFrame(r['data'])
    d['date'] = pd.to_datetime(d['date'])
    d = d.rename(columns={'TotalExchangeMarginMaintenance': 'maint'})[['date', 'maint']]
    d.to_parquet('data/research/chip_macro/margin_maintenance.parquet')
    return d


def build() -> pd.DataFrame:
    p = pd.read_parquet(PANEL)[['date', 'ix_close', 'margin_bal']].copy()
    p['date'] = pd.to_datetime(p['date'])
    m = fetch_maint()
    df = p.merge(m, on='date', how='left').sort_values('date').reset_index(drop=True)
    df['maint'] = df['maint'].ffill()
    # forward returns
    for h in (1, 3, 5, 10, 20):
        df[f'fwd{h}'] = df['ix_close'].shift(-h) / df['ix_close'] - 1
    # signals
    df['maint_z60'] = (df['maint'] - df['maint'].rolling(60).mean()) / df['maint'].rolling(60).std()
    df['maint_d5'] = df['maint'].diff(5)
    df['mb_z60'] = (df['margin_bal'] - df['margin_bal'].rolling(60).mean()) / df['margin_bal'].rolling(60).std()
    df['mb_d1'] = df['margin_bal'].diff()
    df['ma200'] = df['ix_close'].rolling(200).mean()
    df['bull'] = df['ix_close'] > df['ma200']
    return df


def perm_p(mask: pd.Series, fwd: pd.Series, n_event_mean: float) -> float:
    """事件日前瞻報酬均值 vs 隨機同數量日期的分布。單尾(事件較高)。"""
    valid = fwd.notna()
    pool = fwd[valid].values
    k = int(mask[valid].sum())
    if k < 5:
        return np.nan
    obs = fwd[valid & mask].mean()
    cnt = 0
    for _ in range(NPERM):
        samp = RNG.choice(pool, size=k, replace=False)
        if samp.mean() >= obs:
            cnt += 1
    return (cnt + 1) / (NPERM + 1)


def report_signal(df, name, mask):
    print(f"\n### {name}  (n={int(mask.sum())})")
    hdr = f"{'horizon':<8}{'mean':>9}{'hit%':>7}{'perm_p':>9}   {'bull_mean':>10}{'bear_mean':>10}"
    print(hdr)
    for h in (1, 3, 5, 10, 20):
        fwd = df[f'fwd{h}']
        sub = fwd[mask & fwd.notna()]
        mean = sub.mean() * 100
        hit = (sub > 0).mean() * 100
        pp = perm_p(mask, fwd, mask.sum())
        bull = fwd[mask & df['bull'] & fwd.notna()].mean() * 100
        bear = fwd[mask & (~df['bull']) & fwd.notna()].mean() * 100
        print(f"h{h:<7}{mean:>+8.2f}%{hit:>6.0f}%{pp:>9.3f}   {bull:>+9.2f}%{bear:>+9.2f}%")


def main():
    df = build()
    print(f"資料 {df['date'].min().date()}→{df['date'].max().date()} n={len(df)}")
    print(f"維持率 範圍 {df['maint'].min():.1f}~{df['maint'].max():.1f}, 最新 {df['maint'].iloc[-1]:.1f}")
    # baseline
    base = df['fwd10'].mean() * 100
    print(f"\nBaseline 無條件 fwd10 = {base:+.2f}% (hit {(df['fwd10']>0).mean()*100:.0f}%)")

    q_low = df['maint'].quantile(0.05)
    print(f"維持率 5% 分位 = {q_low:.1f}%")

    signals = {
        '① 維持率 z60 < -1.5 (相對急殺)': df['maint_z60'] < -1.5,
        '② 維持率 z60 < -2 (投降級)': df['maint_z60'] < -2,
        '③ 維持率 絕對 < 150% (近追繳)': df['maint'] < 150,
        '④ 維持率 絕對 < 145%': df['maint'] < 145,
        '⑤ 維持率 5日急降 底5%分位': df['maint_d5'] < df['maint_d5'].quantile(0.05),
        '── 對照組(原 C3 融資餘額投降)──': None,
        'C3a 融資餘額 z60 < -2': df['mb_z60'] < -2,
        'C3b 融資單日失血 底5%': df['mb_d1'] < df['mb_d1'].quantile(0.05),
    }
    for name, mask in signals.items():
        if mask is None:
            print(f"\n{name}")
            continue
        m = mask.fillna(False)
        report_signal(df, name, m)

    print("\n[判讀] 若維持率訊號 permutation p 顯著(<0.05)且不像 C3 那樣空頭翻負,則 C3 被救回:")
    print("斷頭壓力(維持率)比餘額量能(融資投降)是更好的止跌訊號。反之則 C3 確定出局。")


if __name__ == '__main__':
    main()
