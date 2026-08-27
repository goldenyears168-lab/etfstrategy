"""2026-08-13：全新訊號框架——VWAP乖離回歸（mean reversion），不是開盤突破延續。

想法：對每檔個股期貨，用逐筆tick即時算當日累積VWAP（sum(price*volume)/sum(volume)，
從當天第一筆tick開始累積，不是跟大盤比較，是跟自己的當日成交量加權均價比較）。
當價格偏離VWAP超過 dev_entry_pct%、且觸發那一筆的成交量放大（>= vol_confirm_mult ×
該標的當日成交量中位數baseline，邏輯跟momentum_breakout_strategy一致），賭的是
「這是短線過度反應（追價/急殺），量能異常放大反而代表籌碼在極端價位換手，
價格會被拉回VWAP」——跟開盤突破策略的動能延續假說完全相反方向。

方向：價格在VWAP之上超過門檻 -> SHORT（賭拉回）；價格在VWAP之下超過門檻 ->
LONG（賭反彈）。

出場規則（單槽位輪動，複用momentum_breakout_strategy.simulate_portfolio_day
同一套「合併全標的tick成單一時間軸掃描」的骨架，一次只能持有一檔）：
  - 停利：價格反轉碰到當下即時VWAP（不是進場當時凍結的VWAP——VWAP本身還在隨
    當天後續成交累積更新，用「當下」VWAP才是誠實的均值回歸目標）。
  - 停損：進場後價格繼續朝原偏離方向擴大達 stop_pct%（相對進場價的固定停損，
    不是移動停利——均值回歸沒有「趨勢」可追蹤，用固定停損比較合理：一旦繼續
    背離就代表這次不是過度反應、是真的在噴，及早停損認錯）。
  - 尾盤仍未出場：強制在最後一筆tick平倉，不留倉。
  - 重新武裝：出場後該標的進入未武裝狀態，價格要先回到當下VWAP±rearm_dev_pct
    內才重新武裝——防止停損出場後（此時價格離VWAP更遠）立刻對同一段延續中的
    走勢重複進場攤平式加碼。
  - 搶佔機制**保留**（延續現有12檔輪動框架、使用者今天明確要求）：持倉中若有
    新訊號分數 ≥ 目前持倉進場分數 × preempt_mult（=2.0，比照現行baseline，不
    另外sweep），提前用當下市價平倉、搶進更好的機會；分數定義比照breakout版本
    ＝overshoot（乖離幅度%）× vol_ratio（量能倍數）。
  - fill/exit全部用訊號當下真實tick價（float(p)），不是理論門檻/VWAP值——這是
    今天已經修過兩次的bug，這裡從一開始就用對的假設寫。

4折留一窗口交叉驗證（leave-one-window-out）：每次留一個窗口當holdout，在其餘
3個窗口上sweep (dev_entry_pct, vol_confirm_mult, stop_pct) 找risk-adj
（日均報酬/日std）最佳點，只把那個點套用到holdout窗口驗證一次，跟現行live
baseline規格（momentum_breakout_strategy.simulate_portfolio_day，breakout_pct
=0.5/trail_pct=1.0/vol_confirm_mult=1.5/rearm_pct=0.25/min_overshoot_pct=0.15/
min_vol_ratio=1.5/preempt_mult=2.0）比較risk-adj。

用法：
  PYTHONPATH=src .venv/bin/python -u \
      scripts/research/momentum_rotation_vwap_reversion_holdout_cv.py
"""

from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import simulate_portfolio_day as baseline_simulate  # noqa: E402
from momentum_rotation_redesign_search import WINDOWS, load_window  # noqa: E402

REARM_DEV_PCT = 0.15  # 出場後要回到VWAP±此百分比內才重新武裝（固定，不sweep）
PREEMPT_MULT = 2.0  # 搶佔倍數門檻（固定，比照現行baseline，不sweep）
MIN_WARMUP_TICKS = 10  # 每檔標的至少累積這麼多筆tick才開始評估進場（VWAP太早不穩）

BASELINE_PARAMS = dict(
    breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
    min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0,
)

DEV_GRID = [0.3, 0.5, 0.8]
VOL_GRID = [1.5, 2.5, 3.5]
STOP_GRID = [0.5, 1.0]


