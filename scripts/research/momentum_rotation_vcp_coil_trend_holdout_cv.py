"""2026-08-13：使用者補上VCP另一個重要前提——量縮盤整必須發生在一個「已經
存在的趨勢」裡（是趨勢中的暫停，不是隨機出現的安靜），突破方向要跟原本的
趨勢一致，不是任何方向都算數。這裡在momentum_rotation_vcp_coil_release_
holdout_cv.py（已修正min_coil_sec嚴謹版）基礎上，疊加一層趨勢方向濾網：
用trend_lookback_min分鐘（3或5分鐘，使用者原話）比較「現在價格」vs
「trend_lookback_min分鐘前價格」決定長期趨勢方向，只有coil release的方向
跟這個長期趨勢一致才承認訊號，不一致就跳過。

三層比較（同一個框架、逐層加條件）：
  A. 純5s微爆量（無coil、無趨勢）
  B. +coil前提（量縮盤整，min_coil_sec嚴謹版）
  C. +coil前提 +趨勢方向一致（本檔新增）
讓每一層的邊際貢獻看得出來，不是只看最終疊加後的數字。

4折留一窗口交叉驗證，跟今天其餘候選同一個標準。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import simulate_portfolio_day as baseline_simulate  # noqa: E402
from momentum_rotation_redesign_search import WINDOWS, load_window  # noqa: E402

TREND_LOOKBACK_MIN_GRID = [3.0, 5.0]
BEST_COIL_PARAMS = dict(contraction_ratio=0.6, min_coil_sec=15.0)  # 中性代表值，見稍早coil_release腳本sweep結果後可調
MIN_TICKS_IN_COIL = 5
BASE_FIXED = dict(trail_pct=1.0, preempt_mult=2.0, window_sec=5.0, cooldown_sec=10.0,
                   move_thresh_pct=0.15, vol_mult=2.5, hold_sec=8.0)
BASELINE_PARAMS = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
                        min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0)


def simulate_day_coil_trend(
    stock_day_data: dict, *,
    trail_pct: float, preempt_mult: float, window_sec: float, cooldown_sec: float,
    move_thresh_pct: float, vol_mult: float, hold_sec: float,
    require_coil: bool, min_coil_sec: float, contraction_ratio: float,
    require_trend_align: bool, trend_lookback_min: float,
) -> list[dict]:
    merged: list[tuple] = []
    for sid, (times, prices, volumes) in stock_day_data.items():
        for k in range(len(times)):
            merged.append((datetime.fromisoformat(times[k]), sid, float(prices[k]), float(volumes[k])))
    merged.sort(key=lambda x: x[0])

    buf: dict[str, list[tuple]] = {sid: [] for sid in stock_day_data}
    coil_buf: dict[str, list[tuple]] = {sid: [] for sid in stock_day_data}
    trend_buf: dict[str, list[tuple]] = {sid: [] for sid in stock_day_data}
    vol_hist: dict[str, list[float]] = {sid: [] for sid in stock_day_data}
    last_signal_dt: dict[str, datetime | None] = {sid: None for sid in stock_day_data}
    last_price: dict[str, float] = {sid: 0.0 for sid in stock_day_data}

    trades: list[dict] = []
    position: dict | None = None
    win_td = timedelta(seconds=window_sec)
    coil_buf_td = timedelta(seconds=min_coil_sec * 2.0)
    trend_buf_td = timedelta(seconds=trend_lookback_min * 60.0)
    cool_td = timedelta(seconds=cooldown_sec)

    def _is_coiled(sid: str, t: datetime) -> bool:
        cb = coil_buf[sid]
        quiet_start = t - timedelta(seconds=min_coil_sec)
        ref_start = t - timedelta(seconds=min_coil_sec * 2.0)
        recent = [r for r in cb if r[0] >= quiet_start]
        reference = [r for r in cb if ref_start <= r[0] < quiet_start]
        if len(recent) < MIN_TICKS_IN_COIL or len(reference) < MIN_TICKS_IN_COIL:
            return False
        recent_prices = [r[1] for r in recent]
        ref_prices = [r[1] for r in reference]
        recent_range = max(recent_prices) - min(recent_prices)
        ref_range = max(ref_prices) - min(ref_prices)
        recent_vol = sum(r[2] for r in recent)
        ref_vol = sum(r[2] for r in reference)
        if ref_range <= 0 or ref_vol <= 0:
            return False
        return recent_range <= ref_range * contraction_ratio and recent_vol <= ref_vol * contraction_ratio

    def _trend_direction(sid: str) -> str | None:
        tb = trend_buf[sid]
        if len(tb) < 2:
            return None
        span_sec = (tb[-1][0] - tb[0][0]).total_seconds()
        if span_sec < trend_lookback_min * 60.0 * 0.5:
            return None  # 涵蓋不到視窗一半長度(例如剛開盤)，不夠格判斷趨勢
        oldest_p, newest_p = tb[0][1], tb[-1][1]
        if newest_p > oldest_p:
            return "long"
        if newest_p < oldest_p:
            return "short"
        return None

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
        tb = trend_buf[sid]
        tb.append((t, p, v))
        while tb and (t - tb[0][0]) > trend_buf_td:
            tb.pop(0)

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

        if require_trend_align:
            trend_dir = _trend_direction(sid)
            if trend_dir is None or trend_dir != direction:
                continue

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


def _run(windows_subset: dict, sim_fn, **kwargs) -> dict:
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

    print(f"\nsweep grid: trend_lookback_min={TREND_LOOKBACK_MIN_GRID}（coil參數固定用代表值 {BEST_COIL_PARAMS}）")

    fold_results = []
    for holdout_name in wnames:
        train_windows = {k: v for k, v in all_windows.items() if k != holdout_name}
        holdout_windows = {holdout_name: all_windows[holdout_name]}
        print(f"\n### Fold: holdout={holdout_name} · train={list(train_windows.keys())} ###")

        best = None
        for tl in TREND_LOOKBACK_MIN_GRID:
            m = _run(train_windows, simulate_day_coil_trend, **BASE_FIXED, **BEST_COIL_PARAMS,
                     require_coil=True, require_trend_align=True, trend_lookback_min=tl)
            if best is None or m["risk_adj"] > best[1]["risk_adj"]:
                best = (tl, m)
        tl_b, train_m = best
        print(f"  train最佳點: trend_lookback_min={tl_b} "
              f"(train risk-adj={train_m['risk_adj']:.3f} 損平={train_m['breakeven_bps']:.1f}bps n={train_m['n']})")

        holdout_c = _run(holdout_windows, simulate_day_coil_trend, **BASE_FIXED, **BEST_COIL_PARAMS,
                          require_coil=True, require_trend_align=True, trend_lookback_min=tl_b)
        holdout_b = _run(holdout_windows, simulate_day_coil_trend, **BASE_FIXED, **BEST_COIL_PARAMS,
                          require_coil=True, require_trend_align=False, trend_lookback_min=tl_b)
        holdout_a = _run(holdout_windows, simulate_day_coil_trend, **BASE_FIXED,
                          contraction_ratio=1.0, min_coil_sec=10.0,
                          require_coil=False, require_trend_align=False, trend_lookback_min=tl_b)
        holdout_baseline = _run(holdout_windows, baseline_simulate, **BASELINE_PARAMS)
        print(f"  >>> HOLDOUT({holdout_name}) C.coil+趨勢一致: n={holdout_c['n']:4d} "
              f"勝率={holdout_c['win_rate']:5.1f}% 損平={holdout_c['breakeven_bps']:6.1f}bps risk-adj={holdout_c['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) B.僅coil       : n={holdout_b['n']:4d} "
              f"勝率={holdout_b['win_rate']:5.1f}% 損平={holdout_b['breakeven_bps']:6.1f}bps risk-adj={holdout_b['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) A.純微爆量(無濾網): n={holdout_a['n']:4d} "
              f"勝率={holdout_a['win_rate']:5.1f}% 損平={holdout_a['breakeven_bps']:6.1f}bps risk-adj={holdout_a['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) baseline(現行規格): n={holdout_baseline['n']:4d} "
              f"勝率={holdout_baseline['win_rate']:5.1f}% 損平={holdout_baseline['breakeven_bps']:6.1f}bps risk-adj={holdout_baseline['risk_adj']:+.3f}")
        fold_results.append({"holdout": holdout_name, "tl": tl_b, "C": holdout_c, "B": holdout_b,
                              "A": holdout_a, "baseline": holdout_baseline})

    print("\n" + "=" * 100)
    print("=== 4折總結：三層邊際貢獻 ===")
    n_c_beats_b = sum(1 for r in fold_results if r["C"]["risk_adj"] > r["B"]["risk_adj"])
    n_c_beats_base = sum(1 for r in fold_results if r["C"]["risk_adj"] > r["baseline"]["risk_adj"])
    for r in fold_results:
        print(f"  {r['holdout']:12s} (trend_lookback={r['tl']}min): "
              f"C(coil+趨勢)={r['C']['risk_adj']:+.3f}/{r['C']['breakeven_bps']:5.1f}bps  "
              f"B(僅coil)={r['B']['risk_adj']:+.3f}/{r['B']['breakeven_bps']:5.1f}bps  "
              f"A(無濾網)={r['A']['risk_adj']:+.3f}/{r['A']['breakeven_bps']:5.1f}bps  "
              f"baseline={r['baseline']['risk_adj']:+.3f}/{r['baseline']['breakeven_bps']:5.1f}bps")
    print(f"\n  C(coil+趨勢) vs B(僅coil): {n_c_beats_b}/4 折趨勢濾網有幫助")
    print(f"  C(coil+趨勢) vs baseline: {n_c_beats_base}/4 折")


if __name__ == "__main__":
    main()
