"""2026-08-13：tight-scalp（vol_confirm_mult=2.5x/min_overshoot_pct=0.4%，固定持有
hold_sec秒不管賺賠出場、期間保留trail_pct=1.0%保護性停損）目前4折3勝1負，但贏的
窗口損平只有0.3~4.8bps，還在成本估計以下。這裡只變「出場機制」本身，訊號門檻鎖死
在代表點，測試三種出場機制變體，看有沒有哪個能把損平往下壓、risk-adj往上推：

  (a) 移除trailing stop，純粹只用固定秒數出場（hold_sec本身當唯一出場條件）
  (b) 加上固定停利目標profit_target_pct，價格達到就提前出場（不用等hold_sec），
      同時保留原本的hold_sec + trail_pct保護
  (c) trail_pct本身一起sweep（0.3%~2.0%），看有沒有比現行1.0%更好的組合

三種變體合併成同一個train sweep grid（同一個「出場機制」研究主題），4折留一窗口
交叉驗證：每折用其餘3窗口對整個grid（a/b/c全部選項）依risk-adj排序選最佳點，
只套用到holdout那一折驗證一次。baseline＝現行live規格，直接呼叫
momentum_breakout_strategy.simulate_portfolio_day（SSOT，entry/exit都已修好用真實
tick價）。

fill/exit都用訊號或觸發當下的真實tick價float(p)，不用理論trigger/stop值——
這是2026-08-13已經修過兩次的bug，這支腳本從頭就直接照抄SSOT的做法。
"""

from __future__ import annotations

import sys
from datetime import datetime

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import simulate_portfolio_day  # noqa: E402
from momentum_rotation_redesign_search import WINDOWS, load_window  # noqa: E402

# 訊號門檻鎖死在今天找到的tight-scalp代表點
SIGNAL_FIXED = dict(vol_confirm_mult=2.5, min_overshoot_pct=0.4)
BASE_FIXED = dict(breakout_pct=0.5, rearm_pct=0.25, preempt_mult=2.0)
HOLD_SEC_GRID = [10.0, 15.0, 20.0, 30.0]
TRAIL_PCT_GRID = [0.3, 0.5, 0.75, 1.0, 1.5, 2.0]
PROFIT_TARGET_GRID = [0.2, 0.3, 0.5, 0.75, 1.0]

BASELINE_KW = dict(
    breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
    min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0,
)


def build_grid() -> list[dict]:
    """組合出 (a)/(b)/(c) 三種出場機制變體的合併sweep grid。
    每個元素：{"variant": "a"/"b"/"c", "hold_sec":, "use_trail":, "trail_pct":,
    "profit_target_pct": None or float}
    """
    grid: list[dict] = []
    # (a) 無trailing stop，純固定秒數出場
    for hs in HOLD_SEC_GRID:
        grid.append({
            "variant": "a_hold_only", "hold_sec": hs, "use_trail": False,
            "trail_pct": None, "profit_target_pct": None,
        })
    # (b) 固定 trail_pct=1.0%（現行值）+ profit_target_pct 提前出場
    for hs in HOLD_SEC_GRID:
        for pt in PROFIT_TARGET_GRID:
            grid.append({
                "variant": "b_profit_target", "hold_sec": hs, "use_trail": True,
                "trail_pct": 1.0, "profit_target_pct": pt,
            })
    # (c) trail_pct本身sweep，無profit target
    for hs in HOLD_SEC_GRID:
        for tp in TRAIL_PCT_GRID:
            grid.append({
                "variant": "c_trail_sweep", "hold_sec": hs, "use_trail": True,
                "trail_pct": tp, "profit_target_pct": None,
            })
    return grid


def build_day_cache(stock_day_data: dict, *, breakout_pct: float, rearm_pct: float) -> tuple[list, dict]:
    """訊號門檻(breakout_pct/rearm_pct)與volume baseline都跟出場機制config無關，
    每個config重跑一次merge+sort+median是純浪費（12分鐘還跑不完一折的根因）。
    這裡把「merged tick序列 + 每筆tick當下的volume baseline」預先算好、快取，
    後面48個出場機制config共用同一份，只重跑position management那段。
    """
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

    vol_history: dict[str, list[float]] = {sid: [] for sid in meta}
    merged_with_baseline: list[tuple] = []
    for t, sid, p, v in merged:
        vh = vol_history[sid]
        baseline = max(np.median(vh), 1e-9) if vh else 1.0
        vh.append(v)
        merged_with_baseline.append((t, sid, p, v, baseline))
    return merged_with_baseline, meta


