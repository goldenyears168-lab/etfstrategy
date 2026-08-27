"""2026-08-13：使用者指定的精確組合——coil從稍早卡死樣本數的10~30秒放鬆到
3秒，趨勢視窗固定3分鐘（不跟5分鐘一起sweep，避免又把樣本切更薄）。目的是
在保留「量縮盤整+趨勢一致」這兩個VCP前提的同時，把樣本量從稍早的個位數~
十幾筆拉回到至少可以看方向的規模。

只sweep contraction_ratio（coil的收縮嚴格度），min_coil_sec=3.0秒與
trend_lookback_min=3.0分鐘都固定，避免三個維度一起sweep又把訓練集切太薄。
複用momentum_rotation_vcp_coil_trend_holdout_cv.py的simulate_day_coil_trend，
不重寫核心邏輯。

使用者接著要求把爆量偵測本身的滾動視窗從5秒也縮到1秒（window_sec=1.0）——
不是coil前提的秒數，是「多久內看到量價齊揚才算爆量」這個判斷本身的解析度。
"""

from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import simulate_portfolio_day as baseline_simulate  # noqa: E402
from momentum_rotation_redesign_search import WINDOWS, load_window  # noqa: E402
from momentum_rotation_vcp_coil_trend_holdout_cv import simulate_day_coil_trend  # noqa: E402

CONTRACTION_RATIO_GRID = [0.5, 0.6, 0.7, 0.8, 0.9]
MIN_COIL_SEC = 3.0
TREND_LOOKBACK_MIN = 3.0
BASE_FIXED = dict(trail_pct=1.0, preempt_mult=2.0, window_sec=1.0, cooldown_sec=10.0,
                   move_thresh_pct=0.15, vol_mult=2.5, hold_sec=8.0)
BASELINE_PARAMS = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
                        min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0)


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

    print(f"\nsweep grid: contraction_ratio={CONTRACTION_RATIO_GRID}（固定 min_coil_sec={MIN_COIL_SEC}s, "
          f"trend_lookback_min={TREND_LOOKBACK_MIN}）")

    fold_results = []
    for holdout_name in wnames:
        train_windows = {k: v for k, v in all_windows.items() if k != holdout_name}
        holdout_windows = {holdout_name: all_windows[holdout_name]}
        print(f"\n### Fold: holdout={holdout_name} · train={list(train_windows.keys())} ###")

        best = None
        for cr in CONTRACTION_RATIO_GRID:
            m = _run(train_windows, simulate_day_coil_trend, **BASE_FIXED,
                     contraction_ratio=cr, min_coil_sec=MIN_COIL_SEC,
                     require_coil=True, require_trend_align=True, trend_lookback_min=TREND_LOOKBACK_MIN)
            if best is None or m["risk_adj"] > best[1]["risk_adj"]:
                best = (cr, m)
        cr_b, train_m = best
        print(f"  train最佳點: contraction_ratio={cr_b} "
              f"(train risk-adj={train_m['risk_adj']:.3f} 損平={train_m['breakeven_bps']:.1f}bps n={train_m['n']})")

        holdout_new = _run(holdout_windows, simulate_day_coil_trend, **BASE_FIXED,
                            contraction_ratio=cr_b, min_coil_sec=MIN_COIL_SEC,
                            require_coil=True, require_trend_align=True, trend_lookback_min=TREND_LOOKBACK_MIN)
        holdout_coil_only = _run(holdout_windows, simulate_day_coil_trend, **BASE_FIXED,
                                  contraction_ratio=cr_b, min_coil_sec=MIN_COIL_SEC,
                                  require_coil=True, require_trend_align=False, trend_lookback_min=TREND_LOOKBACK_MIN)
        holdout_baseline = _run(holdout_windows, baseline_simulate, **BASELINE_PARAMS)
        print(f"  >>> HOLDOUT({holdout_name}) 3s-coil+3m趨勢: n={holdout_new['n']:4d} "
              f"勝率={holdout_new['win_rate']:5.1f}% 損平={holdout_new['breakeven_bps']:6.1f}bps risk-adj={holdout_new['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) 3s-coil(無趨勢): n={holdout_coil_only['n']:4d} "
              f"勝率={holdout_coil_only['win_rate']:5.1f}% 損平={holdout_coil_only['breakeven_bps']:6.1f}bps risk-adj={holdout_coil_only['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) baseline(現行規格): n={holdout_baseline['n']:4d} "
              f"勝率={holdout_baseline['win_rate']:5.1f}% 損平={holdout_baseline['breakeven_bps']:6.1f}bps risk-adj={holdout_baseline['risk_adj']:+.3f}")
        fold_results.append({"holdout": holdout_name, "cr": cr_b, "new": holdout_new,
                              "coil_only": holdout_coil_only, "baseline": holdout_baseline})

    print("\n" + "=" * 100)
    print("=== 4折總結 ===")
    n_wins = sum(1 for r in fold_results if r["new"]["risk_adj"] > r["baseline"]["risk_adj"])
    for r in fold_results:
        print(f"  {r['holdout']:12s} (ratio={r['cr']}): 3s-coil+3m趨勢 n={r['new']['n']} "
              f"risk-adj={r['new']['risk_adj']:+.3f} 損平={r['new']['breakeven_bps']:5.1f}bps  "
              f"vs baseline risk-adj={r['baseline']['risk_adj']:+.3f} 損平={r['baseline']['breakeven_bps']:5.1f}bps")
    print(f"\n  {n_wins}/4 折優於baseline")


if __name__ == "__main__":
    main()
