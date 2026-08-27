"""2026-08-13：在目前找到最好的組合（no_preemption+品質門檻+移動停利放大）附近
微調，看能不能把損平成本從23.9bps推過29bps的文件成本上緣，讓策略在整個記錄的
成本區間(5~29bps)都站得住。同一套simulate_day骨架（來自momentum_rotation_
redesign_search.py），只是關掉搶佔、掃更細的門檻網格。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import UNIVERSE, load_day_bars_with_times  # noqa: E402
from momentum_rotation_redesign_search import load_window, simulate_day  # noqa: E402

WINDOWS = {
    "IS(漲盤)": "2026-07-13_2026-08-11",
    "OOS(拉回)": "2025-10-20_2025-11-24",
    "W3(盤整)": "2025-11-28_2025-12-19",
    "W4": "2026-02-09_2026-03-06",
}
COST_SCENARIOS_BPS = [5, 10, 20, 29]


def run_variant(name: str, windows_data: dict, **kwargs) -> dict:
    all_trades = []
    total_days = 0
    for _wname, (all_by_stock, all_days) in windows_data.items():
        total_days += len(all_days)
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            all_trades.extend(simulate_day(day_data, **kwargs))
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    if len(rets) == 0:
        print(f"{name}: 無交易")
        return {}
    gross = rets.sum() / total_days
    win = float(np.mean(rets > 0) * 100)
    net_lines = [f"{c}bps={(rets - c/100.0).sum()/total_days:+.2f}%" for c in COST_SCENARIOS_BPS]
    breakeven = (rets.sum() / len(rets)) * 100
    print(f"{name:36s}: n={len(rets):4d} 筆/天={len(rets)/total_days:5.2f} "
          f"勝率={win:5.1f}% gross日均={gross:+7.3f}% 損平={breakeven:5.1f}bps  " + " ".join(net_lines))
    return {"name": name, "breakeven_bps": breakeven, "win_rate": win}


def main() -> None:
    print("載入4窗口資料...")
    windows_data = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    base = dict(breakout_pct=0.5, vol_confirm_mult=1.5, rearm_pct=0.25, preempt_mult=2.0, use_preemption=False)
    print()
    results = []
    for ov in [0.2, 0.3, 0.4, 0.5, 0.7]:
        for vr in [1.5, 2.0, 2.5, 3.0]:
            for tp in [1.0, 1.5, 2.0]:
                r = run_variant(f"ov{ov}_vr{vr}_trail{tp}", windows_data,
                                 **{**base, "min_overshoot_pct": ov, "min_vol_ratio": vr, "trail_pct": tp})
                if r:
                    results.append(r)
    print()
    results.sort(key=lambda r: -r["breakeven_bps"])
    print("=== 損平成本排行前10 ===")
    for r in results[:10]:
        print(f"  {r['name']:36s} 損平={r['breakeven_bps']:.1f}bps 勝率={r['win_rate']:.1f}%")


if __name__ == "__main__":
    main()
