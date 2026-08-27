"""2026-08-13：retest-entry（延遲進場）4折holdout 2勝2負，不夠穩健。這裡改測
「延遲進場」的鏡像版本——不動進場時機，改在**出場端**設一個min_hold_sec：
部位進場後min_hold_sec秒內，不管是trailing stop還是搶佔都不能把它踢出場，
撐過這段保護期才恢復正常規則。動機直接來自完整75天樣本的持有時長分析
（momentum_rotation_hold_duration_edge.py）：<15秒出場的部位(不分trail_stop
還是preempted)勝率只有10.3%、損平-36.2bps，且是單調關係——這裡直接測試
「不准太早出場」這個最小、最貼近原始發現的修改，而不是重新設計進場邏輯。

這次從一開始就用4折留一窗口交叉驗證（吸取retest-entry的教訓：不要先報樣本內
數字、之後才做holdout，這裡sweep跟驗證都在同一支腳本、同一次跑）。
"""

from __future__ import annotations

import sys
from datetime import datetime

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_rotation_redesign_search import WINDOWS, load_window  # noqa: E402

MIN_HOLD_GRID = [10.0, 15.0, 20.0, 30.0, 45.0, 60.0, 90.0, 120.0]
BASE = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
            min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0)


def simulate_day_min_hold(
    stock_day_data: dict, *,
    breakout_pct: float, trail_pct: float, vol_confirm_mult: float, rearm_pct: float,
    min_overshoot_pct: float, min_vol_ratio: float, preempt_mult: float, min_hold_sec: float,
) -> list[dict]:
    merged: list[tuple] = []
    meta: dict = {}
    for sid, (times, prices, volumes) in stock_day_data.items():
        if prices.size < 2:
            continue
        open_price = float(prices[0])
        meta[sid] = {
            "open": open_price,
            "long_trigger": open_price * (1 + breakout_pct / 100.0),
            "short_trigger": open_price * (1 - breakout_pct / 100.0),
            "rearm_hi": open_price * (1 + rearm_pct / 100.0),
            "rearm_lo": open_price * (1 - rearm_pct / 100.0),
        }
        for k in range(1, len(times)):
            merged.append((times[k], sid, float(prices[k]), float(volumes[k])))
    merged.sort(key=lambda x: x[0])

    vol_history = {sid: [] for sid in meta}
    armed = {sid: True for sid in meta}
    last_price = {sid: meta[sid]["open"] for sid in meta}
    trades: list[dict] = []
    position: dict | None = None

    for t, sid, p, v in merged:
        st = meta[sid]
        last_price[sid] = p
        vh = vol_history[sid]
        baseline = max(np.median(vh), 1e-9) if vh else 1.0
        vh.append(v)

        is_held = position is not None and position["sid"] == sid
        if is_held:
            elapsed = (datetime.fromisoformat(t) - datetime.fromisoformat(position["entry_time"])).total_seconds()
            protected = elapsed < min_hold_sec
            if not protected:
                if position["direction"] == "long":
                    position["peak_trough"] = max(position["peak_trough"], p)
                    stop = position["peak_trough"] * (1 - trail_pct / 100.0)
                    hit = p <= stop
                else:
                    position["peak_trough"] = min(position["peak_trough"], p)
                    stop = position["peak_trough"] * (1 + trail_pct / 100.0)
                    hit = p >= stop
                if hit:
                    exit_price = float(p)
                    ret_pct = (
                        (exit_price - position["fill"]) / position["fill"] * 100.0
                        if position["direction"] == "long"
                        else (position["fill"] - exit_price) / position["fill"] * 100.0
                    )
                    trades.append({**position, "exit_time": t, "exit": exit_price, "ret_pct": ret_pct, "reason": "trail_stop"})
                    position = None
                    armed[sid] = False
            else:
                # 保護期內仍要更新peak_trough，避免保護期一過就用過期的極值誤判
                if position["direction"] == "long":
                    position["peak_trough"] = max(position["peak_trough"], p)
                else:
                    position["peak_trough"] = min(position["peak_trough"], p)
            continue

        if not armed[sid]:
            if st["rearm_lo"] <= p <= st["rearm_hi"]:
                armed[sid] = True
            continue

        price_hits_long = p >= st["long_trigger"]
        price_hits_short = p <= st["short_trigger"]
        if not (price_hits_long or price_hits_short) or v < vol_confirm_mult * baseline:
            continue
        direction = "long" if price_hits_long else "short"
        trigger = st["long_trigger"] if direction == "long" else st["short_trigger"]
        overshoot = abs(p - trigger) / st["open"] * 100.0
        vol_ratio = v / baseline
        if overshoot < min_overshoot_pct or vol_ratio < min_vol_ratio:
            continue
        score = overshoot * vol_ratio
        fill = float(p)
        candidate = {
            "sid": sid, "direction": direction, "fill": fill, "entry": fill,
            "entry_time": t, "entry_score": score, "peak_trough": fill,
            "overshoot": overshoot, "vol_ratio": vol_ratio,
        }

        if position is None:
            position = candidate
            armed[sid] = False
        else:
            held_elapsed = (datetime.fromisoformat(t) - datetime.fromisoformat(position["entry_time"])).total_seconds()
            held_protected = held_elapsed < min_hold_sec
            if not held_protected and score >= preempt_mult * position["entry_score"]:
                held_sid = position["sid"]
                exit_price = last_price[held_sid]
                ret_pct = (
                    (exit_price - position["fill"]) / position["fill"] * 100.0
                    if position["direction"] == "long"
                    else (position["fill"] - exit_price) / position["fill"] * 100.0
                )
                trades.append({**position, "exit_time": t, "exit": exit_price, "ret_pct": ret_pct, "reason": "preempted"})
                armed[held_sid] = False
                position = candidate
                armed[sid] = False
            # 保護期內：新候選訊號直接放棄（沒有排隊機制，比照這個repo一貫的
            # "不夠格就跳過"設計，不是這次要驗證的重點，先用最簡單版本測主效果）

    if position is not None:
        exit_price = last_price[position["sid"]]
        ret_pct = (
            (exit_price - position["fill"]) / position["fill"] * 100.0
            if position["direction"] == "long"
            else (position["fill"] - exit_price) / position["fill"] * 100.0
        )
        trades.append({**position, "exit_time": "day_end", "exit": exit_price, "ret_pct": ret_pct, "reason": "day_end_forced"})
    return trades


