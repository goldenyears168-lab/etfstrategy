"""2026-08-13：全新訊號框架研究——ATR/相對波動度正規化的突破門檻。

背景：現行 breakout_pct=0.5% 是固定值，12檔標的不管平常波動大小一律套用同一把
尺；波動大的股票（例如3374精材、2455全新）平常盤中擺動就大，固定0.5%對它們
太容易假突破，波動小的股票（例如2049川湖）0.5%又太難觸發、常常錯過真正的
延續走勢。

這裡改成：每檔股票的當日突破門檻＝該股「近N個交易日日內波動度」的線性倍數
（k × 過去N天平均(high-low)/open×100%，非未來、只用當天開盤前已知的歷史天數，
causal），波動大的門檻自動變寬、波動小的自動變窄。歷史不足N天的暖身期天數，
退回baseline固定0.5%（不是跳過交易）。

其餘規則完全沿用baseline不動——trail_pct/vol_confirm_mult/rearm_pct/
min_overshoot_pct/min_vol_ratio/preempt_mult 全部照抄
momentum_breakout_strategy.simulate_portfolio_day 的預設值，只把breakout
trigger的算法從「全域固定0.5%」換成「per-stock ATR正規化」，單獨測試這一個
改動的效果。

4折留一窗口交叉驗證：每次留一個窗口當holdout，在其餘3個窗口sweep
(lookback天數 × k倍數)找risk-adj最佳點，套用到holdout驗證一次，4折輪流。
"""

from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import simulate_portfolio_day as baseline_simulate  # noqa: E402
from momentum_rotation_redesign_search import WINDOWS, load_window  # noqa: E402

BASE = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
            min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0)

BASE_BREAKOUT_PCT = 0.5  # 暖身期天數不足時的 fallback（照抄 baseline）
MIN_CLIP_PCT = 0.15  # effective breakout 下限（避免正規化後門檻小到全是雜訊）
MAX_CLIP_PCT = 2.0  # effective breakout 上限（避免正規化後門檻大到永遠不觸發）

ATR_LOOKBACK_GRID = [3, 5, 10]
K_GRID = [0.15, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0]


def build_atr_history(all_by_stock: dict) -> dict[str, dict[str, float]]:
    """sid -> {day: atr_pct}，atr_pct = 當天(high-low)/open×100（單日已實現波動度）."""
    atr_by_sid: dict[str, dict[str, float]] = {}
    for sid, days in all_by_stock.items():
        d_atr: dict[str, float] = {}
        for d, (_times, prices, _volumes) in days.items():
            if prices.size < 2:
                continue
            open_price = float(prices[0])
            if open_price <= 0:
                continue
            d_atr[d] = (float(prices.max()) - float(prices.min())) / open_price * 100.0
        atr_by_sid[sid] = d_atr
    return atr_by_sid


def build_effective_breakout_map(
    all_by_stock: dict, atr_by_sid: dict[str, dict[str, float]], lookback: int, k: float,
) -> dict[str, dict[str, float]]:
    """sid -> {day: effective_breakout_pct}。causal：只用該日之前的lookback天
    atr_pct 平均值 × k；不足lookback天歷史的暖身期天數 fallback 回
    BASE_BREAKOUT_PCT（不是跳過），clip在[MIN_CLIP_PCT, MAX_CLIP_PCT]之間。
    """
    eff: dict[str, dict[str, float]] = {}
    for sid, days in all_by_stock.items():
        sorted_days = sorted(days.keys())
        d_atr = atr_by_sid.get(sid, {})
        eff_sid: dict[str, float] = {}
        for i, d in enumerate(sorted_days):
            prior_days = sorted_days[max(0, i - lookback):i]
            prior_atrs = [d_atr[pd] for pd in prior_days if pd in d_atr]
            if len(prior_atrs) < lookback:
                eff_sid[d] = BASE_BREAKOUT_PCT
            else:
                bp = k * float(np.mean(prior_atrs))
                eff_sid[d] = max(MIN_CLIP_PCT, min(MAX_CLIP_PCT, bp))
        eff[sid] = eff_sid
    return eff


