#!/usr/bin/env python3
"""個股融資維持率 proxy — 崩盤族群斷頭風險排名。

維持率 proxy = 166.7% × 現價 / 融資流量加權成本 (融資6成→起始166.7%)。
融資加權成本 = 以每日 margin_change(張) 為權重的收盤價加權平均(買進加、賣出按比例減)。
資料:stock_margin_daily(融資餘額/增減) + stock_daily_bars(收盤)。
侷限:假設融資6成(忽略上櫃5成)、成本近似(seed 用期初價);方向性排名參考,非精確維持率。
用法:python scripts/research/chip_macro/stock_maint_proxy.py [--start 2026-01-01]
"""
from __future__ import annotations
import sqlite3, argparse
import pandas as pd, numpy as np
from pathlib import Path

DB = Path(__file__).resolve().parents[3] / "data/stocks.db"
LEV = 1 / 0.6 * 100  # 融資6成 → 起始維持率 166.7%
# 使用者持股 + 2026/07 崩盤族群龍頭
UNIVERSE = {'2327': '國巨', '2492': '華新科', '3189': '景碩', '3653': '健策', '6147': '頎邦',
            '6505': '台塑化', '8046': '南電', '2330': '台積電', '2454': '聯發科', '3481': '群創',
            '2409': '友達', '2408': '南亞科', '2344': '華邦電', '2337': '旺宏', '2881': '富邦金',
            '2892': '第一金', '2880': '華南金', '2887': '台新金', '3711': '日月光', '6239': '力成'}


def proxy(con, sid, start):
    m = pd.read_sql(f"SELECT trade_date,margin_balance,margin_change FROM stock_margin_daily WHERE stock_id='{sid}' AND trade_date>='{start}' ORDER BY trade_date", con)
    px = pd.read_sql(f"SELECT trade_date,close FROM stock_daily_bars WHERE stock_id='{sid}' AND trade_date>='{start}' ORDER BY trade_date", con)
    d = m.merge(px, on='trade_date', how='inner').dropna().reset_index(drop=True)
    if len(d) < 20:
        return None
    S = d['margin_balance'].iloc[0]; C = S * d['close'].iloc[0]
    out = []
    for i, r in d.iterrows():
        if i > 0:
            delta = r['margin_change'] if pd.notna(r['margin_change']) else 0
            if delta > 0:
                S += delta; C += delta * r['close']
            elif delta < 0 and S > 0:
                C *= max(0, (S + delta) / S); S = max(1, S + delta)
        out.append(C / S if S > 0 else np.nan)
    d['maint'] = LEV * d['close'] / pd.Series(out)
    last = d.iloc[-1]
    return {'maint': last['maint'], 'close': last['close'], 'avg_cost': C / S, 'min': d['maint'].min(), 'asof': last['trade_date']}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--start', default='2026-01-01'); a = ap.parse_args()
    con = sqlite3.connect(DB)
    rows = []
    for sid, nm in UNIVERSE.items():
        r = proxy(con, sid, a.start)
        if r:
            rows.append((r['maint'], sid, nm, r['close'], r['avg_cost'], r['min'], r['asof']))
    con.close()
    asof = rows[0][6] if rows else '?'
    print(f"個股融資維持率 proxy (as of {asof})\n{'股':<7}{'現價':>8}{'融資成本':>9}{'維持率':>8}{'期間低':>8}  斷頭風險")
    for mt, sid, nm, px, ac, mn, _ in sorted(rows):
        flag = '🔴<130斷頭區' if mt < 130 else ('🟠<150壓力' if mt < 150 else '🟢健康')
        print(f"{nm:<6}{px:>8.1f}{ac:>9.1f}{mt:>7.0f}%{mn:>7.0f}%  {flag}")
    print("\n註:proxy 假設融資6成、成本近似;方向性斷頭風險排名,非精確維持率。")


if __name__ == '__main__':
    main()
