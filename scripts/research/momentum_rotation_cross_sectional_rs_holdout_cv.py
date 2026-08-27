"""2026-08-13：全新訊號框架 — 橫斷面相對強弱輪動（cross-sectional relative
strength rotation）。

背景：既有的「個股開盤突破+量能確認」動能延續框架（momentum_breakout_strategy.
simulate_portfolio_day，已修好entry/exit fill-price bug後的SSOT）在4個獨立
窗口上勝率35.7%~43.7%、損平僅9.8bps，低於5bps最低成本估計的邊際很薄。而且
今天稍早的盲點分析發現：84.2%的候選訊號60秒內會有另一檔同時發訊號——代表這
12檔高度共同運動，個股「絕對價格突破開盤價±X%」訊號裡混雜了大量大盤beta，
不是各自獨立的idiosyncratic動能。

這支腳本改用完全不同的訊號來源：不看個股絕對突破開盤價，改成每個時間點計算
12檔各自「今天至今報酬率」（相對開盤價）的橫斷面排名，做多目前最強的一檔
（領先者），用它跟「大盤」（12檔的橫斷面中位數，approximates共同因子）的
領先幅度(spread)當進場門檻——這個設計直接把共同因子（大盤同步走勢）當成
baseline扣掉：如果全部12檔一起噴（beta驅動），領先者跟中位數的spread不會
變大；只有真的有個股相對其他11檔走出獨立強度時spread才會擴大，這正是要抓
的idiosyncratic訊號。

規則設計（單槽位，保留搶佔機制——今天使用者明確要求延續現有12檔輪動框架時
要保留搶佔）：
  1. 排名：每一筆tick更新該檔「今天至今報酬率」ret_pct[sid]=(p-open)/open*100。
     leader = ret_pct最高者，median_ret = 12檔ret_pct中位數，
     spread_long = leader_ret - median_ret（領先幅度，扣掉共同因子後的相對強度）。
     long_short模式另外算 laggard = ret_pct最低者，
     spread_short = median_ret - laggard_ret，兩邊哪個spread大就用哪個方向。
  2. 進場：空手時，若這一筆tick讓某檔變成leader（或long_short模式下的laggard）
     且spread ≥ min_spread_pct，直接用當下tick真實價p進場（沿用已修好的
     fill=實際tick價，不是理論門檻價）。
  3. 出場——两條線都比，先到先觸發：
     a. own-price trailing stop（跟舊框架一樣，追高/低點回檔trail_pct%）；
     b. rank-loss exit（這個框架的新機制）：持倉方向的相對優勢被侵蝕超過
        rank_exit_margin個百分點就出場——多單是「leader_ret - 持倉股ret_pct」
        > margin（已經被別人超車一大截），不必等自己價格拉回；margin=None代表
        關掉這條線、只靠trailing stop（ablation對照組）。
     c. 收盤強制平倉。
  4. 搶佔：持倉中若冒出新的leader/laggard、其spread ≥ 目前持倉進場spread ×
     preempt_mult，提前用當下市價平倉、換到更強的候選（邏輯跟舊框架一致）。

初版（confirm_sec=0，一變leader就立刻進場）在探索階段全數負值：日均risk-adj
全負、損平-2~-7bps，交易明細顯示典型「買在瞬間尖峰、被1%移動停利打掉」——
這些單股期貨tick稀疏，瞬間變成leader常常只是單筆買賣價跳動（bid-ask bounce）
造成的雜訊，不是真的相對強度持續。加了confirm_sec（要求leadership連續維持
confirm_sec秒才進場，過濾單筆跳動雜訊）後探索性測試從-2.6bps損平改善到
confirm_sec=30時+0.19bps，方向對但幅度極小、幾乎打平，這是sweep grid裡納入
confirm_sec的原因（見下方grid）。

4折留一窗口交叉驗證：每折用其餘3窗口對(min_spread_pct, trail_pct,
rank_exit_margin, preempt_mult, mode)做網格搜尋、依risk-adj(日均報酬/日std)選
最佳點，只套用到留下的第4個窗口驗證一次；baseline＝直接呼叫
momentum_breakout_strategy.simulate_portfolio_day在同一份holdout資料上跑
現行live規格（breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5,
rearm_pct=0.25, min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0）。

用法：
  PYTHONPATH=src .venv/bin/python -u \
    scripts/research/momentum_rotation_cross_sectional_rs_holdout_cv.py
"""

from __future__ import annotations

import sys
import time
from datetime import datetime

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import simulate_portfolio_day  # noqa: E402
from momentum_rotation_redesign_search import WINDOWS, load_window  # noqa: E402

