"""2026-08-13：今天最大的缺口——所有候選（包括retest-entry）都只在同一份
4窗口75天資料上sweep參數、也在同一份資料上報最終數字，沒有任何獨立樣本外
驗證（多agent workflow那邊的OOS agent被安全分類器擋掉沒跑成）。

這裡做4折留一窗口交叉驗證（leave-one-window-out）：每次留一個窗口當holdout、
在其餘3個窗口上sweep buf×timeout找risk-adj最佳點，再把那個點**只**放到留下的
第4個窗口上驗證一次，重複4次（每個窗口輪流當holdout）。如果retest-entry這個
機制是真的（不是特定窗口噪音），4折的holdout表現應該都優於各自窗口在同一份
baseline(immediate-entry)上的表現；如果只是overfit，會看到sweep出來的最佳點
換到沒看過的窗口就崩掉。
"""

from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import simulate_portfolio_day as baseline_simulate  # noqa: E402
from momentum_rotation_retest_entry import WINDOWS, load_window, simulate_day_retest  # noqa: E402

BUF_GRID = [0.4, 0.5, 0.55, 0.6, 0.65]
TIMEOUT_GRID = [120, 180, 240, 300]
BASE = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
            min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0)


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
    if len(rets) == 0 or len(day_rets) == 0 or day_rets.std() == 0:
        return {"n": len(rets), "risk_adj": float("-inf"), "breakeven_bps": 0.0,
                "day_mean": 0.0, "day_std": 0.0, "win_rate": 0.0}
    breakeven = (rets.sum() / len(rets)) * 100
    win = float(np.mean(rets > 0) * 100)
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    return {"n": len(rets), "risk_adj": day_mean / day_std, "breakeven_bps": breakeven,
            "day_mean": day_mean, "day_std": day_std, "win_rate": win}


def main() -> None:
    print("載入4窗口資料...")
    all_windows = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    wnames = list(all_windows.keys())

    print(f"\nsweep grid: buf={BUF_GRID} x timeout={TIMEOUT_GRID} ({len(BUF_GRID)*len(TIMEOUT_GRID)}組)")
    print("=" * 100)

    fold_results = []
    for holdout_name in wnames:
        train_windows = {k: v for k, v in all_windows.items() if k != holdout_name}
        holdout_windows = {holdout_name: all_windows[holdout_name]}

        print(f"\n### Fold: holdout={holdout_name} · train={list(train_windows.keys())} ###")

        best = None
        for buf in BUF_GRID:
            for timeout in TIMEOUT_GRID:
                m = _run(simulate_day_retest, train_windows, **BASE,
                          retest_buffer_pct=buf, retest_timeout_sec=timeout, max_extension_pct=3.0)
                if best is None or m["risk_adj"] > best[2]["risk_adj"]:
                    best = (buf, timeout, m)
        buf_best, timeout_best, train_m = best
        print(f"  train最佳點: buf={buf_best}% timeout={timeout_best}s "
              f"(train risk-adj={train_m['risk_adj']:.3f} 損平={train_m['breakeven_bps']:.1f}bps 勝率={train_m['win_rate']:.1f}%)")

        holdout_retest = _run(simulate_day_retest, holdout_windows, **BASE,
                               retest_buffer_pct=buf_best, retest_timeout_sec=timeout_best, max_extension_pct=3.0)
        holdout_baseline = _run(baseline_simulate, holdout_windows, **BASE)

        print(f"  >>> HOLDOUT({holdout_name}) retest  : n={holdout_retest['n']:4d} "
              f"勝率={holdout_retest['win_rate']:5.1f}% 損平={holdout_retest['breakeven_bps']:6.1f}bps "
              f"risk-adj={holdout_retest['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) baseline: n={holdout_baseline['n']:4d} "
              f"勝率={holdout_baseline['win_rate']:5.1f}% 損平={holdout_baseline['breakeven_bps']:6.1f}bps "
              f"risk-adj={holdout_baseline['risk_adj']:+.3f}")

        fold_results.append({
            "holdout": holdout_name, "buf": buf_best, "timeout": timeout_best,
            "retest": holdout_retest, "baseline": holdout_baseline,
        })

    print("\n" + "=" * 100)
    print("=== 4折總結：retest-entry在「沒看過」的窗口上是否穩定優於baseline？ ===")
    n_wins = 0
    for r in fold_results:
        beats = r["retest"]["risk_adj"] > r["baseline"]["risk_adj"]
        n_wins += int(beats)
        print(f"  {r['holdout']:12s} (best buf={r['buf']}%/timeout={r['timeout']}s): "
              f"retest risk-adj={r['retest']['risk_adj']:+.3f} 損平={r['retest']['breakeven_bps']:5.1f}bps  "
              f"vs baseline risk-adj={r['baseline']['risk_adj']:+.3f} 損平={r['baseline']['breakeven_bps']:5.1f}bps  "
              f"{'贏' if beats else '輸'}")
    print(f"\n  4折中有{n_wins}折retest-entry在holdout上risk-adj優於baseline")


if __name__ == "__main__":
    main()