def simulate_day_vwap_reversion(
    stock_day_data: dict[str, tuple[list[str], np.ndarray, np.ndarray]],
    *,
    dev_entry_pct: float,
    vol_confirm_mult: float,
    stop_pct: float,
    rearm_dev_pct: float = REARM_DEV_PCT,
    preempt_mult: float = PREEMPT_MULT,
) -> list[dict]:
    """單日、單槽位輪動VWAP乖離回歸模擬。跟simulate_portfolio_day同一種骨架
    （合併全標的tick成單一時間軸），差異只在訊號方向（回歸不是延續）跟出場
    規則（固定停損+VWAP停利，不是移動停利）。
    """
    merged: list[tuple[str, str, float, float]] = []
    stocks = [sid for sid, (times, prices, _v) in stock_day_data.items() if prices.size >= 2]
    for sid in stocks:
        times, prices, volumes = stock_day_data[sid]
        for k in range(len(times)):
            merged.append((times[k], sid, float(prices[k]), float(volumes[k])))
    merged.sort(key=lambda x: x[0])

    vwap_num: dict[str, float] = {sid: 0.0 for sid in stocks}
    vwap_den: dict[str, float] = {sid: 0.0 for sid in stocks}
    tick_count: dict[str, int] = {sid: 0 for sid in stocks}
    vol_history: dict[str, list[float]] = {sid: [] for sid in stocks}
    armed: dict[str, bool] = {sid: True for sid in stocks}
    last_price: dict[str, float] = {sid: 0.0 for sid in stocks}
    trades: list[dict] = []
    position: dict | None = None

    for t, sid, p, v in merged:
        last_price[sid] = p
        vh = vol_history[sid]
        baseline = max(np.median(vh), 1e-9) if vh else 1.0
        vh.append(v)

        # VWAP 用這筆tick更新之前的狀態算，避免用「這筆自己」污染判斷基準
        vwap_now = (vwap_num[sid] / vwap_den[sid]) if vwap_den[sid] > 0 else p
        tick_count[sid] += 1

        is_held = position is not None and position["sid"] == sid
        if is_held:
            direction = position["direction"]
            if direction == "long":
                hit_target = p >= vwap_now
                hit_stop = p <= position["fill"] * (1 - stop_pct / 100.0)
            else:
                hit_target = p <= vwap_now
                hit_stop = p >= position["fill"] * (1 + stop_pct / 100.0)
            if hit_target or hit_stop:
                exit_price = float(p)
                ret_pct = (
                    (exit_price - position["fill"]) / position["fill"] * 100.0
                    if direction == "long"
                    else (position["fill"] - exit_price) / position["fill"] * 100.0
                )
                reason = "target_vwap" if hit_target else "stop_loss"
                trades.append({**position, "exit_time": t, "exit": exit_price, "ret_pct": ret_pct, "reason": reason})
                position = None
                armed[sid] = False
            # 更新VWAP（用當前tick）後才進下一輪；持有中這一輪不再評估新candidate（自己）
            if v > 0:
                vwap_num[sid] += p * v
                vwap_den[sid] += v
            continue

        if v > 0:
            vwap_num[sid] += p * v
            vwap_den[sid] += v

        if not armed[sid]:
            dev_now = abs(p - vwap_now) / vwap_now * 100.0 if vwap_now > 0 else 0.0
            if dev_now <= rearm_dev_pct:
                armed[sid] = True
            continue

        if tick_count[sid] < MIN_WARMUP_TICKS or vwap_now <= 0:
            continue

        deviation_pct = (p - vwap_now) / vwap_now * 100.0
        if abs(deviation_pct) < dev_entry_pct or v < vol_confirm_mult * baseline:
            continue
        direction = "short" if deviation_pct > 0 else "long"  # 過高賭拉回(short)/過低賭反彈(long)
        overshoot = abs(deviation_pct)
        vol_ratio = v / baseline
        score = overshoot * vol_ratio
        fill = float(p)
        candidate = {
            "sid": sid, "direction": direction, "fill": fill, "entry": fill,
            "entry_time": t, "entry_score": score, "overshoot": overshoot, "vol_ratio": vol_ratio,
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


def _run(sim_fn, windows_subset: dict, **kwargs) -> dict:
    all_trades = []
    per_day: dict[str, float] = {}
    for _wname, (all_by_stock, all_days) in windows_subset.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            result = sim_fn(day_data, **kwargs)
            trades = result[0] if isinstance(result, tuple) else result
            all_trades.extend(trades)
            per_day[d] = per_day.get(d, 0.0) + sum(t["ret_pct"] for t in trades)
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    day_rets = np.array(list(per_day.values())) if per_day else np.array([])
    if len(rets) < 20 or len(day_rets) == 0 or day_rets.std() == 0:
        return {"n": len(rets), "risk_adj": float("-inf"), "breakeven_bps": 0.0, "win_rate": 0.0,
                "day_mean": 0.0, "day_std": 0.0}
    breakeven = (rets.sum() / len(rets)) * 100
    win = float(np.mean(rets > 0) * 100)
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    return {"n": len(rets), "risk_adj": day_mean / day_std, "breakeven_bps": breakeven, "win_rate": win,
            "day_mean": day_mean, "day_std": day_std}


def main() -> None:
    print("載入4窗口資料...")
    all_windows = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    wnames = list(all_windows.keys())

    print(f"sweep grid: dev_entry_pct={DEV_GRID} x vol_confirm_mult={VOL_GRID} x stop_pct={STOP_GRID} "
          f"({len(DEV_GRID)*len(VOL_GRID)*len(STOP_GRID)}組) · rearm_dev_pct={REARM_DEV_PCT}(固定) "
          f"preempt_mult={PREEMPT_MULT}(固定)")
    print("=" * 100)

    fold_results = []
    for holdout_name in wnames:
        train_windows = {k: v for k, v in all_windows.items() if k != holdout_name}
        holdout_windows = {holdout_name: all_windows[holdout_name]}
        print(f"\n### Fold: holdout={holdout_name} · train={list(train_windows.keys())} ###")

        best = None
        for dev in DEV_GRID:
            for vc in VOL_GRID:
                for sp in STOP_GRID:
                    m = _run(simulate_day_vwap_reversion, train_windows,
                              dev_entry_pct=dev, vol_confirm_mult=vc, stop_pct=sp)
                    if best is None or m["risk_adj"] > best[3]["risk_adj"]:
                        best = (dev, vc, sp, m)
        dev_best, vc_best, sp_best, train_m = best
        print(f"  train最佳點: dev_entry={dev_best}% vol_confirm={vc_best}x stop={sp_best}% "
              f"(train risk-adj={train_m['risk_adj']:.3f} 損平={train_m['breakeven_bps']:.1f}bps "
              f"勝率={train_m['win_rate']:.1f}% n={train_m['n']})")

        holdout_new = _run(simulate_day_vwap_reversion, holdout_windows,
                            dev_entry_pct=dev_best, vol_confirm_mult=vc_best, stop_pct=sp_best)
        holdout_base = _run(baseline_simulate, holdout_windows, **BASELINE_PARAMS)
        print(f"  >>> HOLDOUT({holdout_name}) vwap_reversion: n={holdout_new['n']:4d} "
              f"勝率={holdout_new['win_rate']:5.1f}% 損平={holdout_new['breakeven_bps']:6.1f}bps "
              f"risk-adj={holdout_new['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) baseline(breakout): n={holdout_base['n']:4d} "
              f"勝率={holdout_base['win_rate']:5.1f}% 損平={holdout_base['breakeven_bps']:6.1f}bps "
              f"risk-adj={holdout_base['risk_adj']:+.3f}")
        fold_results.append({
            "holdout": holdout_name, "dev": dev_best, "vc": vc_best, "sp": sp_best,
            "new": holdout_new, "base": holdout_base,
        })

    print("\n" + "=" * 100)
    print("=== 4折總結：VWAP乖離回歸 vs 現行baseline(開盤突破延續) ===")
    n_wins = 0
    for r in fold_results:
        beats = r["new"]["risk_adj"] > r["base"]["risk_adj"]
        n_wins += int(beats)
        print(f"  {r['holdout']:12s} (dev={r['dev']}%/vol={r['vc']}x/stop={r['sp']}%): "
              f"new risk-adj={r['new']['risk_adj']:+.3f} 損平={r['new']['breakeven_bps']:6.1f}bps  "
              f"vs baseline risk-adj={r['base']['risk_adj']:+.3f} 損平={r['base']['breakeven_bps']:6.1f}bps  "
              f"{'贏' if beats else '輸'}")
    print(f"\n  4折中有{n_wins}折VWAP乖離回歸在holdout上risk-adj優於baseline")

    n_positive_breakeven = sum(1 for r in fold_results if r["new"]["breakeven_bps"] > 5.0)
    print(f"  4折中有{n_positive_breakeven}折損平成本 > 5bps（最低成本估計下限）")


if __name__ == "__main__":
    main()