MIN_ACTIVE = 8  # 當天橫斷面至少要有這麼多檔有tick才納入排名計算（避免早盤稀疏期誤判leader）

# sweep grid（4窗口輪流當holdout，其餘3窗口train上sweep這個網格）。
# preempt_mult固定在既有框架驗證過的甜蜜點2.0（探索階段掃過1.5/2.0/3.0，
# 對這個新訊號來源影響很小，固定它以換取能放大confirm_sec/rank_exit_margin
# 這兩個真正影響大的軸的解析度，控制總組合數在可負擔範圍）。
MODE_GRID = ["long_only", "long_short"]
MIN_SPREAD_GRID = [0.2, 0.3, 0.5]
TRAIL_PCT_GRID = [0.5, 1.0]
RANK_EXIT_MARGIN_GRID = [None, 0.15]
CONFIRM_SEC_GRID = [0.0, 15.0, 30.0, 60.0]
PREEMPT_MULT_GRID = [2.0]

BASELINE_KWARGS = dict(
    breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5,
    rearm_pct=0.25, min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0,
)


def _seconds_between(start_iso: str | None, now_iso: str) -> float:
    if start_iso is None:
        return 0.0
    return (datetime.fromisoformat(now_iso) - datetime.fromisoformat(start_iso)).total_seconds()


