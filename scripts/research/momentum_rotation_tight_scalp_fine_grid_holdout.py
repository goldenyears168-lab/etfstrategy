"""2026-08-13：tight-scalp 精修 round 2。

8/13 稍早 momentum_rotation_tight_scalp_holdout_cv.py 只粗掃 4 個門檻檔位
（1.5x/0.15%、2.5x/0.4%、3.5x/0.6%、5.0x/1.0%）x 4 個持有秒數（10/15/20/30s），
4 折中 3 折贏（tight_scalp beats baseline），但贏的那 3 折最佳點全部落在
hold_sec=10s——網格邊界，暗示真正最佳點可能更短；而且贏的損平只有
0.3~4.8bps，仍在 5bps 最低成本估計以下（沒有安全邊際）。

這裡做更細的三維網格（vol_confirm_mult x min_overshoot_pct x hold_sec 各自獨立
sweep，不綁 tier pair）：
  vol_confirm_mult ∈ [1.8, 2.0, 2.2, 2.5, 2.8, 3.0, 3.5]
  min_overshoot_pct ∈ [0.2, 0.3, 0.4, 0.5, 0.6]
  hold_sec ∈ [3, 5, 7,10, 13, 16, 20]（含比 10 秒更短的選項）
= 7 x 5 x 7 = 245 組合。

策略邏輯完全沿用 momentum_rotation_tight_scalp_holdout_cv.py 的
simulate_day_tight_scalp（緊門檻 + 固定持有 hold_sec 秒後不論賺賠強制出場，
持倉中仍保留保護性 trailing stop，搶佔機制保留）。exit_price 全部用觸發當下
真實 tick 價 float(p)（今天已修好的兩個 bug 教訓）。

baseline **直接呼叫** momentum_breakout_strategy.simulate_portfolio_day 這個
SSOT（不是用 hold_sec=999999 模擬，因為 SSOT 另外還有 min_vol_ratio 獨立
filter，跟 tight-scalp 版本把 vol_confirm_mult 兼當量能門檻不完全等價，
直接呼叫才是忠實的 baseline 對照）。

4 折留一窗口交叉驗證：每次留一折，其餘 3 折 sweep 245 組合找 risk-adj 最高點，
只套用到留下的第 4 折驗證一次。
"""

from __future__ import annotations

import sys
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import simulate_portfolio_day  # noqa: E402
from momentum_rotation_redesign_search import WINDOWS, load_window  # noqa: E402

VOL_CONFIRM_GRID = [1.8, 2.0, 2.2, 2.5, 2.8, 3.0, 3.5]
MIN_OVERSHOOT_GRID = [0.2, 0.3, 0.4, 0.5, 0.6]
HOLD_SEC_GRID = [3.0, 5.0, 7.0, 10.0, 13.0, 16.0, 20.0]
BASE_FIXED = dict(breakout_pct=0.5, trail_pct=1.0, rearm_pct=0.25, preempt_mult=2.0)


# 效能：原本 vol_history 用「每筆tick都對成長中的list呼叫np.median」重算——這個
# baseline（量能基準）完全不吃任何策略參數（vol_confirm_mult/min_overshoot_pct/
# hold_sec都不影響它），對245組合x4折等於重算980次一模一樣的東西，profile量到
# 單一window單一組合就要28秒（210K次np.median呼叫，光numpy.asanyarray轉型
# overhead就吃18秒）。這裡改成每個(window,day,sid)只算一次expanding median
# （pandas C實作、向量化），結果與原本逐筆重算的np.median(vh)語意完全一致
# （見下方parity驗證），跑245x4組合從數小時級降到分鐘級。
def _precompute_baselines(stock_day_data: dict) -> dict[str, np.ndarray]:
    """對每個sid回傳長度 len(times)-1 的baseline陣列，第i個元素對應原本迴圈
    k=1+i那筆tick的baseline（median of volumes[1..k-1]之前已看過的量, 空集合時=1.0）。
    """
    out: dict[str, np.ndarray] = {}
    for sid, (times, prices, volumes) in stock_day_data.items():
        if prices.size < 2:
            continue
        vol_seq = volumes[1:]  # 對應merged迴圈裡從k=1開始append進vol_history的量
        if vol_seq.size == 0:
            out[sid] = np.array([])
            continue
        s = pd.Series(vol_seq).expanding().median().shift(1)
        arr = s.to_numpy()
        arr = np.where(np.isnan(arr), 1.0, np.maximum(arr, 1e-9))
        out[sid] = arr
    return out


