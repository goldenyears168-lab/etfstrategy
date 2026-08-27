"""2026-08-13：使用者的精確假說——不是「全部反向」（已用75天樣本推翻），是
「進場後極短時間內就被搶佔的部位，幾乎注定虧錢」這個時機本身可以復刻，復刻到
就反向。操作化：正常狀態機完全不變（動能方向、搶佔門檻、trailing stop都不動），
唯一差異——當一次搶佔事件發生，且「被搶佔掉的那個部位」持有時間 < flash_sec
（例如15秒），代表這是連環反應/共同因子噪音（見hold_duration_edge.py：<15s
桶勝率僅10.3%、損平-36.2bps，preempted子集<15s桶損平-7.5bps），這時候「造成
這次快速搶佔的新部位」不是照原方向進場，是整個反過來（long變short/反之），
其餘（trailing stop、之後可能再被搶佔、day_end強平）完全沿用同一套規則。

其他所有正常進場（新鮮進場、或前一個部位撐超過flash_sec才被搶佔）維持原方向
不變——這是最小、最貼近使用者假說的修改，不是重新設計整個策略。
"""

from __future__ import annotations

import sys
from datetime import datetime

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_rotation_redesign_search import COST_SCENARIOS_BPS, WINDOWS, load_window  # noqa: E402


