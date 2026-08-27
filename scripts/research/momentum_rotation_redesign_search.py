"""2026-08-13：在「fill=訊號當下實際tick價」這個已修好的寫實假設下，系統性搜尋
有沒有規格能讓momentum-rotation重新有正期望值（扣掉合理成本後）。

背景：修好simulate_portfolio_day的fill-price bug後，現行規格（12檔、0.5%突破、
1%移動停利、2.0x搶佔）4窗口勝率全部35.7%~43.7%、損益兩平成本僅9.8bps，見
reports/research/momentum_breakout_expert_pool_futures_summary.md開頭章節。

這裡不是重新實作一份獨立邏輯，是複製simulate_portfolio_day的核心(已經是修好的
fill=實際tick價版本)、加兩個結構性開關做對照實驗：
  - use_preemption=False：關掉搶佔，一次進場就抱到trail_stop/day_end，不中途換股
    （假說：搶佔選的是score=overshoot×vol_ratio最高的candidate，overshoot越大
    代表離開盤價越遠——搶佔可能系統性偏好追已經噴最多的標的，這正是今天發現的
    「追高」問題的機制來源）
  - min_overshoot_pct/min_vol_ratio拉高：只吃訊號品質最好的那一小撮，犧牲筆數
    換品質

跟原本的cost_adjusted_reaudit.py一樣，對4個完全獨立窗口跑、套用5~29bps文件記錄
的合理成本區間評分。
"""

from __future__ import annotations

import sys
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


def simulate_day(
    stock_day_data: dict,
    *,
    breakout_pct: float,
    trail_pct: float,
    vol_confirm_mult: float,
    rearm_pct: float,
    min_overshoot_pct: float,
    min_vol_ratio: float,
    preempt_mult: float,
    use_preemption: bool,
) -> list[dict]:
    """fill一律用訊號當下實際tick價（已修好的假設），use_preemption=False時
    空手才找candidate、持倉中完全不比較新訊號（不搶佔），其餘邏輯跟
    simulate_portfolio_day一致。"""
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

        if position is not None and not use_preemption:
            continue  # 持倉中且不搶佔：完全不評估新candidate

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
        elif use_preemption and score >= preempt_mult * position["entry_score"]:
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
    for wname, (all_by_stock, all_days) in windows_data.items():
        total_days += len(all_days)
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            all_trades.extend(simulate_day(day_data, **kwargs))
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    if len(rets) == 0:
        print(f"{name:35s}: 無交易")
        return {"name": name, "n_trades": 0}
    gross = rets.sum() / total_days
    win = float(np.mean(rets > 0) * 100)
    net_lines = []
    for c in COST_SCENARIOS_BPS:
        net = (rets - c / 100.0).sum() / total_days
        net_lines.append(f"{c}bps={net:+.2f}%")
    breakeven = (rets.sum() / len(rets)) * 100
    print(f"{name:35s}: n={len(rets):4d} 筆/天={len(rets)/total_days:5.2f} "
          f"勝率={win:5.1f}% gross日均={gross:+7.3f}% 損平={breakeven:5.1f}bps  " + " ".join(net_lines))
    return {"name": name, "n_trades": len(rets), "win_rate": win, "gross_day_mean": gross, "breakeven_bps": breakeven}


def main() -> None:
    print("載入4窗口資料...")
    windows_data = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    print()

    base = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
                min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0, use_preemption=True)

    print("=== 對照組：現行規格（已修好fill-price）===")
    run_variant("baseline(現行規格)", windows_data, **base)
    print()

    print("=== 假說1：關掉搶佔，一次進場抱到底 ===")
    run_variant("no_preemption", windows_data, **{**base, "use_preemption": False})
    print()

    print("=== 假說2：拉高品質門檻，犧牲筆數換品質 ===")
    for ov, vr in [(0.3, 2.0), (0.5, 2.0), (0.5, 3.0), (1.0, 3.0)]:
        run_variant(f"overshoot>={ov}% vol_ratio>={vr}x", windows_data,
                    **{**base, "min_overshoot_pct": ov, "min_vol_ratio": vr})
    print()

    print("=== 假說3：拉高搶佔門檻，減少換手頻率 ===")
    for pm in [3.0, 5.0]:
        run_variant(f"preempt_mult={pm}x", windows_data, **{**base, "preempt_mult": pm})
    print()

    print("=== 假說4：放大移動停利，改善盈虧比 ===")
    for tp in [1.5, 2.0, 3.0]:
        run_variant(f"trail_pct={tp}%", windows_data, **{**base, "trail_pct": tp})
    print()

    print("=== 假說5：no_preemption + 拉高品質門檻 + 放大移動停利（組合拳） ===")
    for ov, vr, tp in [(0.3, 2.0, 1.5), (0.5, 2.0, 2.0), (0.5, 3.0, 2.0)]:
        run_variant(f"no_preempt+ov{ov}+vr{vr}+trail{tp}", windows_data,
                    **{**base, "use_preemption": False, "min_overshoot_pct": ov,
                       "min_vol_ratio": vr, "trail_pct": tp})


if __name__ == "__main__":
    main()
