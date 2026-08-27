"""2026-08-13：全新訊號框架——逐筆 order flow imbalance（訂單流不平衡）。

跟今天已經測過的「開盤突破+量能確認」訊號完全不同來源：不看價格離開盤價多遠，
只看最近一段時間的「主動買方 vs 主動賣方成交量」誰佔上風，賭的是短線 order
flow 一面倒時價格會延續（microstructure momentum），不是價格突破某個絕對關卡。

歷史tick CSV（reports/research/expert_pool_futures_tick/*.csv）只有 price/volume，
沒有 bid/ask（已檢查過欄位：date,futures_id,contract_date,price,volume）。改用
標準 tick rule 當 order flow proxy：
  - 每筆成交價 > 前一筆 → 判定為主動買盤（+volume）；
  - 每筆成交價 < 前一筆 → 判定為主動賣盤（−volume）；
  - 平盤（價格未變）→ 沿用上一筆的方向（tick rule 標準做法，這是文獻上對缺
    bid/ask資料最常見的signed-volume proxy，見Lee & Ready 1991 tick test）。
  - imbalance = 最近 window_ticks 筆的 signed volume 總和 / 總量，範圍[-1,1]，
    +1代表窗內全是主動買盤、-1代表全是主動賣盤。

跟開盤突破訊號共用同一套「12檔單槽位輪動＋動態搶佔」部位管理框架（今天稍早
使用者明確要求「搶佔還是要的」，這裡沿用同一套12檔框架所以保留），差別只在
「進場觸發條件」換成 order flow imbalance 達門檻，不再是價格突破開盤±X%。
出場沿用同一套規則：trailing stop、exit_price 用觸發當下真實tick價
（float(p)，不是理論停損價——今天已修好的同一個bug教訓，這裡從一開始就用對）。

Baseline：直接呼叫 momentum_breakout_strategy.simulate_portfolio_day 這支已修好
fill-price bug的SSOT，用現行live規格（breakout_pct=0.5, trail_pct=1.0,
vol_confirm_mult=1.5, rearm_pct=0.25, min_overshoot_pct=0.15, min_vol_ratio=1.5,
preempt_mult=2.0）當對照，不重新實作一份自己的baseline版本。

驗證方法：4折留一窗口交叉驗證（leave-one-window-out）。每折用其餘3個窗口對
order-flow參數grid做sweep，依risk-adj(日均報酬/日std)挑最佳點，只套用到留下的
第4個窗口驗證一次；4折輪流，最後報「4折中幾折order-flow版本risk-adj優於
baseline」。

用法：
  PYTHONPATH=src .venv/bin/python -u \
      scripts/research/momentum_rotation_order_flow_imbalance_holdout_cv.py
"""

from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import simulate_portfolio_day  # noqa: E402
from momentum_rotation_redesign_search import WINDOWS, load_window  # noqa: E402

BASELINE_KWARGS = dict(
    breakout_pct=0.5,
    trail_pct=1.0,
    vol_confirm_mult=1.5,
    rearm_pct=0.25,
    min_overshoot_pct=0.15,
    min_vol_ratio=1.5,
    preempt_mult=2.0,
)

# order-flow-imbalance 訊號參數 grid（preempt_mult / rearm_imbalance 固定沿用
# baseline的搶佔倍數與一個中性再武裝門檻，避免grid爆炸——這兩個不是這次研究
# 的核心假說，核心假說是「imbalance能不能取代breakout+vol_confirm當觸發條件」）。
WINDOW_TICKS_GRID = [15, 30, 60, 90]
IMBALANCE_THRESH_GRID = [0.5, 0.6, 0.7, 0.85]
MIN_VOL_MULT_GRID = [0.8, 1.2, 1.8]
TRAIL_PCT_GRID = [0.5, 1.0, 1.5]
REARM_IMBALANCE = 0.3
PREEMPT_MULT = 2.0


def precompute_day_ofi(
    stock_day_data: dict[str, tuple[list[str], np.ndarray, np.ndarray]],
) -> dict[str, dict]:
    """單日、跨標的：tick rule 方向分類 + signed-volume累積和，一次算好。

    這些量（directions／cum_signed／cum_vol）不依賴 window_ticks 等grid參數，
    grid sweep時每個組合重算一次是純浪費——4窗口總共只有~48萬筆tick，但grid
    組合數x折數會把同一批tick的tick-rule分類重複算上百次，所以獨立拆出來，
    在sweep最外層只算一次、所有(window_ticks,...)組合共用。
    """
    meta: dict[str, dict] = {}
    for sid, (times, prices, volumes) in stock_day_data.items():
        n = prices.size
        if n < 3:
            continue
        directions = np.zeros(n)
        last_dir = 0.0
        for i in range(1, n):
            if prices[i] > prices[i - 1]:
                last_dir = 1.0
            elif prices[i] < prices[i - 1]:
                last_dir = -1.0
            directions[i] = last_dir
        signed_vol = directions * volumes
        cum_signed = np.concatenate(([0.0], np.cumsum(signed_vol)))
        cum_vol = np.concatenate(([0.0], np.cumsum(volumes)))
        meta[sid] = {
            "times": times, "prices": prices, "volumes": volumes,
            "cum_signed": cum_signed, "cum_vol": cum_vol,
        }
    return meta