def simulate_day_flash_fade(
    stock_day_data: dict,
    *,
    breakout_pct: float, trail_pct: float, vol_confirm_mult: float, rearm_pct: float,
    min_overshoot_pct: float, min_vol_ratio: float, preempt_mult: float, flash_sec: float,
    fade_hold_sec: float = 0.0,
) -> list[dict]:
    """fade_hold_sec>0時：被反向的部位不套用trailing stop/正常搶佔規則，純粹
    持有固定fade_hold_sec秒後、不管當下賺賠都強制出場（使用者原話「反向幾秒鐘
    就好」——只想抓flash-preempt那個瞬間的立即反彈/回檔，不是開一個要撐到
    trailing stop的正常部位）。fade_hold_sec=0退化成原本「照正常規則跑」的版本。"""
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
    n_flash_fades = 0

    for t, sid, p, v in merged:
        st = meta[sid]
        last_price[sid] = p
        vh = vol_history[sid]
        baseline = max(np.median(vh), 1e-9) if vh else 1.0
        vh.append(v)

        is_held = position is not None and position["sid"] == sid
        if is_held:
            if position.get("flash_faded") and fade_hold_sec > 0:
                elapsed = (datetime.fromisoformat(t) - datetime.fromisoformat(position["entry_time"])).total_seconds()
                if elapsed >= fade_hold_sec:
                    exit_price = float(p)
                    ret_pct = (
                        (exit_price - position["fill"]) / position["fill"] * 100.0
                        if position["direction"] == "long"
                        else (position["fill"] - exit_price) / position["fill"] * 100.0
                    )
                    trades.append({**position, "exit_time": t, "exit": exit_price, "ret_pct": ret_pct, "reason": "fade_timed_exit"})
                    position = None
                    armed[sid] = False
                continue
            if position["direction"] == "long":
                position["peak_trough"] = max(position["peak_trough"], p)
                stop = position["peak_trough"] * (1 - trail_pct / 100.0)
                hit = p <= stop
            else:
                position["peak_trough"] = min(position["peak_trough"], p)
                stop = position["peak_trough"] * (1 + trail_pct / 100.0)
                hit = p >= stop
            if hit:
                exit_price = float(p)
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
        trigger = st["long_trigger"] if breakout_dir == "long" else st["short_trigger"]
        overshoot = abs(p - trigger) / st["open"] * 100.0
        vol_ratio = v / baseline
        if overshoot < min_overshoot_pct or vol_ratio < min_vol_ratio:
            continue
        score = overshoot * vol_ratio
        fill = float(p)

        if position is None:
            # 空手新鮮進場：維持原方向，不反向（使用者假說只針對「造成快速搶佔的
            # 那個新部位」，不是全部訊號）
            candidate = {
                "sid": sid, "direction": breakout_dir, "fill": fill, "entry": fill,
                "entry_time": t, "entry_score": score, "peak_trough": fill,
                "overshoot": overshoot, "vol_ratio": vol_ratio,
            }
            position = candidate
            armed[sid] = False
        elif score >= preempt_mult * position["entry_score"]:
            held_sid = position["sid"]
            held_entry_dt = datetime.fromisoformat(position["entry_time"])
            now_dt = datetime.fromisoformat(t)
            held_duration = (now_dt - held_entry_dt).total_seconds()
            is_flash = held_duration < flash_sec

            exit_price = last_price[held_sid]
            ret_pct = (
                (exit_price - position["fill"]) / position["fill"] * 100.0
                if position["direction"] == "long"
                else (position["fill"] - exit_price) / position["fill"] * 100.0
            )
            trades.append({**position, "exit_time": t, "exit": exit_price, "ret_pct": ret_pct,
                           "reason": "preempted", "held_duration_sec": held_duration})
            armed[held_sid] = False

            new_direction = breakout_dir
            if is_flash:
                new_direction = "short" if breakout_dir == "long" else "long"
                n_flash_fades += 1
            candidate = {
                "sid": sid, "direction": new_direction, "fill": fill, "entry": fill,
                "entry_time": t, "entry_score": score, "peak_trough": fill,
                "overshoot": overshoot, "vol_ratio": vol_ratio, "flash_faded": is_flash,
            }
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
    per_day: dict[str, float] = {}
    total_days = 0
    for _wname, (all_by_stock, all_days) in windows_data.items():
        total_days += len(all_days)
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            day_trades = simulate_day_flash_fade(day_data, **kwargs)
            all_trades.extend(day_trades)
            per_day[d] = per_day.get(d, 0.0) + sum(t["ret_pct"] for t in day_trades)

    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    if len(rets) == 0:
        print(f"{name:40s}: 無交易")
        return {}
    day_rets = np.array(list(per_day.values()))
    win = float(np.mean(rets > 0) * 100)
    breakeven = (rets.sum() / len(rets)) * 100
    gross = rets.sum() / total_days
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    risk_adj = day_mean / day_std if day_std > 0 else float("nan")
    faded_rets = np.array([t["ret_pct"] for t in all_trades if t.get("flash_faded")])
    n_faded = len(faded_rets)
    net_lines = [f"{c}bps={(rets - c/100.0).sum()/total_days:+.2f}%" for c in COST_SCENARIOS_BPS]
    print(f"{name:40s}: n={len(rets):4d} faded={n_faded:3d} 勝率={win:5.1f}% "
          f"gross日均={gross:+7.3f}% 損平={breakeven:5.1f}bps 日std={day_std:.3f}% risk-adj={risk_adj:.3f}  "
          + " ".join(net_lines))
    if n_faded > 0:
        faded_win = float(np.mean(faded_rets > 0) * 100)
        faded_mean = float(faded_rets.mean())
        print(f"{'':40s}  └─ 只看被反向那批(n={n_faded}): 勝率={faded_win:5.1f}% 單筆均值={faded_mean:+.4f}%")
    return {"name": name, "n": len(rets), "n_faded": n_faded, "win_rate": win,
            "breakeven_bps": breakeven, "day_mean": day_mean, "day_std": day_std, "risk_adj": risk_adj}


def main() -> None:
    print("載入4窗口資料...")
    windows_data = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}

    base = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
                min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0)

    print("\n=== 對照組：baseline（不做flash-fade，flash_sec=0等於原規格）===")
    run_variant("baseline(flash_sec=0)", windows_data, **base, flash_sec=0.0)

    print("\n=== 假說1：flash_sec門檻掃描（反向部位沿用正常trailing stop規則）===")
    for fs in [5.0, 10.0, 15.0, 20.0, 30.0, 60.0, 120.0]:
        run_variant(f"flash_fade(<{fs:.0f}s反向,正常規則出場)", windows_data, **base, flash_sec=fs)

    print("\n=== 假說2：使用者精確版——flash_sec=15s固定偵測門檻，反向部位只抓幾秒鐘（fade_hold_sec掃描）===")
    for fh in [2.0, 3.0, 5.0, 8.0, 10.0, 15.0, 20.0, 30.0]:
        run_variant(f"flash15s+fade_hold={fh:.0f}s", windows_data, **base, flash_sec=15.0, fade_hold_sec=fh)


if __name__ == "__main__":
    main()
