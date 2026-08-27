import sys, csv
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, 'src')
sys.path.insert(0, 'scripts/research')
from momentum_rotation_good_vs_bad_day_overnight_clues import load_all_days
from momentum_breakout_strategy import simulate_portfolio_day
import numpy as np

TICK_DIR = Path('reports/research/expert_pool_futures_tick')

tx_files = {}
for p in TICK_DIR.glob('tx_market_TX_tick_*.csv'):
    d = p.stem.split('tick_')[-1]
    tx_files[d] = p

def load_tx_ticks(path):
    rows = []
    with open(path) as f:
        r = csv.DictReader(f)
        for row in r:
            rows.append((row['date'], float(row['price'])))
    return rows

by_stock = load_all_days()
all_days = sorted(set().union(*[set(d.keys()) for d in by_stock.values()]))

tx_dates_sorted = sorted(tx_files.keys())

results = []
for i, d in enumerate(tx_dates_sorted):
    if i == 0:
        continue
    prev_d = tx_dates_sorted[i-1]
    # guard: only treat as "previous trading day's night session" if within 4 calendar days
    dt_d = datetime.strptime(d, '%Y-%m-%d')
    dt_prev = datetime.strptime(prev_d, '%Y-%m-%d')
    if (dt_d - dt_prev).days > 4:
        continue
    if d not in by_stock.get(list(by_stock.keys())[0], {}) and d not in all_days:
        pass
    if d not in all_days:
        continue

    # night open: first tick >= 15:00:00 in prev_d's file
    prev_ticks = load_tx_ticks(tx_files[prev_d])
    night_open_candidates = [p for (t, p) in prev_ticks if t[11:19] >= "15:00:00"]
    if not night_open_candidates:
        continue
    night_open = night_open_candidates[0]

    # night close: last tick <= 05:00:00 in d's file
    d_ticks = load_tx_ticks(tx_files[d])
    night_close_candidates = [p for (t, p) in d_ticks if t[11:19] <= "05:00:00"]
    if not night_close_candidates:
        continue
    night_close = night_close_candidates[-1]

    all_night_prices = [p for (t, p) in prev_ticks if t[11:19] >= "15:00:00"] + \
                        [p for (t, p) in d_ticks if t[11:19] <= "05:00:00"]
    night_high = max(all_night_prices)
    night_low = min(all_night_prices)

    night_ret_pct = (night_close - night_open) / night_open * 100
    night_range_pct = (night_high - night_low) / night_open * 100

    # momentum-rotation day_ret for date d
    day_data = {sid: days[d] for sid, days in by_stock.items() if d in days}
    if len(day_data) < 3:
        continue
    trades = simulate_portfolio_day(day_data)
    day_ret = sum(t['ret_pct'] for t in trades) if trades else 0.0
    n_trades = len(trades)

    results.append({
        'date': d, 'day_ret': day_ret, 'n_trades': n_trades,
        'night_ret_pct': night_ret_pct, 'night_range_pct': night_range_pct,
    })

print(f"配對成功 n={len(results)} 天（有TX夜盤資料+當天動能輪動回測結果）\n")
print(f"{'date':<12}{'day_ret%':>10}{'n_trades':>10}{'night_ret%':>12}{'night_range%':>14}")
for r in sorted(results, key=lambda x: x['day_ret']):
    print(f"{r['date']:<12}{r['day_ret']:>10.2f}{r['n_trades']:>10}{r['night_ret_pct']:>12.3f}{r['night_range_pct']:>14.3f}")

def corr(xs, ys):
    xa, ya = np.array(xs), np.array(ya:=ys)
    if xa.std() == 0 or ya.std() == 0: return None
    return float(np.corrcoef(xa, ya)[0,1])

day_rets = [r['day_ret'] for r in results]
print("\n=== Pearson相關（n小，觀察方向用）===")
print("day_ret vs night_ret_pct（夜盤方向）:", corr(day_rets, [r['night_ret_pct'] for r in results]))
print("day_ret vs night_range_pct（夜盤振幅/波動）:", corr(day_rets, [r['night_range_pct'] for r in results]))
print("day_ret vs |night_ret_pct|（夜盤位移幅度不分方向）:", corr(day_rets, [abs(r['night_ret_pct']) for r in results]))

n = len(results)
srt = sorted(results, key=lambda x: x['day_ret'])
tercile = max(1, n//3)
bad, good = srt[:tercile], srt[-tercile:]
def m(vals): return sum(vals)/len(vals)
print(f"\n差日子(n={len(bad)}) vs 好日子(n={len(good)}):")
print(f"  day_ret: 差={m([r['day_ret'] for r in bad]):.2f}%  好={m([r['day_ret'] for r in good]):.2f}%")
print(f"  night_range%: 差={m([r['night_range_pct'] for r in bad]):.3f}  好={m([r['night_range_pct'] for r in good]):.3f}")
print(f"  |night_ret%|: 差={m([abs(r['night_ret_pct']) for r in bad]):.3f}  好={m([abs(r['night_ret_pct']) for r in good]):.3f}")

print("\n=== 拆窗口內部重算（避免又是window效應假象）===")
oos = [r for r in results if r['date'] < '2026-01-01']
is_w = [r for r in results if r['date'] >= '2026-01-01']
for label, grp in [('OOS(2025-10~11)', oos), ('IS(2026-07~08)', is_w)]:
    if len(grp) < 5:
        print(f'{label}: n={len(grp)} 太少')
        continue
    xa = np.array([r['day_ret'] for r in grp])
    ya = np.array([r['night_range_pct'] for r in grp])
    c = np.corrcoef(xa, ya)[0,1] if xa.std()>0 and ya.std()>0 else float('nan')
    print(f"{label}: n={len(grp)} day_ret均值={xa.mean():.2f}% night_range均值={ya.mean():.3f} within-window corr={c:.3f}")