def simulate_day_atr_norm(
    stock_day_data: dict, breakout_pct_by_sid: dict[str, float], *,
    trail_pct: float, vol_confirm_mult: float, rearm_pct: float,
    min_overshoot_pct: float, min_vol_ratio: float, preempt_mult: float,
) -> list[dict]:
    """跟 momentum_breakout_strategy.simulate_portfolio_day 完全一樣的單槽位輪動
    +動態搶佔邏輯（含fill=訊號當下實際tick價的修復），唯一差異：每檔股票的
    breakout trigger 用 breakout_pct_by_sid[sid]（per-stock、per-day ATR正規化
    門檻）取代單一全域 breakout_pct。rearm_pct 仍以此為準（跟該股當天有效門檻
    連動，出場後回到開盤價±rearm_pct內才重新武裝——沿用baseline不變）。
    """
    merged: list[tuple[str, str, float, float]] = []
    meta: dict[str, dict] = {}
    for sid, (times, prices, volumes) in stock_day_data.items():
        if prices.size < 2:
            continue
        bp = breakout_pct_by_sid.get(sid, BASE_BREAKOUT_PCT)
        open_price = float(prices[0])
        meta[sid] = {
            "open": open_price,
            "long_trigger": open_price * (1 + bp / 100.0),
            "short_trigger": open_price * (1 - bp / 100.0),
            "rearm_hi": open_price * (1 + rearm_pct / 100.0),
            "rearm_lo": open_price * (1 - rearm_pct / 100.0),
        }
        for kk in range(1, len(times)):
            merged.append((times[kk], sid, float(prices[kk]), float(volumes[kk])))
    merged.sort(key=lambda x: x[0])

    vol_history: dict[str, list[float]] = {sid: [] for sid in meta}
    armed: dict[str, bool] = {sid: True for sid in meta}
    last_price: dict[str, float] = {sid: meta[sid]["open"] for sid in meta}
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
        score = overshoot * vol_ratio
        fill = float(p)  # 修好的假設：訊號當下實際tick價，不是理論trigger價
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


def _metrics(all_trades: list[dict], per_day: dict[str, float]) -> dict:
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    day_rets = np.array(list(per_day.values())) if per_day else np.array([])
    if len(rets) < 20 or len(day_rets) == 0 or day_rets.std() == 0:
        return {"n": len(rets), "risk_adj": float("-inf"), "breakeven_bps": 0.0, "win_rate": 0.0}
    breakeven = (rets.sum() / len(rets)) * 100
    win = float(np.mean(rets > 0) * 100)
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    return {"n": len(rets), "risk_adj": day_mean / day_std, "breakeven_bps": breakeven, "win_rate": win}


def _run_baseline(windows_subset: dict) -> dict:
    all_trades: list[dict] = []
    per_day: dict[str, float] = {}
    for _wname, (all_by_stock, all_days) in windows_subset.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            trades = baseline_simulate(day_data, **BASE)
            all_trades.extend(trades)
            per_day[d] = per_day.get(d, 0.0) + sum(t["ret_pct"] for t in trades)
    return _metrics(all_trades, per_day)


def _run_atr(windows_subset: dict, lookback: int, k: float) -> dict:
    all_trades: list[dict] = []
    per_day: dict[str, float] = {}
    for _wname, (all_by_stock, all_days) in windows_subset.items():
        atr_by_sid = build_atr_history(all_by_stock)
        eff = build_effective_breakout_map(all_by_stock, atr_by_sid, lookback, k)
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            breakout_map_day = {sid: eff.get(sid, {}).get(d, BASE_BREAKOUT_PCT) for sid in day_data}
            trades = simulate_day_atr_norm(
                day_data, breakout_map_day,
                trail_pct=BASE["trail_pct"], vol_confirm_mult=BASE["vol_confirm_mult"],
                rearm_pct=BASE["rearm_pct"], min_overshoot_pct=BASE["min_overshoot_pct"],
                min_vol_ratio=BASE["min_vol_ratio"], preempt_mult=BASE["preempt_mult"],
            )
            all_trades.extend(trades)
            per_day[d] = per_day.get(d, 0.0) + sum(t["ret_pct"] for t in trades)
    return _metrics(all_trades, per_day)