def simulate_day_cross_sectional_rs(
    stock_day_data: dict,
    *,
    mode: str,
    min_spread_pct: float,
    trail_pct: float,
    rank_exit_margin: float | None,
    preempt_mult: float,
    confirm_sec: float = 0.0,
    min_active: int = MIN_ACTIVE,
) -> list[dict]:
    """單槽位橫斷面相對強弱輪動（含搶佔）。回傳這一天的成交紀錄列表。

    ``confirm_sec``：leadership（or laggard-ship）必須連續維持這麼多秒才視為
    合格候選——這些單股期貨tick稀疏，瞬間變成leader常常只是單筆bid-ask
    bounce雜訊，加這個確認窗過濾掉那種雜訊（見檔頭2026-08-13註記）。
    """
    merged: list[tuple[str, str, float, float]] = []
    open_price: dict[str, float] = {}
    for sid, (times, prices, volumes) in stock_day_data.items():
        if prices.size < 2:
            continue
        open_price[sid] = float(prices[0])
        for k in range(1, len(times)):
            merged.append((times[k], sid, float(prices[k]), float(volumes[k])))
    if len(open_price) < min_active:
        return []
    merged.sort(key=lambda x: x[0])

    last_price: dict[str, float] = dict(open_price)
    ret_pct: dict[str, float] = {sid: 0.0 for sid in open_price}
    trades: list[dict] = []
    position: dict | None = None  # {sid, direction, fill, entry_time, peak_trough, entry_spread}
    # 連續leadership/laggard-ship追蹤（confirm_sec用）
    cur_long_candidate: str | None = None
    cur_long_start: str | None = None
    cur_short_candidate: str | None = None
    cur_short_start: str | None = None

    for t, sid, p, v in merged:
        last_price[sid] = p
        ret_pct[sid] = (p - open_price[sid]) / open_price[sid] * 100.0

        rets_sorted = sorted(ret_pct.items(), key=lambda kv: kv[1])
        laggard_sid, laggard_ret = rets_sorted[0]
        leader_sid, leader_ret = rets_sorted[-1]
        mid = len(rets_sorted) // 2
        if len(rets_sorted) % 2 == 1:
            median_ret = rets_sorted[mid][1]
        else:
            median_ret = (rets_sorted[mid - 1][1] + rets_sorted[mid][1]) / 2.0
        spread_long = leader_ret - median_ret
        spread_short = median_ret - laggard_ret

        if leader_sid == cur_long_candidate and spread_long >= min_spread_pct:
            pass
        elif spread_long >= min_spread_pct:
            cur_long_candidate, cur_long_start = leader_sid, t
        else:
            cur_long_candidate, cur_long_start = None, None

        if laggard_sid == cur_short_candidate and spread_short >= min_spread_pct:
            pass
        elif spread_short >= min_spread_pct:
            cur_short_candidate, cur_short_start = laggard_sid, t
        else:
            cur_short_candidate, cur_short_start = None, None

        # --- 出場檢查（先於進場/搶佔，避免同一筆tick又出又進同一檔造成假象） ---
        if position is not None:
            held_sid = position["sid"]
            held_dir = position["direction"]
            if held_dir == "long":
                position["peak_trough"] = max(position["peak_trough"], last_price[held_sid])
                trail_stop = position["peak_trough"] * (1 - trail_pct / 100.0)
                trail_hit = last_price[held_sid] <= trail_stop
                rank_margin = leader_ret - ret_pct[held_sid]
            else:
                position["peak_trough"] = min(position["peak_trough"], last_price[held_sid])
                trail_stop = position["peak_trough"] * (1 + trail_pct / 100.0)
                trail_hit = last_price[held_sid] >= trail_stop
                rank_margin = ret_pct[held_sid] - laggard_ret
            rank_hit = rank_exit_margin is not None and rank_margin > rank_exit_margin
            if trail_hit or rank_hit:
                exit_price = float(last_price[held_sid])
                fill = position["fill"]
                ret = (exit_price - fill) / fill * 100.0 if held_dir == "long" else (fill - exit_price) / fill * 100.0
                reason = "trail_stop" if trail_hit else "rank_loss"
                trades.append({**position, "exit_time": t, "exit": exit_price, "ret_pct": ret, "reason": reason})
                position = None

        # --- 候選判定（這一筆tick觸發的是哪個方向的leadership） ---
        candidate = None
        long_ready = (
            cur_long_candidate is not None and sid == cur_long_candidate
            and (_seconds_between(cur_long_start, t) >= confirm_sec)
        )
        short_ready = (
            mode == "long_short" and cur_short_candidate is not None and sid == cur_short_candidate
            and (_seconds_between(cur_short_start, t) >= confirm_sec)
        )
        if long_ready and (mode == "long_only" or spread_long >= spread_short):
            candidate = {"sid": sid, "direction": "long", "score": spread_long}
        elif short_ready and spread_short > spread_long:
            candidate = {"sid": sid, "direction": "short", "score": spread_short}

        if candidate is None:
            continue

        if position is None:
            fill = float(p)
            position = {
                "sid": candidate["sid"], "direction": candidate["direction"], "fill": fill, "entry": fill,
                "entry_time": t, "entry_spread": candidate["score"], "peak_trough": fill,
            }
        elif candidate["sid"] != position["sid"] and candidate["score"] >= preempt_mult * position["entry_spread"]:
            held_sid = position["sid"]
            exit_price = float(last_price[held_sid])
            fill = position["fill"]
            held_dir = position["direction"]
            ret = (exit_price - fill) / fill * 100.0 if held_dir == "long" else (fill - exit_price) / fill * 100.0
            trades.append({**position, "exit_time": t, "exit": exit_price, "ret_pct": ret, "reason": "preempted"})
            new_fill = float(p)
            position = {
                "sid": candidate["sid"], "direction": candidate["direction"], "fill": new_fill, "entry": new_fill,
                "entry_time": t, "entry_spread": candidate["score"], "peak_trough": new_fill,
            }

    if position is not None:
        held_sid = position["sid"]
        exit_price = float(last_price[held_sid])
        fill = position["fill"]
        held_dir = position["direction"]
        ret = (exit_price - fill) / fill * 100.0 if held_dir == "long" else (fill - exit_price) / fill * 100.0
        trades.append({**position, "exit_time": "day_end", "exit": exit_price, "ret_pct": ret, "reason": "day_end_forced"})
    return trades


def _metrics(all_trades: list[dict], per_day: dict[str, float]) -> dict:
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    day_rets = np.array(list(per_day.values())) if per_day else np.array([])
    if len(rets) < 10 or len(day_rets) == 0 or day_rets.std() == 0:
        return {"n": len(rets), "risk_adj": float("-inf"), "breakeven_bps": 0.0, "win_rate": 0.0, "n_days": len(day_rets)}
    breakeven = (rets.sum() / len(rets)) * 100
    win = float(np.mean(rets > 0) * 100)
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    return {"n": len(rets), "risk_adj": day_mean / day_std, "breakeven_bps": breakeven, "win_rate": win, "n_days": len(day_rets)}


def _run_cross_sectional(windows_subset: dict, **kwargs) -> dict:
    all_trades: list[dict] = []
    per_day: dict[str, float] = {}
    for _wname, (all_by_stock, all_days) in windows_subset.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < MIN_ACTIVE:
                continue
            trades = simulate_day_cross_sectional_rs(day_data, **kwargs)
            all_trades.extend(trades)
            key = f"{_wname}|{d}"
            per_day[key] = per_day.get(key, 0.0) + sum(t["ret_pct"] for t in trades)
    return _metrics(all_trades, per_day)


