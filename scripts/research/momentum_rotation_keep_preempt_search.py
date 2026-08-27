"""2026-08-13：使用者明確要求「搶佔還是要的」——不能直接關掉搶佔機制，改測兩個
方向：(a) preempt_mult掃更高的值（3x/5x已測過都比baseline差，繼續往上看8x/10x/
15x會不會有轉折點）、(b) 進場後有一段「最短保護期」內完全不能被搶（不管是不是
還在創新高，單純給一段時間發展），這跟之前測過的「動能新鮮度」（距離上次創新高
多久）不同——這裡是「距離進場多久」，更直接對應「太快被切斷」這個問題本身。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import UNIVERSE, load_day_bars_with_times  # noqa: E402
from momentum_rotation_redesign_search import load_window  # noqa: E402

WINDOWS = {
    "IS(漲盤)": "2026-07-13_2026-08-11",
    "OOS(拉回)": "2025-10-20_2025-11-24",
    "W3(盤整)": "2025-11-28_2025-12-19",
    "W4": "2026-02-09_2026-03-06",
}
COST_SCENARIOS_BPS = [5, 10, 20, 29]


def _parse_t(t: str) -> datetime:
    return datetime.strptime(t, "%Y-%m-%d %H:%M:%S")


def simulate_day(
    stock_day_data: dict,
    *,
    breakout_pct: float, trail_pct: float, vol_confirm_mult: float, rearm_pct: float,
    min_overshoot_pct: float, min_vol_ratio: float, preempt_mult: float,
    min_hold_sec: float,
) -> list[dict]:
    """搶佔機制保留；min_hold_sec：進場後這段時間內完全不能被搶佔（不管新訊號
    分數多高），超過這段時間才恢復照preempt_mult規則判斷。"""
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
            if position["direction"] == "long":
                position["peak_trough"] = max(position["peak_trough"], p)
                stop = position["peak_trough"] * (1 - trail_pct / 100.0)
                hit = p <= stop
            else:
                position["peak_trough"] = min(position["peak_trough"], p)
                stop = position["peak_trough"] * (1 + trail_pct / 100.0)
                hit = p >= stop
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
            "overshoot": overshoot, "vol_ratio": vol_ratio,
        }

        if position is None:
            position = candidate
            armed[sid] = False
        elif score >= preempt_mult * position["entry_score"]:
            held_since = (_parse_t(t) - _parse_t(position["entry_time"])).total_seconds()
            if held_since < min_hold_sec:
                continue  # 還在最短保護期內，不准搶
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


def run_variant(name: str, windows_data: dict, **kwargs) -> dict:
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
        return {}
    gross = rets.sum() / total_days
    win = float(np.mean(rets > 0) * 100)
    net_lines = [f"{c}bps={(rets - c/100.0).sum()/total_days:+.2f}%" for c in COST_SCENARIOS_BPS]
    breakeven = (rets.sum() / len(rets)) * 100
    print(f"{name:40s}: n={len(rets):4d} 筆/天={len(rets)/total_days:5.2f} "
          f"勝率={win:5.1f}% gross日均={gross:+7.3f}% 損平={breakeven:5.1f}bps  " + " ".join(net_lines))
    print(f"  出場原因分布: {reasons}")
    return {"name": name, "breakeven_bps": breakeven, "win_rate": win}


def main() -> None:
    print("載入4窗口資料...")
    windows_data = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    base = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
                min_overshoot_pct=0.15, min_vol_ratio=1.5)
    print()
    print("=== 假說A：preempt_mult掃更高（保留搶佔）===")
    run_variant("preempt_mult=2.0x(現行)", windows_data, **base, preempt_mult=2.0, min_hold_sec=0)
    for pm in [8.0, 10.0, 15.0]:
        run_variant(f"preempt_mult={pm}x", windows_data, **base, preempt_mult=pm, min_hold_sec=0)
    print()
    print("=== 假說B：進場後最短保護期（保留搶佔，不管分數多高都先護住）===")
    for mh in [30, 60, 120, 300]:
        run_variant(f"min_hold={mh}s", windows_data, **base, preempt_mult=2.0, min_hold_sec=float(mh))
    print()
    print("=== 假說C：最短保護期 + 拉高品質門檻（組合）===")
    for mh, ov, vr in [(60, 0.3, 2.0), (120, 0.3, 2.0), (60, 0.5, 2.0), (120, 0.5, 3.0)]:
        run_variant(f"min_hold={mh}s+ov{ov}+vr{vr}", windows_data,
                    **{**base, "preempt_mult": 2.0, "min_hold_sec": float(mh),
                       "min_overshoot_pct": ov, "min_vol_ratio": vr})


if __name__ == "__main__":
    main()
