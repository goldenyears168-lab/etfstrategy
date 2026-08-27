"""2026-08-13：使用者要「最後最好的結論、期望值與標準差」——今天累積下來的
數字散在好幾支腳本、口徑也不一致（有的報單筆std、有的報日std，且早上大部分
候選都還沒套用exit_price也要用真實tick價這個第二個bug修正）。這裡用同一份、
已經套用兩個bug修正（entry=p、exit=p，見momentum_breakout_strategy.py::
simulate_portfolio_day同一處的修正說明）的邏輯，對今天實際比較過的幾個候選
統一重跑，同時報「單筆」跟「日」兩種口徑的期望值/標準差，給一次乾淨、一致、
可信的最終數字。

三個候選：
  1. baseline：現行live規格（config/order.yaml/job_registry.yaml記載的參數）
  2. overshoot[0.5,1.0]：今天在「保留搶佔」前提下找到的最佳momentum方向候選
  3. fade_baseline：使用者提出的反方向假說，純方向反轉、其餘不變

不含retest-entry候選（多agent workflow找到的、今天唯一雙贏但完全沒有holdout
驗證、也還沒重驗exit_price修正後數字的候選）——需要另外找那支腳本重跑，這裡
先給已經完整驗證過的三個。
"""

from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_rotation_redesign_search import COST_SCENARIOS_BPS, WINDOWS, load_window  # noqa: E402


def simulate_day(
    stock_day_data: dict,
    *,
    breakout_pct: float, trail_pct: float, vol_confirm_mult: float, rearm_pct: float,
    min_overshoot_pct: float, max_overshoot_pct: float, min_vol_ratio: float,
    preempt_mult: float, fade: bool,
) -> list[dict]:
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
                exit_price = float(p)  # 修正2：真實tick價出場，不是理論stop
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
        breakout_dir = "long" if price_hits_long else "short"
        direction = ("short" if breakout_dir == "long" else "long") if fade else breakout_dir
        trigger = st["long_trigger"] if breakout_dir == "long" else st["short_trigger"]
        overshoot = abs(p - trigger) / st["open"] * 100.0
        vol_ratio = v / baseline
        if overshoot < min_overshoot_pct or overshoot > max_overshoot_pct or vol_ratio < min_vol_ratio:
            continue
        score = overshoot * vol_ratio
        fill = float(p)  # 修正1：真實tick價進場，不是理論trigger
        candidate = {
            "sid": sid, "direction": direction, "fill": fill, "entry": fill,
            "entry_time": t, "entry_score": score, "peak_trough": fill,
            "overshoot": overshoot, "vol_ratio": vol_ratio,
        }

        if position is None:
            position = candidate
            armed[sid] = False
        elif score >= preempt_mult * position["entry_score"]:
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
    per_day_rets: list[float] = []
    all_trades: list[dict] = []
    for _wname, (all_by_stock, all_days) in windows_data.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            day_trades = simulate_day(day_data, **kwargs)
            all_trades.extend(day_trades)
            per_day_rets.append(sum(t["ret_pct"] for t in day_trades))

    rets = np.array([t["ret_pct"] for t in all_trades])
    day_rets = np.array(per_day_rets)
    n_days = len(day_rets)
    win = float(np.mean(rets > 0) * 100)
    breakeven_bps = (rets.sum() / len(rets)) * 100
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    trade_mean, trade_std = float(rets.mean()), float(rets.std())
    risk_adj = day_mean / day_std if day_std > 0 else float("nan")

    print(f"\n=== {name} ===")
    print(f"  天數={n_days} 筆數={len(rets)} 筆/天={len(rets)/n_days:.2f} 勝率={win:.1f}%")
    print(f"  【單筆】期望值={trade_mean:+.4f}% 標準差={trade_std:.4f}%")
    print(f"  【日】  期望值={day_mean:+.4f}% 標準差={day_std:.4f}%  期望值/標準差(risk-adj)={risk_adj:.3f}")
    print(f"  損平成本={breakeven_bps:.1f}bps（文件真實成本區間5-29bps）")
    net_lines = [f"{c}bps日均={(rets - c/100.0).sum()/n_days:+.3f}%" for c in COST_SCENARIOS_BPS]
    print("  " + "  ".join(net_lines))


def main() -> None:
    print("載入4窗口資料...")
    windows_data = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}

    base = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
                min_overshoot_pct=0.15, max_overshoot_pct=999.0, min_vol_ratio=1.5,
                preempt_mult=2.0, fade=False)

    run_variant("1. baseline（現行live規格，兩個bug都已修正）", windows_data, **base)
    run_variant("2. overshoot[0.5%,1.0%]（今天momentum方向最佳候選）", windows_data,
                **{**base, "min_overshoot_pct": 0.5, "max_overshoot_pct": 1.0})
    run_variant("3. fade_baseline（反方向，使用者提出的假說）", windows_data, **{**base, "fade": True})


if __name__ == "__main__":
    main()
