"""2026-08-13：使用者精修版——進場後的保護期不是完全禁止搶佔，是「保護期內挑戰者
門檻拉更高，要贏得夠明顯才准搶」，保護期過後才恢復正常的preempt_mult。這是一個
時間遞減式的門檻，不是二元開關。

grace_period_sec：保護期長度；grace_preempt_mult：保護期內生效的門檻（應該遠高於
正常的preempt_mult）；過了grace_period_sec之後才用正常preempt_mult判斷。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, "scripts/research")
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
    grace_period_sec: float, grace_preempt_mult: float,
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
        else:
            held_since = (_parse_t(t) - _parse_t(position["entry_time"])).total_seconds()
            effective_mult = grace_preempt_mult if held_since < grace_period_sec else preempt_mult
            if score >= effective_mult * position["entry_score"]:
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
    print(f"{name:44s}: n={len(rets):4d} 筆/天={len(rets)/total_days:5.2f} "
          f"勝率={win:5.1f}% gross日均={gross:+7.3f}% 損平={breakeven:5.1f}bps  " + " ".join(net_lines))
    print(f"  出場原因分布: {reasons}")
    return {"name": name, "breakeven_bps": breakeven, "win_rate": win}


def main() -> None:
    print("載入4窗口資料...")
    windows_data = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    base = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
                min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0)
    print()
    print("=== 對照組 ===")
    run_variant("baseline(現行, 無保護期)", windows_data, **base, grace_period_sec=0, grace_preempt_mult=2.0)
    print()
    print("=== 保護期內門檻遞增：挑戰者要「贏得夠明顯」才准搶 ===")
    for gp, gm in [(60, 5.0), (60, 10.0), (120, 5.0), (120, 10.0), (300, 5.0), (300, 10.0), (300, 20.0)]:
        run_variant(f"保護期{gp}s內門檻{gm}x", windows_data, **{**base, "grace_period_sec": float(gp), "grace_preempt_mult": gm})
    print()
    print("=== 保護期+拉高品質門檻（組合）===")
    for gp, gm, ov, vr in [(120, 5.0, 0.3, 2.0), (300, 10.0, 0.3, 2.0), (300, 10.0, 0.5, 2.0)]:
        run_variant(f"保護期{gp}s@{gm}x + ov{ov} + vr{vr}", windows_data,
                    **{**base, "grace_period_sec": float(gp), "grace_preempt_mult": gm,
                       "min_overshoot_pct": ov, "min_vol_ratio": vr})


if __name__ == "__main__":
    main()
