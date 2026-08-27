"""2026-08-13：使用者要求真正的20秒TX方向濾網（不是1分鐘），但TX秒級逐筆tick
只有IS(漲盤)/OOS(拉回)兩個窗口有資料（reports/research/expert_pool_futures_tick/
tx_market_TX_tick_*.csv），W3(盤整)/W4沒有——只能在這兩個窗口上驗證，不是完整
4折。誠實標註：這裡只做「留一窗口」的2折（互相holdout，IS訓練驗證OOS、OOS
訓練驗證IS），比4折留一窗口的說服力弱，但至少是真正的20秒解析度。

TX方向：用嚴格早於訊號當下、已經發生的TX tick，比較「最近lookback_sec秒」的
價格變化方向，避免look-ahead。
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import simulate_portfolio_day as baseline_simulate  # noqa: E402
from momentum_rotation_redesign_search import load_window  # noqa: E402

TICK_DIR = Path("reports/research/expert_pool_futures_tick")
WINDOWS_WITH_TX_TICK = {
    "IS(漲盤)": "2026-07-13_2026-08-11",
    "OOS(拉回)": "2025-10-20_2025-11-24",
}
BASE = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
            min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0)
LOOKBACK_SEC_GRID = [10, 15, 20, 30, 45, 60, 90, 120]


def load_tx_tick_day(day: str) -> list[tuple[datetime, float]]:
    f = TICK_DIR / f"tx_market_TX_tick_{day}.csv"
    if not f.is_file():
        return []
    out = []
    with f.open() as fh:
        for row in csv.DictReader(fh):
            if not row.get("price"):
                continue
            try:
                dt = datetime.fromisoformat(row["date"])
                px = float(row["price"])
            except (ValueError, TypeError):
                continue
            out.append((dt, px))
    out.sort(key=lambda x: x[0])
    return out


def _tx_direction_sec(tx_day: list[tuple[datetime, float]], now: datetime, lookback_sec: float) -> str | None:
    idx_before = None
    for i, (dt, _p) in enumerate(tx_day):
        if dt < now:
            idx_before = i
        else:
            break
    if idx_before is None:
        return None
    target = now.timestamp() - lookback_sec
    idx_prior = None
    for i in range(idx_before, -1, -1):
        if tx_day[i][0].timestamp() <= target:
            idx_prior = i
            break
    if idx_prior is None:
        return None
    px_now = tx_day[idx_before][1]
    px_prior = tx_day[idx_prior][1]
    if px_now > px_prior:
        return "up"
    if px_now < px_prior:
        return "down"
    return None


def simulate_day_tx_filter_sec(
    stock_day_data: dict, tx_day: list[tuple[datetime, float]], *,
    breakout_pct: float, trail_pct: float, vol_confirm_mult: float, rearm_pct: float,
    min_overshoot_pct: float, min_vol_ratio: float, preempt_mult: float, lookback_sec: float,
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

        if tx_day:
            tx_dir = _tx_direction_sec(tx_day, datetime.fromisoformat(t), lookback_sec)
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


def _run_filter(all_by_stock: dict, all_days: list, tx_by_day: dict, lookback_sec: float) -> dict:
    all_trades, per_day = [], {}
    for d in all_days:
        day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
        if len(day_data) < 3:
            continue
        trades = simulate_day_tx_filter_sec(day_data, tx_by_day.get(d, []), **BASE, lookback_sec=lookback_sec)
        all_trades.extend(trades)
        per_day[d] = per_day.get(d, 0.0) + sum(t["ret_pct"] for t in trades)
    return _metrics(all_trades, per_day)


def _run_baseline(all_by_stock: dict, all_days: list) -> dict:
    all_trades, per_day = [], {}
    for d in all_days:
        day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
        if len(day_data) < 3:
            continue
        trades = baseline_simulate(day_data, **BASE)
        all_trades.extend(trades)
        per_day[d] = per_day.get(d, 0.0) + sum(t["ret_pct"] for t in trades)
    return _metrics(all_trades, per_day)


def _metrics(all_trades: list, per_day: dict) -> dict:
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    day_rets = np.array(list(per_day.values())) if per_day else np.array([])
    if len(rets) == 0 or len(day_rets) == 0 or day_rets.std() == 0:
        return {"n": len(rets), "risk_adj": float("-inf"), "breakeven_bps": 0.0, "win_rate": 0.0}
    breakeven = (rets.sum() / len(rets)) * 100
    win = float(np.mean(rets > 0) * 100)
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    return {"n": len(rets), "risk_adj": day_mean / day_std, "breakeven_bps": breakeven, "win_rate": win}


def main() -> None:
    print("⚠️ 只有IS/OOS兩窗口有TX秒級tick，這裡只做2折互相holdout，不是4折——")
    print("   說服力比其他今天測過的候選弱，僅供參考。\n")
    print("載入IS/OOS股票資料...")
    win_data = {}
    for wname, wdate in WINDOWS_WITH_TX_TICK.items():
        all_by_stock, all_days = load_window(wdate)
        win_data[wname] = (all_by_stock, all_days)

    print("載入TX秒級tick...")
    tx_by_day = {}
    for wname, (_stk, days) in win_data.items():
        for d in days:
            tx_rows = load_tx_tick_day(d)
            tx_by_day[d] = tx_rows
    n_have = sum(1 for v in tx_by_day.values() if v)
    print(f"  {n_have}/{len(tx_by_day)} 個交易日有TX秒級資料\n")

    for wname, (_stk, days) in win_data.items():
        m = _run_baseline(*win_data[wname])
        print(f"baseline[{wname}]: n={m['n']} 勝率={m['win_rate']:.1f}% 損平={m['breakeven_bps']:.1f}bps risk-adj={m['risk_adj']:+.3f}")
    print()

    wnames = list(WINDOWS_WITH_TX_TICK.keys())
    fold_results = []
    for holdout_name in wnames:
        train_name = [w for w in wnames if w != holdout_name][0]
        print(f"### Fold: holdout={holdout_name} · train={train_name} ###")
        best = None
        for lb in LOOKBACK_SEC_GRID:
            m = _run_filter(*win_data[train_name], tx_by_day, lb)
            if best is None or m["risk_adj"] > best[1]["risk_adj"]:
                best = (lb, m)
        lb_best, train_m = best
        print(f"  train最佳點: lookback_sec={lb_best}s (train risk-adj={train_m['risk_adj']:.3f} 損平={train_m['breakeven_bps']:.1f}bps)")
        holdout_new = _run_filter(*win_data[holdout_name], tx_by_day, lb_best)
        holdout_base = _run_baseline(*win_data[holdout_name])
        print(f"  >>> HOLDOUT({holdout_name}) tx_filter({lb_best}s): n={holdout_new['n']:4d} "
              f"勝率={holdout_new['win_rate']:5.1f}% 損平={holdout_new['breakeven_bps']:6.1f}bps risk-adj={holdout_new['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) baseline       : n={holdout_base['n']:4d} "
              f"勝率={holdout_base['win_rate']:5.1f}% 損平={holdout_base['breakeven_bps']:6.1f}bps risk-adj={holdout_base['risk_adj']:+.3f}\n")
        fold_results.append({"holdout": holdout_name, "lb": lb_best, "new": holdout_new, "base": holdout_base})

    print("=== 2折總結（僅IS/OOS，非完整4折）===")
    n_wins = sum(1 for r in fold_results if r["new"]["risk_adj"] > r["base"]["risk_adj"])
    for r in fold_results:
        beats = r["new"]["risk_adj"] > r["base"]["risk_adj"]
        print(f"  {r['holdout']:12s} (lookback={r['lb']}s): new risk-adj={r['new']['risk_adj']:+.3f} 損平={r['new']['breakeven_bps']:5.1f}bps  "
              f"vs baseline risk-adj={r['base']['risk_adj']:+.3f} 損平={r['base']['breakeven_bps']:5.1f}bps  {'贏' if beats else '輸'}")
    print(f"\n  2折中有{n_wins}折TX秒級濾網版本優於baseline")


if __name__ == "__main__":
    main()
