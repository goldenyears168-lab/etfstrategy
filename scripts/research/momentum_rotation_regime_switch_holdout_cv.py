"""2026-08-13：把今天兩個各自有機制解釋、但適用場合互斥的發現合起來測——
盤整regime下baseline/大波動留倉表現最好（見momentum_rotation_blindspot_hunt.py
發現A：W3損平+17.4bps遠勝其他3窗口），趨勢regime下baseline很差、但緊門檻+
固定10秒快進快出的tight_scalp反而穩健(4折3贏，見momentum_rotation_
tight_scalp_holdout_cv.py)——單獨用tight_scalp的唯一一敗就是輸在W3盤整窗口。

假說：用TX(台指)大盤的近況（不是個股自己的，避免用未來資訊）當regime分類器，
決定「今天」該用baseline規則還是tight_scalp規則，看切換是否比整年只用單一套
規則更好。

Regime訊號：TX日內高低範圍% = (當日high-low)/當日open，用**當天以前**最近
trailing_days個交易日的平均值代表「近期市況」（嚴格用過去資料，不用當天自己
的range，避免look-ahead——當天range要收盤才知道全貌）。分類門檻：train窗口
的regime訊號中位數（避免額外超參數，用資料自己決定切點）。

高於門檻(近期波動大/趨勢盤) -> 用tight_scalp規則（固定點：2.5x/0.4%/10秒，
今天緊門檻掃描3折贏的代表參數）
低於門檻(近期波動小/盤整) -> 用baseline規則（現行live規格）

4折留一窗口交叉驗證（跟今天後面幾個候選一致，train只用來決定trailing_days
跟門檻，holdout窗口完全沒被用來調過任何參數）。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import simulate_portfolio_day as baseline_simulate  # noqa: E402
from momentum_rotation_redesign_search import WINDOWS, load_window  # noqa: E402
from momentum_rotation_tight_scalp_holdout_cv import simulate_day_tight_scalp  # noqa: E402

_DATA_DIR = os.environ.get("GOLDENSTOCKS_DATA_DIR") or os.path.expanduser("~/goldenstocks-data")
_BARS_DB = os.path.join(_DATA_DIR, "cache", "tmf_channel", "bars.sqlite")

BASE = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
            min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0)
TIGHT_SCALP_PARAMS = dict(breakout_pct=0.5, trail_pct=1.0, rearm_pct=0.25, preempt_mult=2.0,
                           vol_confirm_mult=2.5, min_overshoot_pct=0.4, hold_sec=10.0)
TRAILING_DAYS_GRID = [3, 5, 10, 15, 20]


def load_tx_daily_range(start: str, end: str) -> dict[str, float]:
    """每個交易日的TX日內高低範圍%，用bars.sqlite全庫（不限4窗口），確保
    每個窗口開頭幾天也查得到trailing所需的更早期資料。"""
    conn = sqlite3.connect(_BARS_DB)
    try:
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT day, t, o, h, l FROM bars "
            "WHERE source='tx_1m_tick_built_582d' AND sess='day' AND day BETWEEN ? AND ? "
            "ORDER BY day, t",
            (start, end),
        ).fetchall()
    finally:
        conn.close()
    by_day: dict[str, list[tuple]] = {}
    for day, t, o, h, l in rows:
        by_day.setdefault(day, []).append((t, o, h, l))
    out: dict[str, float] = {}
    for day, day_rows in by_day.items():
        open_px = float(day_rows[0][1])
        hi = max(float(r[2]) for r in day_rows)
        lo = min(float(r[3]) for r in day_rows)
        if open_px > 0:
            out[day] = (hi - lo) / open_px * 100.0
    return out


def trailing_regime_value(day: str, tx_range: dict[str, float], trailing_days: int) -> float | None:
    all_days_sorted = sorted(tx_range.keys())
    if day not in all_days_sorted:
        return None
    idx = all_days_sorted.index(day)
    if idx < trailing_days:
        return None
    window_days = all_days_sorted[idx - trailing_days: idx]
    vals = [tx_range[d] for d in window_days]
    return float(np.mean(vals)) if vals else None


def _run_with_regime(windows_subset: dict, tx_range: dict, trailing_days: int, threshold: float) -> dict:
    all_trades = []
    per_day: dict[str, float] = {}
    for _wname, (all_by_stock, all_days) in windows_subset.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            regime_val = trailing_regime_value(d, tx_range, trailing_days)
            if regime_val is None:
                trades = baseline_simulate(day_data, **BASE)
            elif regime_val > threshold:
                trades = simulate_day_tight_scalp(day_data, **TIGHT_SCALP_PARAMS)
            else:
                trades = baseline_simulate(day_data, **BASE)
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


def _run_pure(windows_subset: dict, sim_fn, **kwargs) -> dict:
    all_trades = []
    per_day: dict[str, float] = {}
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
    if len(rets) == 0 or len(day_rets) == 0 or day_rets.std() == 0:
        return {"n": len(rets), "risk_adj": float("-inf"), "breakeven_bps": 0.0, "win_rate": 0.0}
    breakeven = (rets.sum() / len(rets)) * 100
    win = float(np.mean(rets > 0) * 100)
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    return {"n": len(rets), "risk_adj": day_mean / day_std, "breakeven_bps": breakeven, "win_rate": win}


def main() -> None:
    print("載入4窗口股票資料...")
    all_windows = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    wnames = list(all_windows.keys())

    earliest = min(min(days) for _s, days in all_windows.values())
    latest = max(max(days) for _s, days in all_windows.values())
    query_start = (datetime.fromisoformat(earliest) - timedelta(days=60)).strftime("%Y-%m-%d")
    print(f"載入TX日內範圍（{query_start} ~ {latest}，含trailing緩衝）...")
    tx_range = load_tx_daily_range(query_start, latest)
    print(f"  取得{len(tx_range)}個交易日的TX日內範圍")

    fold_results = []
    for holdout_name in wnames:
        train_windows = {k: v for k, v in all_windows.items() if k != holdout_name}
        holdout_windows = {holdout_name: all_windows[holdout_name]}
        print(f"\n### Fold: holdout={holdout_name} · train={list(train_windows.keys())} ###")

        train_days = sorted({d for _s, days in train_windows.values() for d in days})
        best = None
        for td in TRAILING_DAYS_GRID:
            vals = [trailing_regime_value(d, tx_range, td) for d in train_days]
            vals = [v for v in vals if v is not None]
            if not vals:
                continue
            threshold = float(np.median(vals))
            m = _run_with_regime(train_windows, tx_range, td, threshold)
            if best is None or m["risk_adj"] > best[2]["risk_adj"]:
                best = (td, threshold, m)
        td_best, thr_best, train_m = best
        print(f"  train最佳點: trailing_days={td_best} threshold={thr_best:.3f}% "
              f"(train risk-adj={train_m['risk_adj']:.3f} 損平={train_m['breakeven_bps']:.1f}bps)")

        holdout_switch = _run_with_regime(holdout_windows, tx_range, td_best, thr_best)
        holdout_baseline = _run_pure(holdout_windows, baseline_simulate, **BASE)
        holdout_tight = _run_pure(holdout_windows, simulate_day_tight_scalp, **TIGHT_SCALP_PARAMS)
        print(f"  >>> HOLDOUT({holdout_name}) regime_switch: n={holdout_switch['n']:4d} "
              f"勝率={holdout_switch['win_rate']:5.1f}% 損平={holdout_switch['breakeven_bps']:6.1f}bps risk-adj={holdout_switch['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) 純baseline  : n={holdout_baseline['n']:4d} "
              f"勝率={holdout_baseline['win_rate']:5.1f}% 損平={holdout_baseline['breakeven_bps']:6.1f}bps risk-adj={holdout_baseline['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) 純tight_scalp: n={holdout_tight['n']:4d} "
              f"勝率={holdout_tight['win_rate']:5.1f}% 損平={holdout_tight['breakeven_bps']:6.1f}bps risk-adj={holdout_tight['risk_adj']:+.3f}")
        fold_results.append({"holdout": holdout_name, "td": td_best, "thr": thr_best,
                              "switch": holdout_switch, "baseline": holdout_baseline, "tight": holdout_tight})

    print("\n" + "=" * 100)
    print("=== 4折總結：regime切換 vs 兩個純版本各自最好的那個 ===")
    n_wins = 0
    for r in fold_results:
        best_pure = max(r["baseline"]["risk_adj"], r["tight"]["risk_adj"])
        beats = r["switch"]["risk_adj"] > best_pure
        n_wins += int(beats)
        print(f"  {r['holdout']:12s}: switch risk-adj={r['switch']['risk_adj']:+.3f} 損平={r['switch']['breakeven_bps']:5.1f}bps  "
              f"vs max(純baseline={r['baseline']['risk_adj']:+.3f}, 純tight={r['tight']['risk_adj']:+.3f})  "
              f"{'贏' if beats else '輸'}")
    print(f"\n  4折中有{n_wins}折regime切換版本優於「單獨用兩者中較好的那個」")


if __name__ == "__main__":
    main()
