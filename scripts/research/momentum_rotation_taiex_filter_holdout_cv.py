"""2026-08-13：今天稍早的TAIEX同向測試（momentum_rotation_taiex_agree_test.py）
是被動觀察「訊號成立後跟TX同向 vs 反向，接下來幾秒的報酬差異」，用短lookback
(2分鐘)、結論是同向沒有比較好，甚至反向略優——但那是訊號層級的觀察，不是
真的把TX方向當「進場關卡」嵌進完整的單槽位輪動狀態機（含搶佔、移動停利）驗證。

使用者看到今天真實案例後追問：如果訊號真的參考大盤方向會不會有幫助？這裡用
更嚴謹的方式重測——直接把TX方向filter嵌進已修好的完整simulate_day（baseline
邏輯：進場/搶佔前，先檢查方向是否跟TX近期trailing_min分鐘趨勢一致，不一致就
跳過這個candidate，不管是新鮮進場還是搶佔候選都適用），4折留一窗口交叉驗證，
掃trailing_min（TX用多長的近期窗口代表"趨勢"）。

TX資料：~/goldenstocks-data/cache/tmf_channel/bars.sqlite，
source='tx_1m_tick_built_582d'，sess='day'，涵蓋全部4個窗口。方向判斷嚴格用
「訊號當下那一分鐘之前已經收盤」的TX bar，避免look-ahead。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import simulate_portfolio_day as baseline_simulate  # noqa: E402
from momentum_rotation_redesign_search import WINDOWS, load_window  # noqa: E402

_DATA_DIR = os.environ.get("GOLDENSTOCKS_DATA_DIR") or os.path.expanduser("~/goldenstocks-data")
_BARS_DB = os.path.join(_DATA_DIR, "cache", "tmf_channel", "bars.sqlite")
BASE = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
            min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0)
TRAILING_MIN_GRID = [2, 5, 10, 15, 20, 30]


def load_taiex_days(days: list[str]) -> dict[str, list[tuple[str, float]]]:
    conn = sqlite3.connect(_BARS_DB)
    out: dict[str, list[tuple[str, float]]] = {}
    try:
        cur = conn.cursor()
        for d in days:
            rows = cur.execute(
                "SELECT t, c FROM bars WHERE source='tx_1m_tick_built_582d' AND sess='day' AND day=? ORDER BY t",
                (d,),
            ).fetchall()
            if rows:
                out[d] = [(t, float(c)) for t, c in rows]
    finally:
        conn.close()
    return out


def _tx_direction(taiex_day: list[tuple[str, float]], hhmm_now: str, lookback_min: int) -> str | None:
    idx_before = None
    for i, (t, _c) in enumerate(taiex_day):
        if t < hhmm_now:
            idx_before = i
        else:
            break
    if idx_before is None or idx_before - lookback_min < 0:
        return None
    close_now = taiex_day[idx_before][1]
    close_prior = taiex_day[idx_before - lookback_min][1]
    if close_now > close_prior:
        return "up"
    if close_now < close_prior:
        return "down"
    return None


def simulate_day_tx_filter(
    stock_day_data: dict, taiex_day: list[tuple[str, float]] | None, *,
    breakout_pct: float, trail_pct: float, vol_confirm_mult: float, rearm_pct: float,
    min_overshoot_pct: float, min_vol_ratio: float, preempt_mult: float, tx_lookback_min: int,
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
        direction = "long" if price_hits_long else "short"
        trigger = st["long_trigger"] if direction == "long" else st["short_trigger"]
        overshoot = abs(p - trigger) / st["open"] * 100.0
        vol_ratio = v / baseline
        if overshoot < min_overshoot_pct or vol_ratio < min_vol_ratio:
            continue

        # TX方向濾網：跟大盤近期趨勢不同向就跳過這個candidate（不管新鮮進場
        # 還是搶佔候選都適用）；沒有TX資料或TX判斷不出方向(平盤)時不濾，
        # 保持baseline行為，避免資料缺失時整段時間都無法交易。
        if taiex_day is not None:
            tx_dir = _tx_direction(taiex_day, t[11:16], tx_lookback_min)
            if tx_dir is not None:
                if (direction == "long" and tx_dir == "down") or (direction == "short" and tx_dir == "up"):
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


def _run(windows_subset: dict, taiex_by_day: dict, sim_fn, use_tx: bool, **kwargs) -> dict:
    all_trades = []
    per_day: dict[str, float] = {}
    for _wname, (all_by_stock, all_days) in windows_subset.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            if use_tx:
                trades = sim_fn(day_data, taiex_by_day.get(d), **kwargs)
            else:
                trades = sim_fn(day_data, **kwargs)
            all_trades.extend(trades)
            per_day[d] = per_day.get(d, 0.0) + sum(t["ret_pct"] for t in trades)
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    day_rets = np.array(list(per_day.values())) if per_day else np.array([])
    if len(rets) == 0 or len(day_rets) == 0 or day_rets.std() == 0:
        return {"n": len(rets), "risk_adj": float("-inf"), "breakeven_bps": 0.0, "win_rate": 0.0}
    breakeven = (rets.sum() / len(rets)) * 100
    win = float(np.mean(rets > 0) * 100)
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    return {"n": len(rets), "risk_adj": day_mean / day_std, "breakeven_bps": breakeven, "win_rate": win}


def main() -> None:
    print("載入4窗口股票資料...")
    all_windows = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    wnames = list(all_windows.keys())
    all_days_needed = sorted({d for _s, days in all_windows.values() for d in days})
    print("載入TX日內1分K...")
    taiex_by_day = load_taiex_days(all_days_needed)
    print(f"  {len(taiex_by_day)}/{len(all_days_needed)} 個交易日有TX資料")

    print("\n### 對照：baseline(無TX濾網) 全4窗口 ###")
    m0 = _run(all_windows, taiex_by_day, baseline_simulate, False, **BASE)
    print(f"  n={m0['n']} 勝率={m0['win_rate']:.1f}% 損平={m0['breakeven_bps']:.1f}bps risk-adj={m0['risk_adj']:+.3f}")

    fold_results = []
    for holdout_name in wnames:
        train_windows = {k: v for k, v in all_windows.items() if k != holdout_name}
        holdout_windows = {holdout_name: all_windows[holdout_name]}
        print(f"\n### Fold: holdout={holdout_name} · train={list(train_windows.keys())} ###")

        best = None
        for tm in TRAILING_MIN_GRID:
            m = _run(train_windows, taiex_by_day, simulate_day_tx_filter, True, **BASE, tx_lookback_min=tm)
            if best is None or m["risk_adj"] > best[1]["risk_adj"]:
                best = (tm, m)
        tm_best, train_m = best
        print(f"  train最佳點: tx_lookback_min={tm_best} (train risk-adj={train_m['risk_adj']:.3f} "
              f"損平={train_m['breakeven_bps']:.1f}bps 勝率={train_m['win_rate']:.1f}%)")

        holdout_new = _run(holdout_windows, taiex_by_day, simulate_day_tx_filter, True, **BASE, tx_lookback_min=tm_best)
        holdout_base = _run(holdout_windows, taiex_by_day, baseline_simulate, False, **BASE)
        print(f"  >>> HOLDOUT({holdout_name}) tx_filter(lookback={tm_best}m): n={holdout_new['n']:4d} "
              f"勝率={holdout_new['win_rate']:5.1f}% 損平={holdout_new['breakeven_bps']:6.1f}bps risk-adj={holdout_new['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) baseline               : n={holdout_base['n']:4d} "
              f"勝率={holdout_base['win_rate']:5.1f}% 損平={holdout_base['breakeven_bps']:6.1f}bps risk-adj={holdout_base['risk_adj']:+.3f}")
        fold_results.append({"holdout": holdout_name, "tm": tm_best, "new": holdout_new, "base": holdout_base})

    print("\n" + "=" * 100)
    print("=== 4折總結 ===")
    n_wins = 0
    for r in fold_results:
        beats = r["new"]["risk_adj"] > r["base"]["risk_adj"]
        n_wins += int(beats)
        print(f"  {r['holdout']:12s} (tx_lookback={r['tm']}m): new risk-adj={r['new']['risk_adj']:+.3f} 損平={r['new']['breakeven_bps']:5.1f}bps  "
              f"vs baseline risk-adj={r['base']['risk_adj']:+.3f} 損平={r['base']['breakeven_bps']:5.1f}bps  {'贏' if beats else '輸'}")
    print(f"\n  4折中有{n_wins}折TX方向濾網版本在holdout上risk-adj優於baseline")


if __name__ == "__main__":
    main()
