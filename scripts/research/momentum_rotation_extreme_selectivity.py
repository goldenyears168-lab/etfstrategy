"""2026-08-13：使用者要求繼續往「降低換手頻率+拉高品質門檻」推——這裡測更極端
的門檻組合（比momentum_rotation_refine_best.py掃過的範圍更高），加上限制每天
最多進場次數（真正把換手壓到最低），看損平成本能不能推過29bps文件成本上緣。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_rotation_redesign_search import load_window, simulate_day  # noqa: E402

WINDOWS = {
    "IS(漲盤)": "2026-07-13_2026-08-11",
    "OOS(拉回)": "2025-10-20_2025-11-24",
    "W3(盤整)": "2025-11-28_2025-12-19",
    "W4": "2026-02-09_2026-03-06",
}
COST_SCENARIOS_BPS = [5, 10, 20, 29, 40]


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
    return {"name": name, "breakeven_bps": breakeven, "win_rate": win, "n": len(rets)}


def main() -> None:
    print("載入4窗口資料...")
    windows_data = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    base = dict(breakout_pct=0.5, vol_confirm_mult=1.5, rearm_pct=0.25, preempt_mult=2.0, use_preemption=False)
    print()
    print("=== 更極端的品質門檻 ===")
    results = []
    for ov in [0.8, 1.0, 1.5, 2.0]:
        for vr in [3.0, 4.0, 5.0]:
            for tp in [1.0, 1.5, 2.0]:
                r = run_variant(f"ov{ov}_vr{vr}_trail{tp}", windows_data,
                                 **{**base, "min_overshoot_pct": ov, "min_vol_ratio": vr, "trail_pct": tp})
                if r:
                    results.append(r)
    print()
    results.sort(key=lambda r: -r["breakeven_bps"])
    print("=== 損平成本排行前10 ===")
    for r in results[:10]:
        print(f"  {r['name']:36s} 損平={r['breakeven_bps']:.1f}bps 勝率={r['win_rate']:.1f}% n={r['n']}")


if __name__ == "__main__":
    main()
