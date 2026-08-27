"""2026-08-13：使用者提出的精確版本——不跟開盤價比較(現行breakout_pct邏輯)，
純粹看「最近5秒鐘」的滾動視窗：價格有沒有明顯移動、量有沒有明顯放大，兩者同時
成立就跟著方向進場，只抓5~15秒。這是今天完全沒測過的訊號建構方式（現有的都
綁定「跟開盤價的距離」，這個是「隨時偵測突發量價」，理論上能抓到全天任何時刻
的動能爆發，不只是開盤附近）。

訊號定義：對每個時間點t，取最近window_sec秒內的所有tick，算：
  - price_move_pct = (最新價-視窗內最舊價)/視窗內最舊價 * 100
  - vol_burst = 視窗內成交量總和 / 當天到目前為止每個window_sec區間量的中位數
    （用「相同長度視窗」的歷史量當baseline，不是跟單筆tick比較）
兩者都過門檻(|price_move_pct|>=move_thresh_pct 且 vol_burst>=vol_mult)才觸發，
方向=price_move_pct正負號。進場後固定持有hold_sec秒不論賺賠出場（使用者原話
「5-10秒快速進出」），期間仍有保護性trailing stop。同一標的觸發後進入
cooldown_sec冷卻，避免同一段噴出被連續重複觸發。單槽位輪動+動態搶佔沿用
（使用者先前明確要求保留搶佔機制）。

4折留一窗口交叉驗證，從第一次跑就做，不是先報樣本內數字。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import simulate_portfolio_day as baseline_simulate  # noqa: E402
from momentum_rotation_redesign_search import WINDOWS, load_window  # noqa: E402

MOVE_THRESH_GRID = [0.1, 0.15, 0.2, 0.3]
VOL_MULT_GRID = [1.5, 2.0, 3.0, 4.0]
HOLD_SEC_GRID = [5.0, 8.0, 10.0, 15.0]
BASE_FIXED = dict(trail_pct=1.0, preempt_mult=2.0, window_sec=5.0, cooldown_sec=10.0)


def simulate_day_microburst(
    stock_day_data: dict, *,
    trail_pct: float, preempt_mult: float, window_sec: float, cooldown_sec: float,
    move_thresh_pct: float, vol_mult: float, hold_sec: float,
) -> list[dict]:
    merged: list[tuple] = []
    for sid, (times, prices, volumes) in stock_day_data.items():
        for k in range(len(times)):
            merged.append((datetime.fromisoformat(times[k]), sid, float(prices[k]), float(volumes[k])))
    merged.sort(key=lambda x: x[0])

    # per-sid滾動視窗緩衝(deque風格用list+索引清理)，跟量的歷史baseline
    buf: dict[str, list[tuple]] = {sid: [] for sid in stock_day_data}
    vol_hist: dict[str, list[float]] = {sid: [] for sid in stock_day_data}
    last_signal_dt: dict[str, datetime | None] = {sid: None for sid in stock_day_data}
    last_price: dict[str, float] = {sid: 0.0 for sid in stock_day_data}

    trades: list[dict] = []
    position: dict | None = None
    win_td = timedelta(seconds=window_sec)
    cool_td = timedelta(seconds=cooldown_sec)

    for t, sid, p, v in merged:
        last_price[sid] = p
        b = buf[sid]
        b.append((t, p, v))
        while b and (t - b[0][0]) > win_td:
            b.pop(0)

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
            elapsed = (t - datetime.fromisoformat(position["entry_time"])).total_seconds()
            timed_out = elapsed >= hold_sec
            if hit or timed_out:
                exit_price = float(p)
                ret_pct = (
                    (exit_price - position["fill"]) / position["fill"] * 100.0
                    if position["direction"] == "long"
                    else (position["fill"] - exit_price) / position["fill"] * 100.0
                )
                reason = "trail_stop" if hit else "timed_exit"
                trades.append({**position, "exit_time": t.isoformat(), "exit": exit_price, "ret_pct": ret_pct, "reason": reason})
                position = None
            continue

        vh = vol_hist[sid]
        window_vol_sum = sum(row[2] for row in b)
        if len(b) < 2:
            vh.append(window_vol_sum)
            continue
        baseline = max(np.median(vh), 1e-9) if vh else 1.0
        vh.append(window_vol_sum)

        if last_signal_dt[sid] is not None and (t - last_signal_dt[sid]) < cool_td:
            continue

        oldest_p = b[0][1]
        if oldest_p <= 0:
            continue
        price_move_pct = (p - oldest_p) / oldest_p * 100.0
        vol_burst = window_vol_sum / baseline
        if abs(price_move_pct) < move_thresh_pct or vol_burst < vol_mult:
            continue

        direction = "long" if price_move_pct > 0 else "short"
        last_signal_dt[sid] = t
        score = abs(price_move_pct) * vol_burst
        fill = float(p)
        candidate = {
            "sid": sid, "direction": direction, "fill": fill, "entry": fill,
            "entry_time": t.isoformat(), "entry_score": score, "peak_trough": fill,
        }

        if position is None:
            position = candidate
        elif score >= preempt_mult * position["entry_score"]:
            held_sid = position["sid"]
            exit_price = last_price[held_sid]
            ret_pct = (
                (exit_price - position["fill"]) / position["fill"] * 100.0
                if position["direction"] == "long"
                else (position["fill"] - exit_price) / position["fill"] * 100.0
            )
            trades.append({**position, "exit_time": t.isoformat(), "exit": exit_price, "ret_pct": ret_pct, "reason": "preempted"})
            position = candidate

    if position is not None:
        exit_price = last_price[position["sid"]]
        ret_pct = (
            (exit_price - position["fill"]) / position["fill"] * 100.0
            if position["direction"] == "long"
            else (position["fill"] - exit_price) / position["fill"] * 100.0
        )
        trades.append({**position, "exit_time": "day_end", "exit": exit_price, "ret_pct": ret_pct, "reason": "day_end_forced"})
    return trades


def _run(windows_subset: dict, sim_fn, is_micro: bool, **kwargs) -> dict:
    all_trades, per_day = [], {}
    for _wname, (all_by_stock, all_days) in windows_subset.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            trades = sim_fn(day_data, **kwargs)
            all_trades.extend(trades)
            per_day[d] = per_day.get(d, 0.0) + sum(t["ret_pct"] for t in trades)
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    day_rets = np.array(list(per_day.values())) if per_day else np.array([])
    if len(rets) < 20 or len(day_rets) == 0 or day_rets.std() == 0:
        return {"n": len(rets), "risk_adj": float("-inf"), "breakeven_bps": 0.0, "win_rate": 0.0}
    breakeven = (rets.sum() / len(rets)) * 100
    win = float(np.mean(rets > 0) * 100)
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    return {"n": len(rets), "risk_adj": day_mean / day_std, "breakeven_bps": breakeven, "win_rate": win}


def main() -> None:
    print("載入4窗口資料...")
    all_windows = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    wnames = list(all_windows.keys())
    baseline_params = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
                            min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0)

    print(f"\nsweep grid: move_thresh={MOVE_THRESH_GRID} x vol_mult={VOL_MULT_GRID} x hold_sec={HOLD_SEC_GRID} "
          f"({len(MOVE_THRESH_GRID)*len(VOL_MULT_GRID)*len(HOLD_SEC_GRID)}組)")

    fold_results = []
    for holdout_name in wnames:
        train_windows = {k: v for k, v in all_windows.items() if k != holdout_name}
        holdout_windows = {holdout_name: all_windows[holdout_name]}
        print(f"\n### Fold: holdout={holdout_name} · train={list(train_windows.keys())} ###")

        best = None
        for mt in MOVE_THRESH_GRID:
            for vm in VOL_MULT_GRID:
                for hs in HOLD_SEC_GRID:
                    m = _run(train_windows, simulate_day_microburst, True, **BASE_FIXED,
                             move_thresh_pct=mt, vol_mult=vm, hold_sec=hs)
                    if best is None or m["risk_adj"] > best[3]["risk_adj"]:
                        best = (mt, vm, hs, m)
        mt_b, vm_b, hs_b, train_m = best
        print(f"  train最佳點: move_thresh={mt_b}% vol_mult={vm_b}x hold_sec={hs_b}s "
              f"(train risk-adj={train_m['risk_adj']:.3f} 損平={train_m['breakeven_bps']:.1f}bps n={train_m['n']})")

        holdout_new = _run(holdout_windows, simulate_day_microburst, True, **BASE_FIXED,
                            move_thresh_pct=mt_b, vol_mult=vm_b, hold_sec=hs_b)
        holdout_base = _run(holdout_windows, baseline_simulate, False, **baseline_params)
        print(f"  >>> HOLDOUT({holdout_name}) 5s_microburst: n={holdout_new['n']:4d} "
              f"勝率={holdout_new['win_rate']:5.1f}% 損平={holdout_new['breakeven_bps']:6.1f}bps risk-adj={holdout_new['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) baseline      : n={holdout_base['n']:4d} "
              f"勝率={holdout_base['win_rate']:5.1f}% 損平={holdout_base['breakeven_bps']:6.1f}bps risk-adj={holdout_base['risk_adj']:+.3f}")
        fold_results.append({"holdout": holdout_name, "mt": mt_b, "vm": vm_b, "hs": hs_b,
                              "new": holdout_new, "base": holdout_base})

    print("\n" + "=" * 100)
    print("=== 4折總結 ===")
    n_wins = 0
    for r in fold_results:
        beats = r["new"]["risk_adj"] > r["base"]["risk_adj"]
        n_wins += int(beats)
        print(f"  {r['holdout']:12s} (move={r['mt']}%,vol={r['vm']}x,hold={r['hs']}s): "
              f"new risk-adj={r['new']['risk_adj']:+.3f} 損平={r['new']['breakeven_bps']:5.1f}bps  "
              f"vs baseline risk-adj={r['base']['risk_adj']:+.3f} 損平={r['base']['breakeven_bps']:5.1f}bps  {'贏' if beats else '輸'}")
    print(f"\n  4折中有{n_wins}折5秒滾動微爆量版本在holdout上risk-adj優於baseline")


if __name__ == "__main__":
    main()