def _run_baseline(windows_subset: dict) -> dict:
    all_trades: list[dict] = []
    per_day: dict[str, float] = {}
    for _wname, (all_by_stock, all_days) in windows_subset.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            trades = simulate_portfolio_day(day_data, **BASELINE_KWARGS)
            all_trades.extend(trades)
            key = f"{_wname}|{d}"
            per_day[key] = per_day.get(key, 0.0) + sum(t["ret_pct"] for t in trades)
    return _metrics(all_trades, per_day)


def main() -> None:
    print("載入4窗口資料...")
    all_windows = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    wnames = list(all_windows.keys())

    grid = [
        dict(mode=m, min_spread_pct=sp, trail_pct=tp, rank_exit_margin=rm, preempt_mult=pm, confirm_sec=cs)
        for m in MODE_GRID
        for sp in MIN_SPREAD_GRID
        for tp in TRAIL_PCT_GRID
        for rm in RANK_EXIT_MARGIN_GRID
        for pm in PREEMPT_MULT_GRID
        for cs in CONFIRM_SEC_GRID
    ]
    print(f"grid size = {len(grid)} combos x 4 folds (train=3窗口/次)")

    t0 = time.time()
    m_probe = _run_cross_sectional(all_windows, **grid[0])
    print(f"單一combo跑全4窗口耗時 {time.time()-t0:.1f}s -> n={m_probe['n']} risk_adj={m_probe['risk_adj']:.3f} "
          f"breakeven={m_probe['breakeven_bps']:.1f}bps win={m_probe['win_rate']:.1f}%")

    print("\n### 全4窗口 baseline(現行live規格) 對照 ###")
    base_all = _run_baseline(all_windows)
    print(f"  n={base_all['n']} n_days={base_all['n_days']} 勝率={base_all['win_rate']:.1f}% "
          f"損平={base_all['breakeven_bps']:.1f}bps risk-adj={base_all['risk_adj']:+.3f}")

    fold_results = []
    for holdout_name in wnames:
        train_windows = {k: v for k, v in all_windows.items() if k != holdout_name}
        holdout_windows = {holdout_name: all_windows[holdout_name]}
        print(f"\n### Fold: holdout={holdout_name} · train={list(train_windows.keys())} ###")

        t_fold = time.time()
        best = None
        for params in grid:
            m = _run_cross_sectional(train_windows, **params)
            if best is None or m["risk_adj"] > best[1]["risk_adj"]:
                best = (params, m)
        best_params, train_m = best
        print(f"  sweep耗時 {time.time()-t_fold:.1f}s · train最佳點: {best_params} "
              f"(train risk-adj={train_m['risk_adj']:.3f} 損平={train_m['breakeven_bps']:.1f}bps "
              f"勝率={train_m['win_rate']:.1f}% n={train_m['n']} n_days={train_m['n_days']})")

        holdout_new = _run_cross_sectional(holdout_windows, **best_params)
        holdout_base = _run_baseline(holdout_windows)
        print(f"  >>> HOLDOUT({holdout_name}) cross_sectional_rs: n={holdout_new['n']:4d} n_days={holdout_new['n_days']:3d} "
              f"勝率={holdout_new['win_rate']:5.1f}% 損平={holdout_new['breakeven_bps']:6.1f}bps "
              f"risk-adj={holdout_new['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) baseline           : n={holdout_base['n']:4d} n_days={holdout_base['n_days']:3d} "
              f"勝率={holdout_base['win_rate']:5.1f}% 損平={holdout_base['breakeven_bps']:6.1f}bps "
              f"risk-adj={holdout_base['risk_adj']:+.3f}")
        fold_results.append({
            "holdout": holdout_name, "params": best_params, "new": holdout_new, "base": holdout_base,
        })

    print("\n" + "=" * 100)
    print("=== 4折總結 ===")
    n_wins = 0
    for r in fold_results:
        beats = r["new"]["risk_adj"] > r["base"]["risk_adj"]
        n_wins += int(beats)
        print(f"  {r['holdout']:12s} {r['params']}")
        print(f"      new risk-adj={r['new']['risk_adj']:+.3f} 損平={r['new']['breakeven_bps']:6.1f}bps  "
              f"vs baseline risk-adj={r['base']['risk_adj']:+.3f} 損平={r['base']['breakeven_bps']:6.1f}bps  "
              f"{'贏' if beats else '輸'}")
    print(f"\n  4折中有{n_wins}折cross_sectional_rs版本在holdout上risk-adj優於baseline")


if __name__ == "__main__":
    main()
