"""2026-08-13：使用者提出VCP（Volatility Contraction Pattern）式的假說——不是
「爆量就進場」，是「先確認量縮盤整（coil），再吃從盤整裡真正釋放出來的那個
方向爆發」。這個repo的src/vcp_tm/是給日K用的Minervini式VCP篩選（多週時間尺度），
這裡把同一個「先收縮、再釋放」的概念下放到秒級。

⚠️ 2026-08-13第一版被使用者正確抓到一個弱點：原本的coil判斷只是把
lookback視窗切前後兩半比較，每半段只要求≥2筆tick就承認——tick稀疏的標的
（例如2345/2383這種低流動性的）很容易被湊巧的2、3筆資料誤判成「盤整」，
根本沒有真正確認過「連續一段時間都很安靜」。使用者明確要求「盤整至少十秒」
——這裡改成更嚴格的版本：

  1. min_coil_sec：訊號前**必須連續**這麼長時間才算數（起點10秒，可調更長）。
  2. 同時要求該段時間本身**tick密度足夠**（MIN_TICKS_IN_COIL，固定5筆）——
     tick數不夠，直接判定「不夠格判斷」而不是「算coiled」，避免稀疏資料
     被誤判成安靜。
  3. 拿這段「最近min_coil_sec秒」跟**再往前min_coil_sec秒**的參照段比較
     （而不是同一個視窗切兩半），參照段一樣要求tick數足夠，兩段都通過
     密度門檻，才比較range/量是否真的收縮(contraction_ratio)。

只有這個更嚴格的coil前提成立，才承認接下來的爆量突破訊號是真正的coil
release，否則忽略（回到armed狀態繼續等）。其餘（單槽位輪動+動態搶佔+固定
秒數出場+保護性trailing stop）完全沿用momentum_rotation_5s_microburst_
holdout_cv.py同一套框架，只加這一個前置條件。

4折留一窗口交叉驗證，跟今天其餘候選同一個標準。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import simulate_portfolio_day as baseline_simulate  # noqa: E402
from momentum_rotation_redesign_search import WINDOWS, load_window  # noqa: E402

CONTRACTION_RATIO_GRID = [0.4, 0.5, 0.6, 0.7, 0.8]
MIN_COIL_SEC_GRID = [10.0, 15.0, 20.0, 30.0]
MIN_TICKS_IN_COIL = 5
BASE_FIXED = dict(trail_pct=1.0, preempt_mult=2.0, window_sec=5.0, cooldown_sec=10.0,
                   move_thresh_pct=0.15, vol_mult=2.5, hold_sec=8.0)
BASELINE_PARAMS = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
                        min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0)


def simulate_day_coil_release(
    stock_day_data: dict, *,
    trail_pct: float, preempt_mult: float, window_sec: float, cooldown_sec: float,
    move_thresh_pct: float, vol_mult: float, hold_sec: float,
    require_coil: bool, min_coil_sec: float, contraction_ratio: float,
) -> list[dict]:
    merged: list[tuple] = []
    for sid, (times, prices, volumes) in stock_day_data.items():
        for k in range(len(times)):
            merged.append((datetime.fromisoformat(times[k]), sid, float(prices[k]), float(volumes[k])))
    merged.sort(key=lambda x: x[0])

    buf: dict[str, list[tuple]] = {sid: [] for sid in stock_day_data}
    coil_buf: dict[str, list[tuple]] = {sid: [] for sid in stock_day_data}
    vol_hist: dict[str, list[float]] = {sid: [] for sid in stock_day_data}
    last_signal_dt: dict[str, datetime | None] = {sid: None for sid in stock_day_data}
    last_price: dict[str, float] = {sid: 0.0 for sid in stock_day_data}

    trades: list[dict] = []
    position: dict | None = None
    win_td = timedelta(seconds=window_sec)
    coil_buf_td = timedelta(seconds=min_coil_sec * 2.0)
    cool_td = timedelta(seconds=cooldown_sec)

    def _is_coiled(sid: str, t: datetime) -> bool:
        cb = coil_buf[sid]
        quiet_start = t - timedelta(seconds=min_coil_sec)
        ref_start = t - timedelta(seconds=min_coil_sec * 2.0)
        recent = [r for r in cb if r[0] >= quiet_start]
        reference = [r for r in cb if ref_start <= r[0] < quiet_start]
        if len(recent) < MIN_TICKS_IN_COIL or len(reference) < MIN_TICKS_IN_COIL:
            return False  # tick密度不夠，不夠格判斷，保守判定不算coiled
        recent_prices = [r[1] for r in recent]
        ref_prices = [r[1] for r in reference]
        recent_range = max(recent_prices) - min(recent_prices)
        ref_range = max(ref_prices) - min(ref_prices)
        recent_vol = sum(r[2] for r in recent)
        ref_vol = sum(r[2] for r in reference)
        if ref_range <= 0 or ref_vol <= 0:
            return False
        return recent_range <= ref_range * contraction_ratio and recent_vol <= ref_vol * contraction_ratio

    for t, sid, p, v in merged:
        last_price[sid] = p
        b = buf[sid]
        b.append((t, p, v))
        while b and (t - b[0][0]) > win_td:
            b.pop(0)
        cb = coil_buf[sid]
        cb.append((t, p, v))
        while cb and (t - cb[0][0]) > coil_buf_td:
            cb.pop(0)

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

        if require_coil and not _is_coiled(sid, t):
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


def _run(windows_subset: dict, sim_fn, is_coil: bool, **kwargs) -> dict:
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
    if len(rets) < 15 or len(day_rets) == 0 or day_rets.std() == 0:
        return {"n": len(rets), "risk_adj": float("-inf"), "breakeven_bps": 0.0, "win_rate": 0.0}
    breakeven = (rets.sum() / len(rets)) * 100
    win = float(np.mean(rets > 0) * 100)
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    return {"n": len(rets), "risk_adj": day_mean / day_std, "breakeven_bps": breakeven, "win_rate": win}


def main() -> None:
    print("載入4窗口資料...")
    all_windows = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    wnames = list(all_windows.keys())

    print("\n### 對照：無coil前提（純5s微爆量）全4窗口 ###")
    m0 = _run(all_windows, simulate_day_coil_release, False, **BASE_FIXED,
              require_coil=False, min_coil_sec=10.0, contraction_ratio=1.0)
    print(f"  n={m0['n']} 勝率={m0['win_rate']:.1f}% 損平={m0['breakeven_bps']:.1f}bps risk-adj={m0['risk_adj']:+.3f}")

    print(f"\nsweep grid: contraction_ratio={CONTRACTION_RATIO_GRID} x min_coil_sec={MIN_COIL_SEC_GRID} "
          f"({len(CONTRACTION_RATIO_GRID)*len(MIN_COIL_SEC_GRID)}組, 固定MIN_TICKS_IN_COIL={MIN_TICKS_IN_COIL})")

    fold_results = []
    for holdout_name in wnames:
        train_windows = {k: v for k, v in all_windows.items() if k != holdout_name}
        holdout_windows = {holdout_name: all_windows[holdout_name]}
        print(f"\n### Fold: holdout={holdout_name} · train={list(train_windows.keys())} ###")

        best = None
        for cr in CONTRACTION_RATIO_GRID:
            for mc in MIN_COIL_SEC_GRID:
                m = _run(train_windows, simulate_day_coil_release, True, **BASE_FIXED,
                         require_coil=True, min_coil_sec=mc, contraction_ratio=cr)
                if best is None or m["risk_adj"] > best[2]["risk_adj"]:
                    best = (cr, mc, m)
        cr_b, mc_b, train_m = best
        print(f"  train最佳點: contraction_ratio={cr_b} min_coil_sec={mc_b}s "
              f"(train risk-adj={train_m['risk_adj']:.3f} 損平={train_m['breakeven_bps']:.1f}bps n={train_m['n']})")

        holdout_coil = _run(holdout_windows, simulate_day_coil_release, True, **BASE_FIXED,
                             require_coil=True, min_coil_sec=mc_b, contraction_ratio=cr_b)
        holdout_no_coil = _run(holdout_windows, simulate_day_coil_release, False, **BASE_FIXED,
                                require_coil=False, min_coil_sec=10.0, contraction_ratio=1.0)
        holdout_baseline = _run(holdout_windows, baseline_simulate, False, **BASELINE_PARAMS)
        print(f"  >>> HOLDOUT({holdout_name}) coil_release: n={holdout_coil['n']:4d} "
              f"勝率={holdout_coil['win_rate']:5.1f}% 損平={holdout_coil['breakeven_bps']:6.1f}bps risk-adj={holdout_coil['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) 無coil前提(純微爆量): n={holdout_no_coil['n']:4d} "
              f"勝率={holdout_no_coil['win_rate']:5.1f}% 損平={holdout_no_coil['breakeven_bps']:6.1f}bps risk-adj={holdout_no_coil['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) baseline(現行規格): n={holdout_baseline['n']:4d} "
              f"勝率={holdout_baseline['win_rate']:5.1f}% 損平={holdout_baseline['breakeven_bps']:6.1f}bps risk-adj={holdout_baseline['risk_adj']:+.3f}")
        fold_results.append({"holdout": holdout_name, "cr": cr_b, "mc": mc_b,
                              "coil": holdout_coil, "no_coil": holdout_no_coil, "baseline": holdout_baseline})

    print("\n" + "=" * 100)
    print("=== 4折總結：coil前提有沒有幫助？===")
    n_wins_vs_nocoil = sum(1 for r in fold_results if r["coil"]["risk_adj"] > r["no_coil"]["risk_adj"])
    n_wins_vs_baseline = sum(1 for r in fold_results if r["coil"]["risk_adj"] > r["baseline"]["risk_adj"])
    for r in fold_results:
        print(f"  {r['holdout']:12s} (ratio={r['cr']},min_coil={r['mc']}s): "
              f"coil risk-adj={r['coil']['risk_adj']:+.3f} 損平={r['coil']['breakeven_bps']:5.1f}bps  "
              f"vs 無coil risk-adj={r['no_coil']['risk_adj']:+.3f} 損平={r['no_coil']['breakeven_bps']:5.1f}bps  "
              f"vs baseline risk-adj={r['baseline']['risk_adj']:+.3f}")
    print(f"\n  coil前提 vs 無coil前提: {n_wins_vs_nocoil}/4 折coil較好")
    print(f"  coil前提 vs baseline: {n_wins_vs_baseline}/4 折coil較好")


if __name__ == "__main__":
    main()
