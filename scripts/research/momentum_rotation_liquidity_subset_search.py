"""2026-08-13：流動性子集假說——12檔UNIVERSE的Roll(1984)隱含價差成本估計範圍
(5~29bps)可能不是均勻分布，而是「流動性最好的幾檔接近5bps、最差的幾檔接近
29bps」。如果篩出tick密度最高的子集重跑backtest，損平成本是否明顯優於
全12檔混跑的baseline(9.8bps/std0.926%/risk-adj0.449)？

步驟：
  1. 對每檔股票，算4個窗口合併後的日均tick數當流動性代理指標
  2. 排序找出流動性最好的子集（3檔/6檔/8檔）
  3. 只用該子集跑simulate_day（保留搶佔機制，複用momentum_rotation_max_overshoot_cap
     的simulate_day——已是fill=實際tick價的修好版本），套用背景指定的評估標準
  4. 若高流動性子集確實優於全12檔，再疊加overshoot[0.5%,1.0%]濾網做組合測試

不測試關掉搶佔（use_preemption=False）——已被使用者2026-08-13明確排除。

PYTHONPATH=src:scripts/research .venv/bin/python -u \
    scripts/research/momentum_rotation_liquidity_subset_search.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import UNIVERSE  # noqa: E402
from momentum_rotation_max_overshoot_cap import simulate_day  # noqa: E402
from momentum_rotation_redesign_search import load_window  # noqa: E402

WINDOWS = {
    "IS(漲盤)": "2026-07-13_2026-08-11",
    "OOS(拉回)": "2025-10-20_2025-11-24",
    "W3(盤整)": "2025-11-28_2025-12-19",
    "W4": "2026-02-09_2026-03-06",
}
COST_SCENARIOS_BPS = [5, 10, 20, 29]
BASE_PARAMS = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
                    min_overshoot_pct=0.15, max_overshoot_pct=999.0, min_vol_ratio=1.5, preempt_mult=2.0)


def measure_tick_density(windows_data: dict) -> dict[str, dict]:
    """每檔股票：跨4窗口合併的日均tick數、總ticks、總天數."""
    stats: dict[str, dict] = {sid: {"total_ticks": 0, "n_days": 0} for sid in UNIVERSE}
    for _wname, (all_by_stock, _all_days) in windows_data.items():
        for sid, days in all_by_stock.items():
            for _d, (times, _prices, _volumes) in days.items():
                stats[sid]["total_ticks"] += len(times)
                stats[sid]["n_days"] += 1
    for sid, s in stats.items():
        s["avg_ticks_per_day"] = s["total_ticks"] / s["n_days"] if s["n_days"] else 0.0
    return stats


def run_variant(name: str, windows_data: dict, subset: set[str] | None, **kwargs) -> dict:
    all_trades = []
    total_days = 0
    by_day: dict[str, float] = {}
    for wname, (all_by_stock, all_days) in windows_data.items():
        total_days += len(all_days)
        for d in all_days:
            day_data = {
                sid: days[d]
                for sid, days in all_by_stock.items()
                if d in days and (subset is None or sid in subset)
            }
            day_key = f"{wname}|{d}"
            by_day[day_key] = 0.0
            if len(day_data) < 3:
                continue
            day_trades = simulate_day(day_data, **kwargs)
            all_trades.extend(day_trades)
            by_day[day_key] = sum(t["ret_pct"] for t in day_trades)
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    if len(rets) == 0:
        print(f"{name}: 無交易")
        return {}

    day_rets = np.array(list(by_day.values()))

    n = len(rets)
    n_per_day = n / total_days
    ret_mean, ret_std = float(rets.mean()), float(rets.std())
    ret_max_loss, ret_max_win = float(rets.min()), float(rets.max())
    gross_day_mean = rets.sum() / total_days
    day_std = float(day_rets.std()) if len(day_rets) else float("nan")
    day_worst = float(day_rets.min()) if len(day_rets) else float("nan")
    day_best = float(day_rets.max()) if len(day_rets) else float("nan")
    losing_day_frac = float(np.mean(day_rets < 0)) * 100 if len(day_rets) else float("nan")
    risk_adj = gross_day_mean / day_std if day_std else float("nan")
    breakeven_bps = (rets.sum() / n) * 100
    win = float(np.mean(rets > 0) * 100)

    net_lines = [f"{c}bps={(rets - c/100.0).sum()/total_days:+.3f}%" for c in COST_SCENARIOS_BPS]
    print(f"{name:42s}: n={n:4d} 筆/天={n_per_day:5.2f} 勝率={win:5.1f}% "
          f"gross日均={gross_day_mean:+7.3f}% 損平={breakeven_bps:5.1f}bps "
          f"日std={day_std:.3f}% risk-adj={risk_adj:.3f}  " + " ".join(net_lines))
    return {
        "name": name, "n_trades": n, "n_per_day": n_per_day, "win_rate": win,
        "ret_mean": ret_mean, "ret_std": ret_std, "ret_max_loss": ret_max_loss, "ret_max_win": ret_max_win,
        "gross_day_mean": gross_day_mean, "day_std": day_std, "day_worst": day_worst, "day_best": day_best,
        "losing_day_frac": losing_day_frac, "risk_adj": risk_adj, "breakeven_bps": breakeven_bps,
    }


def main() -> None:
    print("載入4窗口資料...")
    windows_data = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}

    print("\n=== 流動性代理指標（4窗口合併日均tick數）===")
    stats = measure_tick_density(windows_data)
    ranked = sorted(stats.items(), key=lambda kv: -kv[1]["avg_ticks_per_day"])
    for sid, s in ranked:
        name = UNIVERSE[sid][1]
        print(f"  {sid} {name:6s} avg_ticks/day={s['avg_ticks_per_day']:8.1f}  "
              f"total_ticks={s['total_ticks']:7d}  n_days={s['n_days']:3d}")

    print("\n=== baseline：全12檔（現行部署設定）===")
    baseline = run_variant("全12檔baseline", windows_data, subset=None, **BASE_PARAMS)

    print("\n=== 流動性子集：只留tick密度最高的N檔 ===")
    subset_results = []
    for n_keep in (3, 6, 8):
        subset_sids = {sid for sid, _s in ranked[:n_keep]}
        names = ",".join(UNIVERSE[s][1] for s in subset_sids)
        r = run_variant(f"top{n_keep}流動性子集({names})", windows_data, subset=subset_sids, **BASE_PARAMS)
        if r:
            r["subset_sids"] = sorted(subset_sids)
            subset_results.append(r)

    print("\n=== 組合：流動性子集 + overshoot[0.5%,1.0%]濾網 ===")
    combo_results = []
    combo_params = {**BASE_PARAMS, "min_overshoot_pct": 0.5, "max_overshoot_pct": 1.0}
    for n_keep in (3, 6, 8):
        subset_sids = {sid for sid, _s in ranked[:n_keep]}
        names = ",".join(UNIVERSE[s][1] for s in subset_sids)
        r = run_variant(f"top{n_keep}流動性+overshoot[0.5,1.0]({names})", windows_data,
                         subset=subset_sids, **combo_params)
        if r:
            combo_results.append(r)

    print("\n=== 彙總比較（baseline: 損平9.8bps / std0.926% / risk-adj0.449）===")
    all_results = [baseline] + subset_results + combo_results
    for r in all_results:
        if not r:
            continue
        print(f"  {r['name']:52s} 損平={r['breakeven_bps']:5.1f}bps 單筆std={r['ret_std']:.3f}% "
              f"日std={r['day_std']:.3f}% risk-adj={r['risk_adj']:.3f} 筆/天={r['n_per_day']:.2f}")


if __name__ == "__main__":
    main()