def simulate_day_exit_variant(
    merged_with_baseline: list, meta: dict, *,
    preempt_mult: float,
    vol_confirm_mult: float, min_overshoot_pct: float,
    hold_sec: float, use_trail: bool, trail_pct: float | None,
    profit_target_pct: float | None,
) -> list[dict]:
    armed = {sid: True for sid in meta}
    last_price = {sid: meta[sid]["open"] for sid in meta}
    trades: list[dict] = []
    position: dict | None = None

    for t, sid, p, v, baseline in merged_with_baseline:
        st = meta[sid]
        last_price[sid] = p

        is_held = position is not None and position["sid"] == sid
        if is_held:
            elapsed = (datetime.fromisoformat(t) - datetime.fromisoformat(position["entry_time"])).total_seconds()
            hit_trail = False
            if use_trail:
                if position["direction"] == "long":
                    position["peak_trough"] = max(position["peak_trough"], p)
                    stop = position["peak_trough"] * (1 - trail_pct / 100.0)
                    hit_trail = p <= stop
                else:
                    position["peak_trough"] = min(position["peak_trough"], p)
                    stop = position["peak_trough"] * (1 + trail_pct / 100.0)
                    hit_trail = p >= stop

            hit_profit = False
            if profit_target_pct is not None:
                if position["direction"] == "long":
                    move_pct = (p - position["fill"]) / position["fill"] * 100.0
                else:
                    move_pct = (position["fill"] - p) / position["fill"] * 100.0
                hit_profit = move_pct >= profit_target_pct

            timed_out = elapsed >= hold_sec

            if hit_trail or hit_profit or timed_out:
                exit_price = float(p)
                ret_pct = (
                    (exit_price - position["fill"]) / position["fill"] * 100.0
                    if position["direction"] == "long"
                    else (position["fill"] - exit_price) / position["fill"] * 100.0
                )
                if hit_trail:
                    reason = "trail_stop"
                elif hit_profit:
                    reason = "profit_target"
                else:
                    reason = "timed_exit"
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


def _metrics(all_trades: list[dict], per_day: dict[str, float]) -> dict:
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    day_rets = np.array(list(per_day.values())) if per_day else np.array([])
    if len(rets) < 20 or len(day_rets) == 0 or day_rets.std() == 0:
        return {"n": len(rets), "risk_adj": float("-inf"), "breakeven_bps": 0.0, "win_rate": 0.0}
    breakeven = (rets.sum() / len(rets)) * 100
    win = float(np.mean(rets > 0) * 100)
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    return {"n": len(rets), "risk_adj": day_mean / day_std, "breakeven_bps": breakeven, "win_rate": win}


def _build_caches(windows_subset: dict) -> list[tuple[str, list, dict]]:
    caches: list[tuple[str, list, dict]] = []
    for _wname, (all_by_stock, all_days) in windows_subset.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            merged_with_baseline, meta = build_day_cache(
                day_data, breakout_pct=BASE_FIXED["breakout_pct"], rearm_pct=BASE_FIXED["rearm_pct"],
            )
            caches.append((d, merged_with_baseline, meta))
    return caches


def _run_variant_cached(day_caches: list[tuple[str, list, dict]], cfg: dict) -> dict:
    all_trades: list[dict] = []
    per_day: dict[str, float] = {}
    for d, merged_with_baseline, meta in day_caches:
        trades = simulate_day_exit_variant(
            merged_with_baseline, meta,
            preempt_mult=BASE_FIXED["preempt_mult"], **SIGNAL_FIXED,
            hold_sec=cfg["hold_sec"], use_trail=cfg["use_trail"],
            trail_pct=cfg["trail_pct"], profit_target_pct=cfg["profit_target_pct"],
        )
        all_trades.extend(trades)
        per_day[d] = per_day.get(d, 0.0) + sum(t["ret_pct"] for t in trades)
    return _metrics(all_trades, per_day)


def _run_variant(windows_subset: dict, cfg: dict) -> dict:
    return _run_variant_cached(_build_caches(windows_subset), cfg)


def _run_baseline(windows_subset: dict) -> dict:
    all_trades: list[dict] = []
    per_day: dict[str, float] = {}
    for _wname, (all_by_stock, all_days) in windows_subset.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            trades = simulate_portfolio_day(day_data, **BASELINE_KW)
            all_trades.extend(trades)
            per_day[d] = per_day.get(d, 0.0) + sum(t["ret_pct"] for t in trades)
    return _metrics(all_trades, per_day)


def cfg_label(cfg: dict) -> str:
    if cfg["variant"] == "a_hold_only":
        return f"a_hold_only(hold={cfg['hold_sec']}s,no_trail)"
    if cfg["variant"] == "b_profit_target":
        return f"b_profit_target(hold={cfg['hold_sec']}s,trail=1.0%,pt={cfg['profit_target_pct']}%)"
    return f"c_trail_sweep(hold={cfg['hold_sec']}s,trail={cfg['trail_pct']}%)"


