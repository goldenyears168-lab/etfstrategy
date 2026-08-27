"""2026-08-13：second_scalp_test.py發現一個訊號層級（未管理部位）的現象——
確認門檻越緊（vol_confirm_mult、min_overshoot_pct都拉高），動能延續的edge
從第1秒就是正的（vol3.5x/overshoot0.6%：1秒+1.5bps、10秒+4.0bps），不像
現行門檻要等10秒後才轉正。但那是「訊號本身資訊量」的量測，不是真正可執行的
策略（訊號可重疊、沒有單槽位/資金限制）。

這裡把它做成真正的單槽位輪動策略（搶佔機制保留，比照使用者一貫要求）：
訊號成立門檻拉緊 + 固定持有hold_sec秒後不管賺賠強制出場（比照使用者原話
「賺幾秒鐘就好」），持倉中仍保留一個保護性trailing stop（避免固定持有期間
價格反著噴出去時死撐），也保留搶佔（更強訊號可以提前接手）。

門檻嚴格度×固定持有秒數一起sweep，4折留一窗口交叉驗證（同一次跑內完成，
不分兩階段）。
"""

from __future__ import annotations

import sys
from datetime import datetime

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_rotation_redesign_search import WINDOWS, load_window  # noqa: E402

CONFIRM_TIERS = [
    ("baseline(1.5x/0.15%)", 1.5, 0.15),
    ("緊(2.5x/0.4%)", 2.5, 0.4),
    ("很緊(3.5x/0.6%)", 3.5, 0.6),
    ("極緊(5.0x/1.0%)", 5.0, 1.0),
]
HOLD_SEC_GRID = [10.0, 15.0, 20.0, 30.0]
BASE_FIXED = dict(breakout_pct=0.5, trail_pct=1.0, rearm_pct=0.25, preempt_mult=2.0)


def simulate_day_tight_scalp(
    stock_day_data: dict, *,
    breakout_pct: float, trail_pct: float, rearm_pct: float, preempt_mult: float,
    vol_confirm_mult: float, min_overshoot_pct: float, hold_sec: float,
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
            elapsed = (datetime.fromisoformat(t) - datetime.fromisoformat(position["entry_time"])).total_seconds()
            if position["direction"] == "long":
                position["peak_trough"] = max(position["peak_trough"], p)
                stop = position["peak_trough"] * (1 - trail_pct / 100.0)
                hit = p <= stop
            else:
                position["peak_trough"] = min(position["peak_trough"], p)
                stop = position["peak_trough"] * (1 + trail_pct / 100.0)
                hit = p >= stop
            timed_out = elapsed >= hold_sec
            if hit or timed_out:
                exit_price = float(p)
                ret_pct = (
                    (exit_price - position["fill"]) / position["fill"] * 100.0
                    if position["direction"] == "long"
                    else (position["fill"] - exit_price) / position["fill"] * 100.0
                )
                reason = "trail_stop" if hit else "timed_exit"
                trades.append({**position, "exit_time": t, "exit": exit_price, "ret_pct": ret_pct, "reason": reason})
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
        if overshoot < min_overshoot_pct:
            continue
        vol_ratio = v / baseline
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


def _run(windows_subset: dict, **kwargs) -> dict:
    all_trades = []
    per_day: dict[str, float] = {}
    for _wname, (all_by_stock, all_days) in windows_subset.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            trades = simulate_day_tight_scalp(day_data, **kwargs)
            all_trades.extend(trades)
            per_day[d] = per_day.get(d, 0.0) + sum(t["ret_pct"] for t in trades)
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    day_rets = np.array(list(per_day.values())) if per_day else np.array([])
    if len(rets) < 20 or len(day_rets) == 0 or day_rets.std() == 0:
        return {"n": len(rets), "risk_adj": float("-inf"), "breakeven_bps": 0.0, "win_rate": 0.0}
    breakeven = (rets.sum() / len(rets)) * 100
    win = float(np.mean(rets > 0) * 100)
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    return {"n": len(rets), "risk_adj": day_mean / day_std, "breakeven_bps": breakeven, "win_rate": win}


def main() -> None:
    print("載入4窗口資料...")
    all_windows = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    wnames = list(all_windows.keys())

    print(f"sweep grid: {len(CONFIRM_TIERS)}個門檻 x hold_sec={HOLD_SEC_GRID}")
    print("=" * 100)

    print("\n### 對照：baseline(門檻1.5x/0.15%, trailing stop正常規則, 無固定持有) 全4窗口 ###")
    m0 = _run(all_windows, **BASE_FIXED, vol_confirm_mult=1.5, min_overshoot_pct=0.15, hold_sec=999999.0)
    print(f"  n={m0['n']} 勝率={m0['win_rate']:.1f}% 損平={m0['breakeven_bps']:.1f}bps risk-adj={m0['risk_adj']:+.3f}")

    fold_results = []
    for holdout_name in wnames:
        train_windows = {k: v for k, v in all_windows.items() if k != holdout_name}
        holdout_windows = {holdout_name: all_windows[holdout_name]}
        print(f"\n### Fold: holdout={holdout_name} · train={list(train_windows.keys())} ###")

        best = None
        for tier_name, vc, mo in CONFIRM_TIERS:
            for hs in HOLD_SEC_GRID:
                m = _run(train_windows, **BASE_FIXED, vol_confirm_mult=vc, min_overshoot_pct=mo, hold_sec=hs)
                if best is None or m["risk_adj"] > best[3]["risk_adj"]:
                    best = (tier_name, vc, mo, m, hs)
        tier_name, vc_best, mo_best, train_m, hs_best = best
        print(f"  train最佳點: {tier_name} hold_sec={hs_best}s (train risk-adj={train_m['risk_adj']:.3f} "
              f"損平={train_m['breakeven_bps']:.1f}bps 勝率={train_m['win_rate']:.1f}% n={train_m['n']})")

        holdout_new = _run(holdout_windows, **BASE_FIXED, vol_confirm_mult=vc_best, min_overshoot_pct=mo_best, hold_sec=hs_best)
        holdout_base = _run(holdout_windows, **BASE_FIXED, vol_confirm_mult=1.5, min_overshoot_pct=0.15, hold_sec=999999.0)
        print(f"  >>> HOLDOUT({holdout_name}) tight_scalp: n={holdout_new['n']:4d} "
              f"勝率={holdout_new['win_rate']:5.1f}% 損平={holdout_new['breakeven_bps']:6.1f}bps risk-adj={holdout_new['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) baseline   : n={holdout_base['n']:4d} "
              f"勝率={holdout_base['win_rate']:5.1f}% 損平={holdout_base['breakeven_bps']:6.1f}bps risk-adj={holdout_base['risk_adj']:+.3f}")
        fold_results.append({"holdout": holdout_name, "tier": tier_name, "hs": hs_best, "new": holdout_new, "base": holdout_base})

    print("\n" + "=" * 100)
    print("=== 4折總結 ===")
    n_wins = 0
    for r in fold_results:
        beats = r["new"]["risk_adj"] > r["base"]["risk_adj"]
        n_wins += int(beats)
        print(f"  {r['holdout']:12s} ({r['tier']}, hold={r['hs']}s): new risk-adj={r['new']['risk_adj']:+.3f} 損平={r['new']['breakeven_bps']:5.1f}bps  "
              f"vs baseline risk-adj={r['base']['risk_adj']:+.3f} 損平={r['base']['breakeven_bps']:5.1f}bps  {'贏' if beats else '輸'}")
    print(f"\n  4折中有{n_wins}折tight_scalp版本在holdout上risk-adj優於baseline")


if __name__ == "__main__":
    main()
