"""2026-08-13：精修版假說——不是「有沒有浮盈」（測過，變差），是「動能是不是還在
持續創新高」。持倉的peak_trough如果最近一段時間都沒有再往有利方向推進（動能已經
停滯、進入尾巴），才允許被搶佔；如果剛剛才創新高（動能還活著），不准搶。

用「距離上次創新高過了多久」當staleness指標，掃過幾個門檻（10秒/30秒/60秒）。
跟momentum_rotation_favorable_preempt_check.py同一套simulate_day骨架。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import UNIVERSE, load_day_bars_with_times  # noqa: E402

TICK_DIR = Path("reports/research/expert_pool_futures_tick")
WINDOWS = {
    "IS(漲盤)": "2026-07-13_2026-08-11",
    "OOS(拉回)": "2025-10-20_2025-11-24",
    "W3(盤整)": "2025-11-28_2025-12-19",
    "W4": "2026-02-09_2026-03-06",
}
COST_SCENARIOS_BPS = [5, 10, 20, 29]


def load_window(wdate: str) -> tuple[dict, list[str]]:
    all_by_stock: dict[str, dict] = {}
    for sid in UNIVERSE:
        matches = list(TICK_DIR.glob(f"*{sid}_*{wdate}*.csv"))
        if not matches:
            continue
        days: dict = {}
        for p in matches:
            days.update(load_day_bars_with_times(p))
        all_by_stock[sid] = days
    all_days = sorted(set().union(*[set(d.keys()) for d in all_by_stock.values()]))
    return all_by_stock, all_days


def _parse_t(t: str) -> datetime:
    return datetime.strptime(t, "%Y-%m-%d %H:%M:%S")


def simulate_day(
    stock_day_data: dict,
    *,
    breakout_pct: float, trail_pct: float, vol_confirm_mult: float, rearm_pct: float,
    min_overshoot_pct: float, min_vol_ratio: float, preempt_mult: float,
    staleness_sec: float | None,
) -> list[dict]:
    """staleness_sec=None：現行規則（不看動能新鮮度）。否則：持倉peak_trough
    距離「最近一次創新高」超過staleness_sec秒才允許被搶佔（動能停滯才放行）。"""
    merged: list[tuple] = []
    meta: dict = {}
    for sid, (times, prices, volumes) in stock_day_data.items():
        if prices.size < 2:
            continue
        open_price = float(prices[0])
        meta[sid] = {
            "open": open_price,
            "long_trigger": open_price * (1 + breakout_pct / 100.0),
            "short_trigger": open_price * (1 - breakout_pct / 100.0),
            "rearm_hi": open_price * (1 + rearm_pct / 100.0),
            "rearm_lo": open_price * (1 - rearm_pct / 100.0),
        }
        for k in range(1, len(times)):
            merged.append((times[k], sid, float(prices[k]), float(volumes[k])))
    merged.sort(key=lambda x: x[0])

    vol_history = {sid: [] for sid in meta}
    armed = {sid: True for sid in meta}
    last_price = {sid: meta[sid]["open"] for sid in meta}
    trades: list[dict] = []
    position: dict | None = None

    for t, sid, p, v in merged:
        st = meta[sid]
        last_price[sid] = p
        vh = vol_history[sid]
        baseline = max(np.median(vh), 1e-9) if vh else 1.0
        vh.append(v)

        is_held = position is not None and position["sid"] == sid
        if is_held:
            made_new_peak = False
            if position["direction"] == "long":
                if p > position["peak_trough"]:
                    made_new_peak = True
                position["peak_trough"] = max(position["peak_trough"], p)
                stop = position["peak_trough"] * (1 - trail_pct / 100.0)
                hit = p <= stop
            else:
                if p < position["peak_trough"]:
                    made_new_peak = True
                position["peak_trough"] = min(position["peak_trough"], p)
                stop = position["peak_trough"] * (1 + trail_pct / 100.0)
                hit = p >= stop
            if made_new_peak:
                position["last_peak_time"] = t
            if hit:
                exit_price = stop
                ret_pct = (
                    (exit_price - position["fill"]) / position["fill"] * 100.0
                    if position["direction"] == "long"
                    else (position["fill"] - exit_price) / position["fill"] * 100.0
                )
                trades.append({**position, "exit_time": t, "exit": exit_price, "ret_pct": ret_pct, "reason": "trail_stop"})
                position = None
                armed[sid] = False
            continue

        if not armed[sid]:
            if st["rearm_lo"] <= p <= st["rearm_hi"]:
                armed[sid] = True
            continue

        price_hits_long = p >= st["long_trigger"]
        price_hits_short = p <= st["short_trigger"]
        if not (price_hits_long or price_hits_short) or v < vol_confirm_mult * baseline:
            continue
        direction = "long" if price_hits_long else "short"
        trigger = st["long_trigger"] if direction == "long" else st["short_trigger"]
        overshoot = abs(p - trigger) / st["open"] * 100.0
        vol_ratio = v / baseline
        if overshoot < min_overshoot_pct or vol_ratio < min_vol_ratio:
            continue
        score = overshoot * vol_ratio
        fill = float(p)
        candidate = {
            "sid": sid, "direction": direction, "fill": fill, "entry": fill,
            "entry_time": t, "entry_score": score, "peak_trough": fill,
            "overshoot": overshoot, "vol_ratio": vol_ratio, "last_peak_time": t,
        }

        if position is None:
            position = candidate
            armed[sid] = False
        elif score >= preempt_mult * position["entry_score"]:
            if staleness_sec is not None:
                secs_since_peak = (_parse_t(t) - _parse_t(position["last_peak_time"])).total_seconds()
                if secs_since_peak < staleness_sec:
                    continue  # 動能還新鮮（最近才創新高），不准搶
            held_sid = position["sid"]
            exit_price = last_price[held_sid]
            ret_pct = (
                (exit_price - position["fill"]) / position["fill"] * 100.0
                if position["direction"] == "long"
                else (position["fill"] - exit_price) / position["fill"] * 100.0
            )
            trades.append({**position, "exit_time": t, "exit": exit_price, "ret_pct": ret_pct, "reason": "preempted"})
            armed[held_sid] = False
            position = candidate
            armed[sid] = False

    if position is not None:
        exit_price = last_price[position["sid"]]
        ret_pct = (
            (exit_price - position["fill"]) / position["fill"] * 100.0
            if position["direction"] == "long"
            else (position["fill"] - exit_price) / position["fill"] * 100.0
        )
        trades.append({**position, "exit_time": "day_end", "exit": exit_price, "ret_pct": ret_pct, "reason": "day_end_forced"})
    return trades


def run_variant(name: str, windows_data: dict, **kwargs) -> None:
    all_trades = []
    total_days = 0
    reasons: dict[str, int] = {}
    for _wname, (all_by_stock, all_days) in windows_data.items():
        total_days += len(all_days)
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            for tr in simulate_day(day_data, **kwargs):
                all_trades.append(tr)
                reasons[tr["reason"]] = reasons.get(tr["reason"], 0) + 1
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    if len(rets) == 0:
        print(f"{name}: 無交易")
        return
    gross = rets.sum() / total_days
    win = float(np.mean(rets > 0) * 100)
    net_lines = [f"{c}bps={(rets - c/100.0).sum()/total_days:+.2f}%" for c in COST_SCENARIOS_BPS]
    breakeven = (rets.sum() / len(rets)) * 100
    print(f"{name:26s}: n={len(rets):4d} 筆/天={len(rets)/total_days:5.2f} "
          f"勝率={win:5.1f}% gross日均={gross:+7.3f}% 損平={breakeven:5.1f}bps  " + " ".join(net_lines))
    print(f"  出場原因分布: {reasons}")


def main() -> None:
    print("載入4窗口資料...")
    windows_data = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    base = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
                min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0)
    print()
    run_variant("baseline(現行)", windows_data, **base, staleness_sec=None)
    for s in [10, 30, 60, 120]:
        run_variant(f"staleness>={s}s才准搶", windows_data, **base, staleness_sec=float(s))


if __name__ == "__main__":
    main()