def main() -> None:
    print("載入4窗口資料...")
    all_windows = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    wnames = list(all_windows.keys())
    grid = build_grid()
    print(f"合併grid大小: {len(grid)} 個出場機制組合 (a={len(HOLD_SEC_GRID)}, "
          f"b={len(HOLD_SEC_GRID) * len(PROFIT_TARGET_GRID)}, c={len(HOLD_SEC_GRID) * len(TRAIL_PCT_GRID)})")
    print("=" * 100)

    ref_cfg = {"variant": "ref", "hold_sec": 10.0, "use_trail": True, "trail_pct": 1.0, "profit_target_pct": None}

    print("\n預先建立每個窗口的tick快取（merge+sort+volume baseline，跟出場機制config無關，只算一次）...")
    window_caches = {wname: _build_caches({wname: all_windows[wname]}) for wname in wnames}
    all_caches = [c for cs in window_caches.values() for c in cs]

    print("\n### 對照：現行tight-scalp代表點(hold=10s, trail=1.0%固定, 無profit target) 全4窗口 ###")
    m_tight_full = _run_variant_cached(all_caches, ref_cfg)
    print(f"  n={m_tight_full['n']} 勝率={m_tight_full['win_rate']:.1f}% "
          f"損平={m_tight_full['breakeven_bps']:.1f}bps risk-adj={m_tight_full['risk_adj']:+.3f}")

    print("\n### 對照：baseline(live現行規格) 全4窗口 ###")
    m0 = _run_baseline(all_windows)
    print(f"  n={m0['n']} 勝率={m0['win_rate']:.1f}% 損平={m0['breakeven_bps']:.1f}bps risk-adj={m0['risk_adj']:+.3f}")

    fold_results = []
    for holdout_name in wnames:
        train_windows = {k: v for k, v in all_windows.items() if k != holdout_name}
        holdout_windows = {holdout_name: all_windows[holdout_name]}
        train_caches = [c for wn in train_windows for c in window_caches[wn]]
        holdout_caches = window_caches[holdout_name]
        print(f"\n### Fold: holdout={holdout_name} · train={list(train_windows.keys())} ###")

        best = None
        for cfg in grid:
            m = _run_variant_cached(train_caches, cfg)
            if best is None or m["risk_adj"] > best[1]["risk_adj"]:
                best = (cfg, m)
        best_cfg, train_m = best
        print(f"  train最佳點: {cfg_label(best_cfg)} (train risk-adj={train_m['risk_adj']:.3f} "
              f"損平={train_m['breakeven_bps']:.1f}bps 勝率={train_m['win_rate']:.1f}% n={train_m['n']})")

        holdout_new = _run_variant_cached(holdout_caches, best_cfg)
        holdout_base = _run_baseline(holdout_windows)
        holdout_tight_ref = _run_variant_cached(holdout_caches, ref_cfg)
        print(f"  >>> HOLDOUT({holdout_name}) 新出場機制: n={holdout_new['n']:4d} "
              f"勝率={holdout_new['win_rate']:5.1f}% 損平={holdout_new['breakeven_bps']:6.1f}bps risk-adj={holdout_new['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) tight-scalp現行出場: n={holdout_tight_ref['n']:4d} "
              f"勝率={holdout_tight_ref['win_rate']:5.1f}% 損平={holdout_tight_ref['breakeven_bps']:6.1f}bps risk-adj={holdout_tight_ref['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) baseline(live): n={holdout_base['n']:4d} "
              f"勝率={holdout_base['win_rate']:5.1f}% 損平={holdout_base['breakeven_bps']:6.1f}bps risk-adj={holdout_base['risk_adj']:+.3f}")
        fold_results.append({
            "holdout": holdout_name, "cfg": best_cfg, "cfg_label": cfg_label(best_cfg),
            "new": holdout_new, "tight_ref": holdout_tight_ref, "base": holdout_base,
        })

    print("\n" + "=" * 100)
    print("=== 4折總結（新出場機制 vs baseline(live)）===")
    n_wins_vs_base = 0
    n_wins_vs_tight = 0
    for r in fold_results:
        beats_base = r["new"]["risk_adj"] > r["base"]["risk_adj"]
        beats_tight = r["new"]["risk_adj"] > r["tight_ref"]["risk_adj"]
        n_wins_vs_base += int(beats_base)
        n_wins_vs_tight += int(beats_tight)
        print(f"  {r['holdout']:12s} ({r['cfg_label']}): new risk-adj={r['new']['risk_adj']:+.3f} "
              f"損平={r['new']['breakeven_bps']:5.1f}bps  vs baseline risk-adj={r['base']['risk_adj']:+.3f} "
              f"損平={r['base']['breakeven_bps']:5.1f}bps [{'贏' if beats_base else '輸'}]  "
              f"vs tight-scalp現行 risk-adj={r['tight_ref']['risk_adj']:+.3f} "
              f"損平={r['tight_ref']['breakeven_bps']:5.1f}bps [{'贏' if beats_tight else '輸'}]")
    print(f"\n  4折中有{n_wins_vs_base}折新出場機制在holdout上risk-adj優於baseline(live)")
    print(f"  4折中有{n_wins_vs_tight}折新出場機制在holdout上risk-adj優於tight-scalp現行出場(hold=10s,trail=1.0%固定)")


if __name__ == "__main__":
    main()
