"""2026-08-14：把micro VCP在190檔broad universe上驗證過的命中率結果（三階段
sweep後，holdout組38.6% vs 對照組27.5%，見momentum_rotation_broad_universe_
staged_sweep.py）轉換成真正的損益——命中率只回答「方向對不對」，不是「扣完
成本還有沒有賺」，這裡把完全一樣的最佳參數包進真正的交易模擬（simulate_day_
coil_trend，含單槽位輪動+動態搶佔+固定8秒出場+保護性trailing stop+真實
tick價進出場），只在HOLDOUT組（完全沒看過參數選擇過程的95檔，同一組種子42
切分）上跑一次，避免用同一份資料選參數又拿來宣稱獲利。

最佳參數（三階段sweep結果）：
  contraction_ratio=0.6, min_coil_sec=2.0s, trend_lookback_min=3.0,
  vol_mult=2.5x, move_thresh_pct=0.1%, hold_sec=8s(不變), trail_pct=1.0%(不變)

對照組：(a) 同一份holdout股票上，micro VCP訊號但關掉coil+趨勢濾網（純爆量）；
       (b) 同一份holdout股票上，現行momentum-rotation baseline規格（開盤突破）。
"""

from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import simulate_portfolio_day as baseline_simulate  # noqa: E402
from momentum_rotation_broad_universe_coil_trend_test import (  # noqa: E402
    RANDOM_SEED,
    load_broad_universe,
)
from momentum_rotation_vcp_coil_trend_holdout_cv import simulate_day_coil_trend  # noqa: E402

BEST_PARAMS = dict(
    trail_pct=1.0, preempt_mult=2.0, window_sec=1.0, cooldown_sec=10.0,
    move_thresh_pct=0.15, vol_mult=2.5, hold_sec=8.0,
    require_coil=True, min_coil_sec=3.0, contraction_ratio=0.4,
    require_trend_align=True, trend_lookback_min=3.0,
)
NO_FILTER_PARAMS = dict(
    trail_pct=1.0, preempt_mult=2.0, window_sec=1.0, cooldown_sec=10.0,
    move_thresh_pct=0.15, vol_mult=2.5, hold_sec=8.0,
    require_coil=False, min_coil_sec=3.0, contraction_ratio=1.0,
    require_trend_align=False, trend_lookback_min=3.0,
)
BASELINE_PARAMS = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
                        min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0)
COST_SCENARIOS_BPS = [5, 10, 20, 29]


def _run(universe_subset: dict, sim_fn, **kwargs) -> dict:
    all_days = sorted({d for days in universe_subset.values() for d in days})
    all_trades: list[dict] = []
    per_day: dict[str, float] = {}
    for d in all_days:
        day_data = {code: days[d] for code, days in universe_subset.items() if d in days}
        if len(day_data) < 3:
            continue
        trades = sim_fn(day_data, **kwargs)
        all_trades.extend(trades)
        per_day[d] = per_day.get(d, 0.0) + sum(t["ret_pct"] for t in trades)

    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    day_rets = np.array(list(per_day.values())) if per_day else np.array([])
    n_days = len(day_rets)
    if len(rets) == 0 or n_days == 0:
        return {"n": 0, "n_days": n_days, "risk_adj": float("-inf"), "breakeven_bps": 0.0,
                "win_rate": 0.0, "gross_day_mean": 0.0, "day_std": 0.0}
    breakeven = (rets.sum() / len(rets)) * 100
    win = float(np.mean(rets > 0) * 100)
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    risk_adj = day_mean / day_std if day_std > 0 else float("-inf")
    net_lines = {c: float((rets - c / 100.0).sum() / n_days) for c in COST_SCENARIOS_BPS}
    return {"n": len(rets), "n_days": n_days, "risk_adj": risk_adj, "breakeven_bps": breakeven,
            "win_rate": win, "gross_day_mean": day_mean, "day_std": day_std, "net_by_cost": net_lines}


def _print(name: str, m: dict) -> None:
    if m["n"] == 0:
        print(f"{name}: 無交易")
        return
    net_str = "  ".join(f"{c}bps={v:+.3f}%" for c, v in m.get("net_by_cost", {}).items())
    print(f"{name}: n={m['n']} 天數={m['n_days']} 筆/天={m['n']/m['n_days']:.2f} "
          f"勝率={m['win_rate']:.1f}% 日均(gross)={m['gross_day_mean']:+.3f}% 日std={m['day_std']:.3f}% "
          f"risk-adj={m['risk_adj']:+.3f} 損平={m['breakeven_bps']:.1f}bps")
    print(f"  {net_str}")


def main() -> None:
    print("載入TAIFEX全市場個股期貨archive...")
    universe = load_broad_universe()
    print(f"  {len(universe)}檔通過流動性門檻")

    import random
    codes = sorted(universe.keys())
    rng = random.Random(RANDOM_SEED)
    shuffled = codes[:]
    rng.shuffle(shuffled)
    split = len(shuffled) // 2
    holdout_codes = shuffled[split:]
    holdout_universe = {c: universe[c] for c in holdout_codes}
    print(f"  holdout組{len(holdout_codes)}檔（完全沒看過參數選擇過程，種子{RANDOM_SEED}）\n")

    print("=== A. micro VCP最佳參數（coil+趨勢，三階段sweep結果）===")
    m_best = _run(holdout_universe, simulate_day_coil_trend, **BEST_PARAMS)
    _print("micro VCP(coil+趨勢)", m_best)

    print("\n=== B. 同樣訊號但關掉coil+趨勢濾網（純爆量對照）===")
    m_nofilter = _run(holdout_universe, simulate_day_coil_trend, **NO_FILTER_PARAMS)
    _print("純爆量(無濾網)", m_nofilter)

    print("\n=== C. 現行momentum-rotation baseline規格（開盤突破，同一批holdout股票）===")
    m_baseline = _run(holdout_universe, baseline_simulate, **BASELINE_PARAMS)
    _print("baseline(現行規格)", m_baseline)

    print("\n" + "=" * 90)
    print("=== 總結：命中率轉換成真正損益後 ===")
    print(f"  A. micro VCP(coil+趨勢): 損平={m_best['breakeven_bps']:.1f}bps risk-adj={m_best['risk_adj']:+.3f}")
    print(f"  B. 純爆量(無濾網)      : 損平={m_nofilter['breakeven_bps']:.1f}bps risk-adj={m_nofilter['risk_adj']:+.3f}")
    print(f"  C. baseline(現行規格)  : 損平={m_baseline['breakeven_bps']:.1f}bps risk-adj={m_baseline['risk_adj']:+.3f}")


if __name__ == "__main__":
    main()