def simulate_day_ofi(
    precomputed_day_data: dict[str, dict],
    *,
    window_ticks: int,
    imbalance_thresh: float,
    min_vol_mult: float,
    trail_pct: float,
    rearm_imbalance: float = REARM_IMBALANCE,
    preempt_mult: float = PREEMPT_MULT,
) -> list[dict]:
    """單槽位輪動（含動態搶佔），觸發條件換成 order flow imbalance。

    結構完全比照 momentum_breakout_strategy.simulate_portfolio_day（同一套持倉
    管理／搶佔／出場邏輯，出場exit_price一律用觸發當下真實tick價），差別只在
    候選訊號的產生方式：不看價格是否突破開盤±X%，看最近window_ticks筆tick用
    tick rule算出來的signed volume imbalance是否一面倒。

    ``precomputed_day_data``：precompute_day_ofi() 的輸出（單日、跨標的）。
    """
    merged: list[tuple[str, str, int]] = []
    meta: dict[str, dict] = {}
    for sid, st in precomputed_day_data.items():
        n = st["prices"].size
        if n < window_ticks + 2:
            continue
        meta[sid] = st
        for k in range(window_ticks, n):
            merged.append((st["times"][k], sid, k))
    merged.sort(key=lambda x: x[0])

    armed: dict[str, bool] = {sid: True for sid in meta}
    last_price: dict[str, float] = {sid: float(meta[sid]["prices"][0]) for sid in meta}
    trades: list[dict] = []
    position: dict | None = None

    for t, sid, k in merged:
        st = meta[sid]
        p = float(st["prices"][k])
        v = float(st["volumes"][k])
        last_price[sid] = p

        window_signed = st["cum_signed"][k + 1] - st["cum_signed"][k + 1 - window_ticks]
        window_vol = st["cum_vol"][k + 1] - st["cum_vol"][k + 1 - window_ticks]
        imbalance = window_signed / window_vol if window_vol > 0 else 0.0
        day_avg_tick_vol = st["cum_vol"][k + 1] / (k + 1)
        window_avg_tick_vol = window_vol / window_ticks
        vol_ratio = window_avg_tick_vol / day_avg_tick_vol if day_avg_tick_vol > 0 else 0.0

        is_held = position is not None and position["sid"] == sid
        if is_held:
            if position["direction"] == "long":
                position["peak_trough"] = max(position["peak_trough"], p)
                stop = position["peak_trough"] * (1 - trail_pct / 100.0)
                hit = p <= stop
            else:
                position["peak_trough"] = min(position["peak_trough"], p)
                stop = position["peak_trough"] * (1 + trail_pct / 100.0)
                hit = p >= stop
            if hit:
                exit_price = float(p)  # 真實tick價，不是理論stop（今天已修好的教訓）
                ret_pct = (
                    (exit_price - position["fill"]) / position["fill"] * 100.0
                    if position["direction"] == "long"
                    else (position["fill"] - exit_price) / position["fill"] * 100.0
                )
                trades.append({**position, "exit_time": t, "exit": exit_price, "ret_pct": ret_pct, "reason": "trail_stop"})
                position = None
                armed[sid] = False
            continue

        if not armed[sid]:
            if abs(imbalance) < rearm_imbalance:
                armed[sid] = True
            continue

        if abs(imbalance) < imbalance_thresh or vol_ratio < min_vol_mult:
            continue
        direction = "long" if imbalance > 0 else "short"
        score = abs(imbalance) * vol_ratio
        fill = p  # 訊號當下真實tick價，不是任何理論門檻價
        candidate = {
            "sid": sid, "direction": direction, "fill": fill, "entry": fill,
            "entry_time": t, "entry_score": score, "peak_trough": fill,
            "imbalance": imbalance, "vol_ratio": vol_ratio,
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


def _run_from_days(day_data_iter, sim_fn, **kwargs) -> dict:
    """``day_data_iter``: iterable of (day_key, day_data) — day_data 的型別依
    sim_fn而定（baseline吃raw (times,prices,volumes)，order-flow吃precompute好
    的meta dict）。共用同一套彙總/風控指標計算，跟baseline那份_run同一套定義。
    """
    all_trades: list[dict] = []
    per_day: dict[str, float] = {}
    for day_key, day_data in day_data_iter:
        if len(day_data) < 3:
            continue
        trades = sim_fn(day_data, **kwargs)
        all_trades.extend(trades)
        per_day[day_key] = per_day.get(day_key, 0.0) + sum(t["ret_pct"] for t in trades)
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    day_rets = np.array(list(per_day.values())) if per_day else np.array([])
    if len(rets) < 20 or len(day_rets) == 0 or day_rets.std() == 0:
        return {"n": len(rets), "risk_adj": float("-inf"), "breakeven_bps": 0.0, "win_rate": 0.0}
    breakeven = (rets.sum() / len(rets)) * 100
    win = float(np.mean(rets > 0) * 100)
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    return {"n": len(rets), "risk_adj": day_mean / day_std, "breakeven_bps": breakeven, "win_rate": win}


def _run_baseline(windows_subset: dict) -> dict:
    def gen():
        for wname, (all_by_stock, all_days) in windows_subset.items():
            for d in all_days:
                day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
                yield f"{wname}:{d}", day_data
    return _run_from_days(gen(), simulate_portfolio_day, **BASELINE_KWARGS)


def _run_ofi(precomputed_windows_subset: dict, **kwargs) -> dict:
    def gen():
        for wname, day_map in precomputed_windows_subset.items():
            for d, day_data in day_map.items():
                yield f"{wname}:{d}", day_data
    return _run_from_days(gen(), simulate_day_ofi, **kwargs)


def main() -> None:
    print("載入4窗口資料...")
    all_windows = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    wnames = list(all_windows.keys())

    print("預先算好tick-rule方向分類 + signed-volume累積和（跟grid參數無關，只算一次）...")
    precomputed_windows: dict[str, dict[str, dict]] = {}
    for wname, (all_by_stock, all_days) in all_windows.items():
        day_map: dict[str, dict] = {}
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            day_map[d] = precompute_day_ofi(day_data)
        precomputed_windows[wname] = day_map

    grid = [
        (wt, it, mv, tp)
        for wt in WINDOW_TICKS_GRID
        for it in IMBALANCE_THRESH_GRID
        for mv in MIN_VOL_MULT_GRID
        for tp in TRAIL_PCT_GRID
    ]
    print(f"sweep grid size = {len(grid)} (window_ticks x imbalance_thresh x min_vol_mult x trail_pct)")
    print(f"固定: rearm_imbalance={REARM_IMBALANCE}, preempt_mult={PREEMPT_MULT}")
    print("=" * 100)

    print("\n### 對照：baseline（開盤突破+量能確認，現行live規格）全4窗口 ###")
    m0 = _run_baseline(all_windows)
    print(f"  n={m0['n']} 勝率={m0['win_rate']:.1f}% 損平={m0['breakeven_bps']:.1f}bps risk-adj={m0['risk_adj']:+.3f}")

    fold_results = []
    for holdout_name in wnames:
        train_pre = {k: v for k, v in precomputed_windows.items() if k != holdout_name}
        holdout_pre = {holdout_name: precomputed_windows[holdout_name]}
        holdout_windows = {holdout_name: all_windows[holdout_name]}
        print(f"\n### Fold: holdout={holdout_name} · train={[k for k in wnames if k != holdout_name]} ###")

        best = None
        for wt, it, mv, tp in grid:
            m = _run_ofi(
                train_pre,
                window_ticks=wt, imbalance_thresh=it, min_vol_mult=mv, trail_pct=tp,
            )
            if best is None or m["risk_adj"] > best[1]["risk_adj"]:
                best = ((wt, it, mv, tp), m)
        (wt_b, it_b, mv_b, tp_b), train_m = best
        print(f"  train最佳點: window_ticks={wt_b} imbalance_thresh={it_b} min_vol_mult={mv_b} trail_pct={tp_b} "
              f"(train risk-adj={train_m['risk_adj']:.3f} 損平={train_m['breakeven_bps']:.1f}bps "
              f"勝率={train_m['win_rate']:.1f}% n={train_m['n']})")

        holdout_new = _run_ofi(
            holdout_pre,
            window_ticks=wt_b, imbalance_thresh=it_b, min_vol_mult=mv_b, trail_pct=tp_b,
        )
        holdout_base = _run_baseline(holdout_windows)
        print(f"  >>> HOLDOUT({holdout_name}) order_flow: n={holdout_new['n']:4d} "
              f"勝率={holdout_new['win_rate']:5.1f}% 損平={holdout_new['breakeven_bps']:6.1f}bps risk-adj={holdout_new['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) baseline   : n={holdout_base['n']:4d} "
              f"勝率={holdout_base['win_rate']:5.1f}% 損平={holdout_base['breakeven_bps']:6.1f}bps risk-adj={holdout_base['risk_adj']:+.3f}")
        fold_results.append({
            "holdout": holdout_name, "params": (wt_b, it_b, mv_b, tp_b),
            "new": holdout_new, "base": holdout_base,
        })

    print("\n" + "=" * 100)
    print("=== 4折總結 ===")
    n_wins = 0
    for r in fold_results:
        beats = r["new"]["risk_adj"] > r["base"]["risk_adj"]
        n_wins += int(beats)
        wt_b, it_b, mv_b, tp_b = r["params"]
        print(f"  {r['holdout']:12s} (wt={wt_b},thr={it_b},minvol={mv_b},trail={tp_b}): "
              f"new risk-adj={r['new']['risk_adj']:+.3f} 損平={r['new']['breakeven_bps']:6.1f}bps  "
              f"vs baseline risk-adj={r['base']['risk_adj']:+.3f} 損平={r['base']['breakeven_bps']:6.1f}bps  "
              f"{'贏' if beats else '輸'}")
    print(f"\n  4折中有{n_wins}折order-flow-imbalance版本在holdout上risk-adj優於baseline")


if __name__ == "__main__":
    main()
