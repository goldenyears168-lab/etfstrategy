"""2026-08-14：把多波收縮VCP（真正符合Minervini定義：連續n_waves個子區間震幅
+量能遞減，見momentum_rotation_multiwave_vcp_test.py，holdout命中率33.0%
vs對照30.9%，n=203、train->holdout只掉1.2個百分點，是今天最穩的版本）包進
完整交易模擬算真正的損平bps。

順便處理一個到目前都沒處理過的盲點：trail_pct=1.0%這個停損寬度，一整天都是
直接沿用開盤突破策略的原始設定，從沒針對「8秒固定短持有」這個完全不同的
時間尺度重新校準過。這裡在TRAIN組上先掃trail_pct，找到更適合這個時間尺度
的停損寬度，才套到HOLDOUT組算最終損平bps——不是繼續沿用一個沒驗證過的
预設值。
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import simulate_portfolio_day as baseline_simulate  # noqa: E402
from momentum_rotation_broad_universe_coil_trend_test import (  # noqa: E402
    CONTINUATION_SEC,
    MOVE_THRESH_PCT,
    RANDOM_SEED,
    TREND_LOOKBACK_MIN,
    VOL_MULT,
    WINDOW_SEC,
    load_broad_universe,
)

BEST_N_WAVES = 2
BEST_COIL_LOOKBACK_SEC = 12.0
BEST_WAVE_CONTRACTION_RATIO = 0.5
MIN_TICKS_PER_BUCKET = 3
HOLD_SEC = 8.0
PREEMPT_MULT = 2.0
COOLDOWN_SEC = 10.0
TRAIL_PCT_GRID = [0.3, 0.5, 0.75, 1.0, 1.5, 2.0]
COST_SCENARIOS_BPS = [5, 10, 20, 29]
BASELINE_PARAMS = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
                        min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0)


def simulate_day_multiwave(
    stock_day_data: dict, *,
    trail_pct: float, preempt_mult: float, window_sec: float, cooldown_sec: float,
    move_thresh_pct: float, vol_mult: float, hold_sec: float,
    n_waves: int, coil_lookback_sec: float, wave_contraction_ratio: float,
    trend_lookback_min: float,
) -> list[dict]:
    merged: list[tuple] = []
    for sid, (times, prices, volumes) in stock_day_data.items():
        for k in range(len(times)):
            merged.append((datetime.fromisoformat(times[k]), sid, float(prices[k]), float(volumes[k])))
    merged.sort(key=lambda x: x[0])

    buf: dict[str, list[tuple]] = {sid: [] for sid in stock_day_data}
    coil_buf: dict[str, list[tuple]] = {sid: [] for sid in stock_day_data}
    trend_buf: dict[str, list[tuple]] = {sid: [] for sid in stock_day_data}
    vol_hist: dict[str, list[float]] = {sid: [] for sid in stock_day_data}
    last_signal_dt: dict[str, datetime | None] = {sid: None for sid in stock_day_data}
    last_price: dict[str, float] = {sid: 0.0 for sid in stock_day_data}

    trades: list[dict] = []
    position: dict | None = None
    win_td = timedelta(seconds=window_sec)
    coil_td = timedelta(seconds=coil_lookback_sec)
    trend_td = timedelta(seconds=trend_lookback_min * 60.0)
    cool_td = timedelta(seconds=cooldown_sec)
    bucket_sec = coil_lookback_sec / n_waves

    def _is_multiwave_coiled(sid: str, t: datetime) -> bool:
        cb = coil_buf[sid]
        if len(cb) < n_waves * MIN_TICKS_PER_BUCKET:
            return False
        bucket_start = t - timedelta(seconds=coil_lookback_sec)
        buckets: list[list[tuple]] = [[] for _ in range(n_waves)]
        for row in cb:
            offset = (row[0] - bucket_start).total_seconds()
            idx = int(offset // bucket_sec)
            if 0 <= idx < n_waves:
                buckets[idx].append(row)
        if any(len(b) < MIN_TICKS_PER_BUCKET for b in buckets):
            return False
        ranges = [max(r[1] for r in b) - min(r[1] for r in b) for b in buckets]
        vols = [sum(r[2] for r in b) for b in buckets]
        if any(r <= 0 for r in ranges) or any(vv <= 0 for vv in vols):
            return False
        return all(
            ranges[i] <= ranges[i - 1] * wave_contraction_ratio
            and vols[i] <= vols[i - 1] * wave_contraction_ratio
            for i in range(1, n_waves)
        )

    def _trend_direction(sid: str) -> str | None:
        tb = trend_buf[sid]
        if len(tb) < 2:
            return None
        span_sec = (tb[-1][0] - tb[0][0]).total_seconds()
        if span_sec < trend_lookback_min * 60.0 * 0.5:
            return None
        oldest_p, newest_p = tb[0][1], tb[-1][1]
        if newest_p > oldest_p:
            return "long"
        if newest_p < oldest_p:
            return "short"
        return None

    for t, sid, p, v in merged:
        last_price[sid] = p
        b = buf[sid]
        b.append((t, p, v))
        while b and (t - b[0][0]) > win_td:
            b.pop(0)
        cb = coil_buf[sid]
        cb.append((t, p, v))
        while cb and (t - cb[0][0]) > coil_td:
            cb.pop(0)
        tb = trend_buf[sid]
        tb.append((t, p, v))
        while tb and (t - tb[0][0]) > trend_td:
            tb.pop(0)

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
            elapsed = (t - datetime.fromisoformat(position["entry_time"])).total_seconds()
            timed_out = elapsed >= hold_sec
            if hit or timed_out:
                exit_price = float(p)
                ret_pct = (
                    (exit_price - position["fill"]) / position["fill"] * 100.0
                    if position["direction"] == "long"
                    else (position["fill"] - exit_price) / position["fill"] * 100.0
                )
                reason = "trail_stop" if hit else "timed_exit"
                trades.append({**position, "exit_time": t.isoformat(), "exit": exit_price, "ret_pct": ret_pct, "reason": reason})
                position = None
            continue

        vh = vol_hist[sid]
        window_vol_sum = sum(row[2] for row in b)
        if len(b) < 2:
            vh.append(window_vol_sum)
            continue
        baseline = max(np.median(vh), 1e-9) if vh else 1.0
        vh.append(window_vol_sum)

        if last_signal_dt[sid] is not None and (t - last_signal_dt[sid]) < cool_td:
            continue
        oldest_p = b[0][1]
        if oldest_p <= 0:
            continue
        price_move_pct = (p - oldest_p) / oldest_p * 100.0
        vol_burst = window_vol_sum / baseline
        if abs(price_move_pct) < move_thresh_pct or vol_burst < vol_mult:
            continue
        if not _is_multiwave_coiled(sid, t):
            continue

        direction = "long" if price_move_pct > 0 else "short"
        trend_dir = _trend_direction(sid)
        if trend_dir is None or trend_dir != direction:
            continue

        last_signal_dt[sid] = t
        score = abs(price_move_pct) * vol_burst
        fill = float(p)
        candidate = {
            "sid": sid, "direction": direction, "fill": fill, "entry": fill,
            "entry_time": t.isoformat(), "entry_score": score, "peak_trough": fill,
        }

        if position is None:
            position = candidate
        elif score >= preempt_mult * position["entry_score"]:
            held_sid = position["sid"]
            exit_price = last_price[held_sid]
            ret_pct = (
                (exit_price - position["fill"]) / position["fill"] * 100.0
                if position["direction"] == "long"
                else (position["fill"] - exit_price) / position["fill"] * 100.0
            )
            trades.append({**position, "exit_time": t.isoformat(), "exit": exit_price, "ret_pct": ret_pct, "reason": "preempted"})
            position = candidate

    if position is not None:
        exit_price = last_price[position["sid"]]
        ret_pct = (
            (exit_price - position["fill"]) / position["fill"] * 100.0
            if position["direction"] == "long"
            else (position["fill"] - exit_price) / position["fill"] * 100.0
        )
        trades.append({**position, "exit_time": "day_end", "exit": exit_price, "ret_pct": ret_pct, "reason": "day_end_forced"})
    return trades


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
                "win_rate": 0.0, "gross_day_mean": 0.0, "day_std": 0.0, "net_by_cost": {}}
    breakeven = (rets.sum() / len(rets)) * 100
    win = float(np.mean(rets > 0) * 100)
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    risk_adj = day_mean / day_std if day_std > 0 else float("-inf")
    net_by_cost = {c: float((rets - c / 100.0).sum() / n_days) for c in COST_SCENARIOS_BPS}
    return {"n": len(rets), "n_days": n_days, "risk_adj": risk_adj, "breakeven_bps": breakeven,
            "win_rate": win, "gross_day_mean": day_mean, "day_std": day_std, "net_by_cost": net_by_cost}


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

    codes = sorted(universe.keys())
    rng = random.Random(RANDOM_SEED)
    shuffled = codes[:]
    rng.shuffle(shuffled)
    split = len(shuffled) // 2
    train_codes, holdout_codes = shuffled[:split], shuffled[split:]
    train_universe = {c: universe[c] for c in train_codes}
    holdout_universe = {c: universe[c] for c in holdout_codes}
    print(f"  train組{len(train_codes)}檔 / holdout組{len(holdout_codes)}檔（種子{RANDOM_SEED}）\n")

    fixed = dict(preempt_mult=PREEMPT_MULT, window_sec=WINDOW_SEC, cooldown_sec=COOLDOWN_SEC,
                 move_thresh_pct=MOVE_THRESH_PCT, vol_mult=VOL_MULT, hold_sec=HOLD_SEC,
                 n_waves=BEST_N_WAVES, coil_lookback_sec=BEST_COIL_LOOKBACK_SEC,
                 wave_contraction_ratio=BEST_WAVE_CONTRACTION_RATIO, trend_lookback_min=TREND_LOOKBACK_MIN)

    print(f"=== TRAIN組：trail_pct掃描 {TRAIL_PCT_GRID} ===")
    best = None
    for tp in TRAIL_PCT_GRID:
        m = _run(train_universe, simulate_day_multiwave, trail_pct=tp, **fixed)
        flag = ""
        if m["n"] >= 15 and (best is None or m["risk_adj"] > best[1]["risk_adj"]):
            best = (tp, m)
            flag = " <- 目前最佳"
        print(f"  trail_pct={tp}%: ", end="")
        _print("", m)
        if flag:
            print(f" {flag}")

    if best is None:
        print("找不到樣本數足夠的trail_pct，用預設1.0%")
        tp_best = 1.0
    else:
        tp_best = best[0]
    print(f"\nTRAIN最佳 trail_pct={tp_best}%\n")

    print("=" * 90)
    print(f"=== HOLDOUT組驗證（完全沒看過的{len(holdout_codes)}檔，只套用一次，trail_pct={tp_best}%）===")
    m_multiwave = _run(holdout_universe, simulate_day_multiwave, trail_pct=tp_best, **fixed)
    _print("A. 多波VCP(coil+趨勢, 校準過trail_pct)", m_multiwave)

    print("\n=== 對照：同樣多波VCP但trail_pct維持原始1.0%(沒校準) ===")
    m_multiwave_orig = _run(holdout_universe, simulate_day_multiwave, trail_pct=1.0, **fixed)
    _print("A'. 多波VCP(trail_pct=1.0%未校準)", m_multiwave_orig)

    print("\n=== C. baseline(現行momentum-rotation規格，同一批holdout股票) ===")
    m_baseline = _run(holdout_universe, baseline_simulate, **BASELINE_PARAMS)
    _print("baseline(現行規格)", m_baseline)

    print("\n" + "=" * 90)
    print("=== 總結 ===")
    print(f"  A. 多波VCP(trail校準後): 損平={m_multiwave['breakeven_bps']:.1f}bps risk-adj={m_multiwave['risk_adj']:+.3f}")
    print(f"  A'. 多波VCP(trail未校準): 損平={m_multiwave_orig['breakeven_bps']:.1f}bps risk-adj={m_multiwave_orig['risk_adj']:+.3f}")
    print(f"  C. baseline(現行規格)  : 損平={m_baseline['breakeven_bps']:.1f}bps risk-adj={m_baseline['risk_adj']:+.3f}")


if __name__ == "__main__":
    main()