def _run(windows_subset: dict, **kwargs) -> dict:
    all_trades = []
    per_day: dict[str, float] = {}
    for _wname, (all_by_stock, all_days) in windows_subset.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            trades = simulate_day_min_hold(day_data, **kwargs)
            all_trades.extend(trades)
            per_day[d] = per_day.get(d, 0.0) + sum(t["ret_pct"] for t in trades)
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    day_rets = np.array(list(per_day.values())) if per_day else np.array([])
    if len(rets) == 0 or len(day_rets) == 0 or day_rets.std() == 0:
        return {"n": len(rets), "risk_adj": float("-inf"), "breakeven_bps": 0.0, "win_rate": 0.0}
    breakeven = (rets.sum() / len(rets)) * 100
    win = float(np.mean(rets > 0) * 100)
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    return {"n": len(rets), "risk_adj": day_mean / day_std, "breakeven_bps": breakeven, "win_rate": win}


def main() -> None:
    print("載入4窗口資料...")
    all_windows = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    wnames = list(all_windows.keys())

    print(f"sweep grid: min_hold_sec={MIN_HOLD_GRID}")
    print("=" * 100)

    print("\n### 對照：baseline(min_hold_sec=0，等於現行規格) 全4窗口 ###")
    m0 = _run(all_windows, **BASE, min_hold_sec=0.0)
    print(f"  n={m0['n']} 勝率={m0['win_rate']:.1f}% 損平={m0['breakeven_bps']:.1f}bps risk-adj={m0['risk_adj']:+.3f}")

    fold_results = []
    for holdout_name in wnames:
        train_windows = {k: v for k, v in all_windows.items() if k != holdout_name}
        holdout_windows = {holdout_name: all_windows[holdout_name]}

        print(f"\n### Fold: holdout={holdout_name} · train={list(train_windows.keys())} ###")
        best = None
        for mh in MIN_HOLD_GRID:
            m = _run(train_windows, **BASE, min_hold_sec=mh)
            if best is None or m["risk_adj"] > best[1]["risk_adj"]:
                best = (mh, m)
        mh_best, train_m = best
        print(f"  train最佳點: min_hold_sec={mh_best}s (train risk-adj={train_m['risk_adj']:.3f} "
              f"損平={train_m['breakeven_bps']:.1f}bps 勝率={train_m['win_rate']:.1f}%)")

        holdout_new = _run(holdout_windows, **BASE, min_hold_sec=mh_best)
        holdout_base = _run(holdout_windows, **BASE, min_hold_sec=0.0)
        print(f"  >>> HOLDOUT({holdout_name}) min_hold={mh_best}s: n={holdout_new['n']:4d} "
              f"勝率={holdout_new['win_rate']:5.1f}% 損平={holdout_new['breakeven_bps']:6.1f}bps risk-adj={holdout_new['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) baseline    : n={holdout_base['n']:4d} "
              f"勝率={holdout_base['win_rate']:5.1f}% 損平={holdout_base['breakeven_bps']:6.1f}bps risk-adj={holdout_base['risk_adj']:+.3f}")
        fold_results.append({"holdout": holdout_name, "mh": mh_best, "new": holdout_new, "base": holdout_base})

    print("\n" + "=" * 100)
    print("=== 4折總結 ===")
    n_wins = 0
    for r in fold_results:
        beats = r["new"]["risk_adj"] > r["base"]["risk_adj"]
        n_wins += int(beats)
        print(f"  {r['holdout']:12s} (min_hold={r['mh']}s): new risk-adj={r['new']['risk_adj']:+.3f} 損平={r['new']['breakeven_bps']:5.1f}bps  "
              f"vs baseline risk-adj={r['base']['risk_adj']:+.3f} 損平={r['base']['breakeven_bps']:5.1f}bps  {'贏' if beats else '輸'}")
    print(f"\n  4折中有{n_wins}折min_hold版本在holdout上risk-adj優於baseline")


if __name__ == "__main__":
    main()