def main() -> None:
    print("載入4窗口資料...")
    all_windows = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    wnames = list(all_windows.keys())

    print(f"sweep grid: lookback={ATR_LOOKBACK_GRID} x k={K_GRID} "
          f"({len(ATR_LOOKBACK_GRID) * len(K_GRID)}組)，clip=[{MIN_CLIP_PCT}%,{MAX_CLIP_PCT}%]")
    print("=" * 100)

    print("\n### 對照：baseline(固定breakout_pct=0.5%，其餘規格不變) 全4窗口 ###")
    m0 = _run_baseline(all_windows)
    print(f"  n={m0['n']} 勝率={m0['win_rate']:.1f}% 損平={m0['breakeven_bps']:.1f}bps risk-adj={m0['risk_adj']:+.3f}")

    fold_results = []
    for holdout_name in wnames:
        train_windows = {k_: v for k_, v in all_windows.items() if k_ != holdout_name}
        holdout_windows = {holdout_name: all_windows[holdout_name]}
        print(f"\n### Fold: holdout={holdout_name} · train={list(train_windows.keys())} ###")

        best = None
        for lookback in ATR_LOOKBACK_GRID:
            for k in K_GRID:
                m = _run_atr(train_windows, lookback, k)
                if best is None or m["risk_adj"] > best[2]["risk_adj"]:
                    best = (lookback, k, m)
        lookback_best, k_best, train_m = best
        print(f"  train最佳點: lookback={lookback_best}天 k={k_best} "
              f"(train risk-adj={train_m['risk_adj']:.3f} 損平={train_m['breakeven_bps']:.1f}bps "
              f"勝率={train_m['win_rate']:.1f}% n={train_m['n']})")

        holdout_atr = _run_atr(holdout_windows, lookback_best, k_best)
        holdout_base = _run_baseline(holdout_windows)
        print(f"  >>> HOLDOUT({holdout_name}) atr_norm : n={holdout_atr['n']:4d} "
              f"勝率={holdout_atr['win_rate']:5.1f}% 損平={holdout_atr['breakeven_bps']:6.1f}bps "
              f"risk-adj={holdout_atr['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) baseline : n={holdout_base['n']:4d} "
              f"勝率={holdout_base['win_rate']:5.1f}% 損平={holdout_base['breakeven_bps']:6.1f}bps "
              f"risk-adj={holdout_base['risk_adj']:+.3f}")

        fold_results.append({
            "holdout": holdout_name, "lookback": lookback_best, "k": k_best,
            "atr_norm": holdout_atr, "baseline": holdout_base,
        })

    print("\n" + "=" * 100)
    print("=== 4折總結：ATR正規化突破門檻在「沒看過」的窗口上是否穩定優於baseline？ ===")
    n_wins = 0
    for r in fold_results:
        beats = r["atr_norm"]["risk_adj"] > r["baseline"]["risk_adj"]
        n_wins += int(beats)
        print(f"  {r['holdout']:12s} (best lookback={r['lookback']}天/k={r['k']}): "
              f"atr_norm risk-adj={r['atr_norm']['risk_adj']:+.3f} 損平={r['atr_norm']['breakeven_bps']:6.1f}bps  "
              f"vs baseline risk-adj={r['baseline']['risk_adj']:+.3f} 損平={r['baseline']['breakeven_bps']:6.1f}bps  "
              f"{'贏' if beats else '輸'}")
    print(f"\n  4折中有{n_wins}折 ATR正規化突破門檻在holdout上risk-adj優於baseline")


if __name__ == "__main__":
    main()
