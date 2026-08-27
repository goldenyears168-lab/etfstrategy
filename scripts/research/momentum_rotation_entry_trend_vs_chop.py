import sys
from pathlib import Path
from datetime import datetime, timedelta
sys.path.insert(0, 'src')
sys.path.insert(0, 'scripts/research')
from momentum_rotation_good_vs_bad_day_overnight_clues import load_all_days
from momentum_breakout_strategy import simulate_portfolio_day
import numpy as np, random

random.seed(7)  # 允許用固定seed做隨機基準抽樣，不影響策略本身邏輯

by_stock = load_all_days()
all_days = sorted(set().union(*[set(d.keys()) for d in by_stock.values()]))

LOOKBACK_MIN = 10

def parse_t(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")

def efficiency_ratio(times, prices, end_idx, lookback_min):
    end_t = parse_t(times[end_idx])
    start_t = end_t - timedelta(minutes=lookback_min)
    lo = end_idx
    while lo > 0 and parse_t(times[lo-1]) >= start_t:
        lo -= 1
    window_p = prices[lo:end_idx+1]
    if len(window_p) < 5:
        return None
    net_move = window_p[-1] - window_p[0]
    path_len = np.sum(np.abs(np.diff(window_p)))
    if path_len <= 0:
        return None
    return float(net_move), float(path_len), float(abs(net_move) / path_len)

entry_results = []
random_results = []

for d in all_days:
    day_data = {sid: days[d] for sid, days in by_stock.items() if d in days}
    if len(day_data) < 3:
        continue
    trades = simulate_portfolio_day(day_data)
    for t in trades:
        sid = t['sid']
        times, prices, _vols = day_data[sid]
        entry_t = t['entry_time']
        # 找到entry_time在該檔times裡的index（entry_time本身就是一個tick時間戳）
        try:
            idx = times.index(entry_t)
        except ValueError:
            # entry_time可能是long_trigger/short_trigger價位觸發當下，用第一個>=的tick
            idx = next((i for i, tt in enumerate(times) if tt >= entry_t), None)
        if idx is None or idx < 5:
            continue
        res = efficiency_ratio(times, prices, idx - 1, LOOKBACK_MIN)  # idx-1: 不含觸發那一筆本身，看它之前
        if res is None:
            continue
        net_move, path_len, er = res
        direction = t['direction']
        aligned = (net_move > 0 and direction == 'long') or (net_move < 0 and direction == 'short')
        entry_results.append({'er': er, 'aligned': aligned, 'net_move': net_move, 'ret_pct': t['ret_pct']})

    # 隨機基準：同一天、同樣檔數的隨機時間點（跟entry數量對齊，避免抽樣量不對等）
    n_sample = len(trades) if trades else 3
    for _ in range(n_sample):
        sid = random.choice(list(day_data.keys()))
        times, prices, _vols = day_data[sid]
        if len(times) < 50:
            continue
        idx = random.randint(30, len(times) - 1)
        res = efficiency_ratio(times, prices, idx, LOOKBACK_MIN)
        if res is None:
            continue
        _net, _path, er = res
        random_results.append(er)

ers = [r['er'] for r in entry_results]
aligned_frac = sum(1 for r in entry_results if r['aligned']) / len(entry_results)
print(f"進場筆數 n={len(entry_results)}")
print(f"進場前{LOOKBACK_MIN}分鐘 efficiency ratio：均值={np.mean(ers):.3f} 中位數={np.median(ers):.3f}")
print(f"進場前{LOOKBACK_MIN}分鐘價格淨位移方向 跟 這筆交易方向一致的比例：{aligned_frac*100:.1f}%")
print(f"\n隨機時間點基準（同天同檔數抽樣，n={len(random_results)}）：")
print(f"efficiency ratio：均值={np.mean(random_results):.3f} 中位數={np.median(random_results):.3f}")

# 依efficiency ratio分組看勝率/報酬（高ER=進場前是乾淨趨勢, 低ER=進場前是雜訊來回）
ers_arr = np.array(ers)
median_er = np.median(ers_arr)
high = [r for r in entry_results if r['er'] >= median_er]
low = [r for r in entry_results if r['er'] < median_er]
def stats(rows):
    rets = [r['ret_pct'] for r in rows]
    win = sum(1 for r in rets if r > 0) / len(rets) * 100
    return len(rows), win, np.mean(rets)
n_h, win_h, mean_h = stats(high)
n_l, win_l, mean_l = stats(low)
print(f"\n高ER組（進場前乾淨趨勢, n={n_h}）：勝率={win_h:.1f}% 均值ret={mean_h:.3f}%")
print(f"低ER組（進場前雜訊/來回, n={n_l}）：勝率={win_l:.1f}% 均值ret={mean_l:.3f}%")