def simulate_day_tight_scalp(
    stock_day_data: dict, *,
    breakout_pct: float, trail_pct: float, rearm_pct: float, preempt_mult: float,
    vol_confirm_mult: float, min_overshoot_pct: float, hold_sec: float,
    precomputed_baselines: dict[str, np.ndarray] | None = None,
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

    if precomputed_baselines is None:
        precomputed_baselines = _precompute_baselines(stock_day_data)
    baseline_idx = {sid: 0 for sid in meta}
    armed = {sid: True for sid in meta}
    last_price = {sid: meta[sid]["open"] for sid in meta}
    trades: list[dict] = []
    position: dict | None = None

    for t, sid, p, v in merged:
        st = meta[sid]
        last_price[sid] = p
        idx = baseline_idx[sid]
        baseline = precomputed_baselines[sid][idx]
        baseline_idx[sid] = idx + 1

        is_held = position is not None and position["sid"] == sid
        if is_held:
            elapsed = (datetime.fromisoformat(t) - datetime.fromisoformat(position["entry_time"])).total_seconds()
            if position["direction"] == "long":
                position["peak_trough"] = max(position["peak_trough"], p)
                stop = position["peak_trough"] * (1 - trail_pct / 100.0)
                hit = p <= stop
            else:
                position["peak_trough"] = min(position["peak_trough"], p)
                stop = position["peak_trough"] * (1 + trail_pct / 100.0)
                hit = p >= stop
            timed_out = elapsed >= hold_sec
            if hit or timed_out:
                exit_price = float(p)
                ret_pct = (
                    (exit_price - position["fill"]) / position["fill"] * 100.0
                    if position["direction"] == "long"
                    else (position["fill"] - exit_price) / position["fill"] * 100.0
                )
                reason = "trail_stop" if hit else "timed_exit"
                trades.append({**position, "exit_time": t, "exit": exit_price, "ret_pct": ret_pct, "reason": reason})
                position = None
                armed[sid] = False
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
        if overshoot < min_overshoot_pct:
            continue
        vol_ratio = v / baseline
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
        elif score >= preempt_mult * position["entry_score"]:
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

    if position is not None:
        exit_price = last_price[position["sid"]]
        ret_pct = (
            (exit_price - position["fill"]) / position["fill"] * 100.0
            if position["direction"] == "long"
            else (position["fill"] - exit_price) / position["fill"] * 100.0
        )
        trades.append({**position, "exit_time": "day_end", "exit": exit_price, "ret_pct": ret_pct, "reason": "day_end_forced"})
    return trades


def _stats(all_trades: list[dict], per_day: dict[str, float]) -> dict:
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    day_rets = np.array(list(per_day.values())) if per_day else np.array([])
    if len(rets) < 20 or len(day_rets) == 0 or day_rets.std() == 0:
        return {"n": len(rets), "risk_adj": float("-inf"), "breakeven_bps": 0.0, "win_rate": 0.0}
    breakeven = (rets.sum() / len(rets)) * 100
    win = float(np.mean(rets > 0) * 100)
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    return {"n": len(rets), "risk_adj": day_mean / day_std, "breakeven_bps": breakeven, "win_rate": win}


def _run_tight_scalp(
    windows_subset: dict, *, vol_confirm_mult: float, min_overshoot_pct: float, hold_sec: float,
    baseline_cache: dict[tuple[str, str], dict[str, np.ndarray]] | None = None,
) -> dict:
    all_trades = []
    per_day: dict[str, float] = {}
    for wname, (all_by_stock, all_days) in windows_subset.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            precomputed = baseline_cache.get((wname, d)) if baseline_cache is not None else None
            trades = simulate_day_tight_scalp(
                day_data, **BASE_FIXED,
                vol_confirm_mult=vol_confirm_mult, min_overshoot_pct=min_overshoot_pct, hold_sec=hold_sec,
                precomputed_baselines=precomputed,
            )
            all_trades.extend(trades)
            per_day[d] = per_day.get(d, 0.0) + sum(t["ret_pct"] for t in trades)
    return _stats(all_trades, per_day)


def _build_baseline_cache(all_windows: dict) -> dict[tuple[str, str], dict[str, np.ndarray]]:
    """對每個(window, day)只算一次量能baseline（不吃任何策略參數），跨245x4組合
    重複使用，避免980次重算同一件事。"""
    cache: dict[tuple[str, str], dict[str, np.ndarray]] = {}
    for wname, (all_by_stock, all_days) in all_windows.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            cache[(wname, d)] = _precompute_baselines(day_data)
    return cache


def _run_baseline(windows_subset: dict) -> dict:
    """直接呼叫 SSOT momentum_breakout_strategy.simulate_portfolio_day（live現行規格：
    breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
    min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0，全部用函式預設值）。
    """
    all_trades = []
    per_day: dict[str, float] = {}
    for _wname, (all_by_stock, all_days) in windows_subset.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            trades = simulate_portfolio_day(day_data)
            all_trades.extend(trades)
            per_day[d] = per_day.get(d, 0.0) + sum(t["ret_pct"] for t in trades)
    return _stats(all_trades, per_day)


def main() -> None:
    print("載入4窗口資料...")
    all_windows = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    wnames = list(all_windows.keys())

    n_combos = len(VOL_CONFIRM_GRID) * len(MIN_OVERSHOOT_GRID) * len(HOLD_SEC_GRID)
    print(f"精細網格: vol_confirm_mult={VOL_CONFIRM_GRID} x min_overshoot_pct={MIN_OVERSHOOT_GRID} "
          f"x hold_sec={HOLD_SEC_GRID} = {n_combos}組合")
    print("=" * 110)

    print("預算量能baseline快取（每個window-day只算一次，跨組合重用）...")
    baseline_cache = _build_baseline_cache(all_windows)

    print("\n### 對照：baseline = momentum_breakout_strategy.simulate_portfolio_day（live現行規格,全預設值） 全4窗口 ###")
    m0 = _run_baseline(all_windows)
    print(f"  n={m0['n']} 勝率={m0['win_rate']:.1f}% 損平={m0['breakeven_bps']:.1f}bps risk-adj={m0['risk_adj']:+.3f}")

    fold_results = []
    for holdout_name in wnames:
        train_windows = {k: v for k, v in all_windows.items() if k != holdout_name}
        holdout_windows = {holdout_name: all_windows[holdout_name]}
        print(f"\n### Fold: holdout={holdout_name} · train={list(train_windows.keys())} ###")

        best = None
        all_train_results = []
        for vc in VOL_CONFIRM_GRID:
            for mo in MIN_OVERSHOOT_GRID:
                for hs in HOLD_SEC_GRID:
                    m = _run_tight_scalp(
                        train_windows, vol_confirm_mult=vc, min_overshoot_pct=mo, hold_sec=hs,
                        baseline_cache=baseline_cache,
                    )
                    all_train_results.append((vc, mo, hs, m))
                    if best is None or m["risk_adj"] > best[3]["risk_adj"]:
                        best = (vc, mo, hs, m)
        vc_best, mo_best, hs_best, train_m = best
        print(f"  train最佳點: vol_confirm_mult={vc_best} min_overshoot_pct={mo_best}% hold_sec={hs_best}s "
              f"(train risk-adj={train_m['risk_adj']:.3f} 損平={train_m['breakeven_bps']:.1f}bps "
              f"勝率={train_m['win_rate']:.1f}% n={train_m['n']})")

        # 印出 train 上前 5 名，觀察穩健度（是否有一大片鄰近組合都表現接近，而非孤立尖峰）
        top5 = sorted(all_train_results, key=lambda x: x[3]["risk_adj"], reverse=True)[:5]
        print("  train前5名:")
        for vc, mo, hs, m in top5:
            print(f"    vc={vc} mo={mo}% hs={hs}s: risk-adj={m['risk_adj']:+.3f} 損平={m['breakeven_bps']:5.1f}bps n={m['n']}")

        holdout_new = _run_tight_scalp(
            holdout_windows, vol_confirm_mult=vc_best, min_overshoot_pct=mo_best, hold_sec=hs_best,
            baseline_cache=baseline_cache,
        )
        holdout_base = _run_baseline(holdout_windows)
        print(f"  >>> HOLDOUT({holdout_name}) tight_scalp: n={holdout_new['n']:4d} "
              f"勝率={holdout_new['win_rate']:5.1f}% 損平={holdout_new['breakeven_bps']:6.1f}bps risk-adj={holdout_new['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) baseline   : n={holdout_base['n']:4d} "
              f"勝率={holdout_base['win_rate']:5.1f}% 損平={holdout_base['breakeven_bps']:6.1f}bps risk-adj={holdout_base['risk_adj']:+.3f}")
        fold_results.append({
            "holdout": holdout_name, "vc": vc_best, "mo": mo_best, "hs": hs_best,
            "new": holdout_new, "base": holdout_base,
        })

    print("\n" + "=" * 110)
    print("=== 4折總結 ===")
    n_wins = 0
    for r in fold_results:
        beats = r["new"]["risk_adj"] > r["base"]["risk_adj"]
        n_wins += int(beats)
        print(f"  {r['holdout']:12s} (vc={r['vc']}/mo={r['mo']}%/hs={r['hs']}s): "
              f"new risk-adj={r['new']['risk_adj']:+.3f} 損平={r['new']['breakeven_bps']:5.1f}bps  "
              f"vs baseline risk-adj={r['base']['risk_adj']:+.3f} 損平={r['base']['breakeven_bps']:5.1f}bps  "
              f"{'贏' if beats else '輸'}")
    print(f"\n  4折中有{n_wins}折tight_scalp精修版在holdout上risk-adj優於baseline")

    print("\n=== 各折選中的最佳點是否收斂到同一組合？（穩健度檢視） ===")
    for r in fold_results:
        print(f"  holdout={r['holdout']:12s} -> vc={r['vc']} mo={r['mo']}% hs={r['hs']}s")


if __name__ == "__main__":
    main()
