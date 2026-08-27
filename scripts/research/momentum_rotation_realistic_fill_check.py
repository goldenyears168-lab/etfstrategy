"""2026-08-13重大發現：simulate_portfolio_day用「理論觸發價」記fill、卻用
「訊號當下實際tick價」算overshoot/score——兩者對不上，尤其preemption進場
（score越高代表overshoot越大，backtest卻假裝零滑價精準買在起漲點，現實不可能）。

這裡重跑一次backtest，唯一差異是fill改用訊號當下的實際tick價（不是理論觸發價）。
結果：勝率86.9%→40.2%（比丟銅板還差）、日均+22.8%→+1.67%（還沒扣真實成本）。
見reports/research/momentum_breakout_expert_pool_futures_summary.md開頭的
2026-08-13更新章節。

跟scripts/research/momentum_breakout_strategy.py::simulate_portfolio_day刻意
獨立、不共用程式碼——這裡是要「複製並改一個假設」做對照實驗，不是要維護一份
生產邏輯，兩份重複但用途不同。
"""

import sys
from pathlib import Path
sys.path.insert(0, 'src')
sys.path.insert(0, 'scripts/research')
from momentum_rotation_good_vs_bad_day_overnight_clues import load_all_days
import numpy as np, statistics

BREAKOUT_PCT, TRAIL_PCT, VOL_CONFIRM_MULT = 0.5, 1.0, 1.5
REARM_PCT, MIN_OVERSHOOT_PCT, MIN_VOL_RATIO, PREEMPT_MULT = 0.25, 0.15, 1.5, 2.0

def simulate_day(day_data, *, realistic_fill):
    """realistic_fill=False複製原本backtest邏輯（fill=理論trigger）；
    realistic_fill=True改用訊號當下的實際tick價p當fill（比較貼近live限價單
    「用當下市價+緩衝」的精神，不是理論觸發價）。"""
    meta, merged = {}, []
    for sid, (times, prices, volumes) in day_data.items():
        if prices.size < 2: continue
        open_price = float(prices[0])
        meta[sid] = {
            "open": open_price,
            "long_trigger": open_price * (1+BREAKOUT_PCT/100), "short_trigger": open_price * (1-BREAKOUT_PCT/100),
            "rearm_hi": open_price * (1+REARM_PCT/100), "rearm_lo": open_price * (1-REARM_PCT/100),
        }
        for k in range(1, len(times)):
            merged.append((times[k], sid, float(prices[k]), float(volumes[k])))
    merged.sort(key=lambda x: x[0])

    vol_history = {sid: [] for sid in meta}
    armed = {sid: True for sid in meta}
    last_price = {sid: meta[sid]["open"] for sid in meta}
    trades, position = [], None

    for t, sid, p, v in merged:
        st = meta[sid]; last_price[sid] = p
        vh = vol_history[sid]
        baseline = max(statistics.median(vh), 1e-9) if vh else 1.0
        is_held = position is not None and position["sid"] == sid
        if is_held:
            if position["direction"] == "long":
                position["peak_trough"] = max(position["peak_trough"], p)
                stop = position["peak_trough"] * (1-TRAIL_PCT/100); hit = p <= stop
            else:
                position["peak_trough"] = min(position["peak_trough"], p)
                stop = position["peak_trough"] * (1+TRAIL_PCT/100); hit = p >= stop
            if hit:
                exit_price = stop
                ret = (exit_price-position["fill"])/position["fill"]*100 if position["direction"]=="long" else (position["fill"]-exit_price)/position["fill"]*100
                trades.append({**position, "exit_time": t, "exit": exit_price, "ret_pct": ret, "reason": "trail_stop"})
                position = None; armed[sid] = False
            vh.append(v); continue
        if not armed[sid]:
            if st["rearm_lo"] <= p <= st["rearm_hi"]: armed[sid] = True
            vh.append(v); continue
        hits_long, hits_short = p >= st["long_trigger"], p <= st["short_trigger"]
        if not (hits_long or hits_short) or v < VOL_CONFIRM_MULT*baseline:
            vh.append(v); continue
        direction = "long" if hits_long else "short"
        trigger_fill = st["long_trigger"] if direction=="long" else st["short_trigger"]
        overshoot = abs(p-trigger_fill)/st["open"]*100
        vol_ratio = v/baseline
        if overshoot < MIN_OVERSHOOT_PCT or vol_ratio < MIN_VOL_RATIO:
            vh.append(v); continue
        score = overshoot*vol_ratio
        actual_fill = p if realistic_fill else trigger_fill  # <-- 關鍵差異
        candidate = {"sid": sid, "direction": direction, "fill": actual_fill, "entry_time": t,
                     "entry_score": score, "peak_trough": actual_fill, "overshoot": overshoot, "vol_ratio": vol_ratio}
        if position is None:
            position = candidate; armed[sid] = False
        elif score >= PREEMPT_MULT*position["entry_score"]:
            held_sid = position["sid"]; exit_price = last_price[held_sid]
            ret = (exit_price-position["fill"])/position["fill"]*100 if position["direction"]=="long" else (position["fill"]-exit_price)/position["fill"]*100
            trades.append({**position, "exit_time": t, "exit": exit_price, "ret_pct": ret, "reason": "preempted"})
            armed[held_sid] = False; position = candidate; armed[sid] = False
        vh.append(v)

    if position is not None:
        exit_price = last_price[position["sid"]]
        ret = (exit_price-position["fill"])/position["fill"]*100 if position["direction"]=="long" else (position["fill"]-exit_price)/position["fill"]*100
        trades.append({**position, "exit_time": "day_end", "exit": exit_price, "ret_pct": ret, "reason": "day_end_forced"})
    return trades

by_stock = load_all_days()
all_days = sorted(set().union(*[set(d.keys()) for d in by_stock.values()]))

for label, realistic in [("原本backtest假設(fill=理論trigger)", False), ("寫實假設(fill=訊號當下實際tick價)", True)]:
    all_trades = []
    for d in all_days:
        day_data = {sid: days[d] for sid, days in by_stock.items() if d in days}
        if len(day_data) < 3: continue
        all_trades.extend(simulate_day(day_data, realistic_fill=realistic))
    rets = [t["ret_pct"] for t in all_trades]
    win = sum(1 for r in rets if r>0)/len(rets)*100 if rets else 0
    n_days = len(all_days)
    print(f"{label}: n={len(rets)} 勝率={win:.1f}% 均值={np.mean(rets):.3f}% 加總={sum(rets):.1f}% 日均={sum(rets)/n_days:.3f}%")
