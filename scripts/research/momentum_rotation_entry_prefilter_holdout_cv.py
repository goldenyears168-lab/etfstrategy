"""2026-08-13：tight-scalp（緊門檻confirm + hold_sec固定短秒數出場）目前最好的
代表點是4折3勝1負、但贏的窗口損平只有0.3~4.8bps，還在5bps最低成本估計之下。
這裡在**不動出場機制**（固定 hold_sec=10s、trail_pct=1.0%，抄
momentum_rotation_tight_scalp_holdout_cv.py 找到的代表點）的前提下，測試三個
額外的「進場前置條件」能不能把損平再往上推：

  (a) require_consec2：連續2根tick都符合「突破+量能確認」門檻才進場（濾掉單筆
      雜訊觸發——現行邏輯只要某一筆tick同時滿足價格突破+量能confirm就馬上進場，
      這裡改成要連續兩筆tick都滿足才觸發，第一筆只是"武裝"進入確認狀態）。
  (b) require_accel：進場當下的（絕對值）偏離開盤幅度，要比過去5筆同標的tick
      的偏離幅度都大（=新高，動能正在加速噴出，不是已經噴完在減速回吐）。
  (c) breakout_pct 本身也一起 sweep（0.3%~1.0%），跟 (a)(b) 交叉組合找最佳點。

vol_confirm_mult=2.5x / min_overshoot_pct=0.4%（今天tight-scalp找到、4折3勝1負
那組代表點的下緣，見momentum_rotation_tight_scalp_holdout_cv.py CONFIRM_TIERS的
「緊(2.5x/0.4%)」那一檔）維持固定，不在這裡的sweep範圍——這支腳本只測試(a)(b)(c)
這三個新的進場前置條件，其餘沿用tight-scalp代表點，避免把兩輪研究的變因混在一起。

4折留一窗口交叉驗證：3窗口sweep(a)(b)(c)全部組合、以risk-adj(日均/日std)排序
選最佳點，套到留下的第4個窗口驗證一次；baseline = 直接呼叫
momentum_breakout_strategy.simulate_portfolio_day（SSOT，含2026-08-13修好的
fill-price/exit-price bug）用現行live預設參數。
"""

from __future__ import annotations

import sys
import time
from collections import deque
from datetime import datetime
from itertools import product

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import simulate_portfolio_day as ssot_baseline  # noqa: E402
from momentum_rotation_redesign_search import WINDOWS, load_window  # noqa: E402

# 出場機制固定：今天tight-scalp找到的代表點
FIXED_EXIT = dict(trail_pct=1.0, hold_sec=10.0)
# 進場門檻固定（tight-scalp代表區間下緣，不在本次sweep範圍）
FIXED_ENTRY = dict(vol_confirm_mult=2.5, min_overshoot_pct=0.4, rearm_pct=0.25, preempt_mult=2.0)

BREAKOUT_PCT_GRID = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
CONSEC2_GRID = [False, True]
ACCEL_GRID = [False, True]
DEV_HISTORY_LEN = 5

BASELINE_KWARGS = dict(
    breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5,
    rearm_pct=0.25, min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0,
)


def build_day_cache(stock_day_data: dict) -> dict:
    """把merge+排序+ISO時間parse這些跟超參數無關的固定成本，一天只做一次、
    整個sweep重複用——這是原本效能瓶頸（每個grid combo都重新merge/sort/parse
    一次，32組合x4折x~70天 = 太慢）。回傳的cache只依賴tick原始資料，不依賴
    breakout_pct/rearm_pct等超參數（那些留在simulate函式裡用cache現算，很便宜）。
    """
    merged: list[tuple] = []
    opens: dict[str, float] = {}
    for sid, (times, prices, volumes) in stock_day_data.items():
        if prices.size < 2:
            continue
        opens[sid] = float(prices[0])
        for k in range(1, len(times)):
            t_epoch = datetime.fromisoformat(times[k]).timestamp()
            merged.append((t_epoch, times[k], sid, float(prices[k]), float(volumes[k])))
    merged.sort(key=lambda x: x[0])
    return {"merged": merged, "opens": opens}


def simulate_day_prefilter(
    cache: dict, *,
    breakout_pct: float, trail_pct: float, vol_confirm_mult: float, min_overshoot_pct: float,
    rearm_pct: float, preempt_mult: float, hold_sec: float,
    require_consec2: bool, require_accel: bool,
) -> list[dict]:
    opens = cache["opens"]
    meta: dict = {}
    for sid, open_price in opens.items():
        meta[sid] = {
            "open": open_price,
            "long_trigger": open_price * (1 + breakout_pct / 100.0),
            "short_trigger": open_price * (1 - breakout_pct / 100.0),
            "rearm_hi": open_price * (1 + rearm_pct / 100.0),
            "rearm_lo": open_price * (1 - rearm_pct / 100.0),
        }

    vol_history = {sid: [] for sid in meta}
    dev_history = {sid: deque(maxlen=DEV_HISTORY_LEN) for sid in meta}
    streak = {sid: 0 for sid in meta}
    armed = {sid: True for sid in meta}
    last_price = {sid: meta[sid]["open"] for sid in meta}
    trades: list[dict] = []
    position: dict | None = None

    for t_epoch, t, sid, p, v in cache["merged"]:
        st = meta[sid]
        last_price[sid] = p
        vh = vol_history[sid]
        baseline = max(np.median(vh), 1e-9) if vh else 1.0
        vh.append(v)

        dh = dev_history[sid]
        dev_now = abs(p - st["open"]) / st["open"] * 100.0
        # (b) 加速判定要用「這一筆之前」的歷史，所以先讀再更新
        accel_ok = (not require_accel) or (len(dh) >= DEV_HISTORY_LEN and dev_now > max(dh))
        dh.append(dev_now)

        is_held = position is not None and position["sid"] == sid
        if is_held:
            elapsed = t_epoch - position["entry_epoch"]
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
                streak[sid] = 0
            continue

        price_hits_long = p >= st["long_trigger"]
        price_hits_short = p <= st["short_trigger"]
        vol_ok = v >= vol_confirm_mult * baseline
        threshold_ok = (price_hits_long or price_hits_short) and vol_ok

        # (a) 連續2根tick都符合門檻才進場：第一筆只累積streak，不觸發
        streak[sid] = streak[sid] + 1 if threshold_ok else 0
        if not threshold_ok:
            continue
        if require_consec2 and streak[sid] < 2:
            continue

        direction = "long" if price_hits_long else "short"
        trigger = st["long_trigger"] if direction == "long" else st["short_trigger"]
        overshoot = abs(p - trigger) / st["open"] * 100.0
        if overshoot < min_overshoot_pct:
            continue
        if not accel_ok:
            continue

        vol_ratio = v / baseline
        score = overshoot * vol_ratio
        fill = float(p)
        candidate = {
            "sid": sid, "direction": direction, "fill": fill, "entry": fill,
            "entry_time": t, "entry_epoch": t_epoch, "entry_score": score, "peak_trough": fill,
            "overshoot": overshoot, "vol_ratio": vol_ratio,
        }

        if position is None:
            position = candidate
            armed[sid] = False
            streak[sid] = 0
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
            streak[sid] = 0

    if position is not None:
        exit_price = last_price[position["sid"]]
        ret_pct = (
            (exit_price - position["fill"]) / position["fill"] * 100.0
            if position["direction"] == "long"
            else (position["fill"] - exit_price) / position["fill"] * 100.0
        )
        trades.append({**position, "exit_time": "day_end", "exit": exit_price, "ret_pct": ret_pct, "reason": "day_end_forced"})
    return trades


def _metrics_from_trades(all_trades: list[dict], per_day: dict[str, float]) -> dict:
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    day_rets = np.array(list(per_day.values())) if per_day else np.array([])
    if len(rets) < 20 or len(day_rets) == 0 or day_rets.std() == 0:
        return {"n": len(rets), "risk_adj": float("-inf"), "breakeven_bps": 0.0, "win_rate": 0.0}
    breakeven = (rets.sum() / len(rets)) * 100
    win = float(np.mean(rets > 0) * 100)
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    return {"n": len(rets), "risk_adj": day_mean / day_std, "breakeven_bps": breakeven, "win_rate": win}


def build_all_day_caches(all_windows: dict) -> dict[str, dict[str, dict]]:
    """{wname: {day: cache}}，只建一次、後面sweep全部組合共用（cache跟超參數無關）。"""
    out: dict[str, dict[str, dict]] = {}
    for wname, (all_by_stock, all_days) in all_windows.items():
        day_caches: dict[str, dict] = {}
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            day_caches[d] = build_day_cache(day_data)
        out[wname] = day_caches
    return out


def _run_prefilter(day_caches_subset: dict[str, dict[str, dict]], **kwargs) -> dict:
    all_trades = []
    per_day: dict[str, float] = {}
    for _wname, day_caches in day_caches_subset.items():
        for d, cache in day_caches.items():
            trades = simulate_day_prefilter(cache, **kwargs)
            all_trades.extend(trades)
            per_day[d] = per_day.get(d, 0.0) + sum(t["ret_pct"] for t in trades)
    return _metrics_from_trades(all_trades, per_day)


def _run_baseline(windows_subset: dict, **kwargs) -> dict:
    all_trades = []
    per_day: dict[str, float] = {}
    for _wname, (all_by_stock, all_days) in windows_subset.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            trades = ssot_baseline(day_data, **kwargs)
            all_trades.extend(trades)
            per_day[d] = per_day.get(d, 0.0) + sum(t["ret_pct"] for t in trades)
    return _metrics_from_trades(all_trades, per_day)


def main() -> None:
    t0 = time.time()
    print("載入4窗口資料...")
    all_windows = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    wnames = list(all_windows.keys())

    print("預建逐日tick cache（merge/sort/parse只做一次，sweep全部組合共用）...")
    all_day_caches = build_all_day_caches(all_windows)
    n_days_total = sum(len(v) for v in all_day_caches.values())
    print(f"  cache完成 · {len(wnames)}窗口 · 共{n_days_total}個交易日 · {time.time() - t0:.1f}s")

    grid = list(product(BREAKOUT_PCT_GRID, CONSEC2_GRID, ACCEL_GRID))
    print(f"sweep grid: breakout_pct{BREAKOUT_PCT_GRID} x consec2{CONSEC2_GRID} x accel{ACCEL_GRID} "
          f"= {len(grid)}組合 · 固定 vol_confirm_mult={FIXED_ENTRY['vol_confirm_mult']}x "
          f"min_overshoot_pct={FIXED_ENTRY['min_overshoot_pct']}% hold_sec={FIXED_EXIT['hold_sec']}s "
          f"trail_pct={FIXED_EXIT['trail_pct']}%")
    print("=" * 100)

    print("\n### 對照：tight-scalp代表點（無a/b/c新條件, breakout_pct=0.5）全4窗口 ###")
    m_ts = _run_prefilter(
        all_day_caches, breakout_pct=0.5,
        **FIXED_ENTRY, **FIXED_EXIT, require_consec2=False, require_accel=False,
    )
    print(f"  n={m_ts['n']} 勝率={m_ts['win_rate']:.1f}% 損平={m_ts['breakeven_bps']:.1f}bps risk-adj={m_ts['risk_adj']:+.3f}")

    print("\n### 對照：SSOT baseline（momentum_breakout_strategy.simulate_portfolio_day，live預設參數）全4窗口 ###")
    m0 = _run_baseline(all_windows, **BASELINE_KWARGS)
    print(f"  n={m0['n']} 勝率={m0['win_rate']:.1f}% 損平={m0['breakeven_bps']:.1f}bps risk-adj={m0['risk_adj']:+.3f}")

    fold_results = []
    for holdout_name in wnames:
        train_caches = {k: v for k, v in all_day_caches.items() if k != holdout_name}
        holdout_caches = {holdout_name: all_day_caches[holdout_name]}
        holdout_windows_raw = {holdout_name: all_windows[holdout_name]}
        print(f"\n### Fold: holdout={holdout_name} · train={[k for k in wnames if k != holdout_name]} ###")

        best = None
        for bp, c2, acc in grid:
            m = _run_prefilter(
                train_caches, breakout_pct=bp,
                **FIXED_ENTRY, **FIXED_EXIT, require_consec2=c2, require_accel=acc,
            )
            if best is None or m["risk_adj"] > best[3]["risk_adj"]:
                best = (bp, c2, acc, m)
        bp_best, c2_best, acc_best, train_m = best
        print(f"  train最佳點: breakout_pct={bp_best}% consec2={c2_best} accel={acc_best} "
              f"(train risk-adj={train_m['risk_adj']:.3f} 損平={train_m['breakeven_bps']:.1f}bps "
              f"勝率={train_m['win_rate']:.1f}% n={train_m['n']}) · 累計{time.time() - t0:.1f}s")

        holdout_new = _run_prefilter(
            holdout_caches, breakout_pct=bp_best,
            **FIXED_ENTRY, **FIXED_EXIT, require_consec2=c2_best, require_accel=acc_best,
        )
        holdout_base = _run_baseline(holdout_windows_raw, **BASELINE_KWARGS)
        holdout_ts = _run_prefilter(
            holdout_caches, breakout_pct=0.5,
            **FIXED_ENTRY, **FIXED_EXIT, require_consec2=False, require_accel=False,
        )
        print(f"  >>> HOLDOUT({holdout_name}) new(a/b/c) : n={holdout_new['n']:4d} "
              f"勝率={holdout_new['win_rate']:5.1f}% 損平={holdout_new['breakeven_bps']:6.1f}bps risk-adj={holdout_new['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) tight-scalp: n={holdout_ts['n']:4d} "
              f"勝率={holdout_ts['win_rate']:5.1f}% 損平={holdout_ts['breakeven_bps']:6.1f}bps risk-adj={holdout_ts['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) SSOT base  : n={holdout_base['n']:4d} "
              f"勝率={holdout_base['win_rate']:5.1f}% 損平={holdout_base['breakeven_bps']:6.1f}bps risk-adj={holdout_base['risk_adj']:+.3f}")
        fold_results.append({
            "holdout": holdout_name, "bp": bp_best, "c2": c2_best, "acc": acc_best,
            "new": holdout_new, "ts": holdout_ts, "base": holdout_base,
        })

    print("\n" + "=" * 100)
    print("=== 4折總結（vs SSOT baseline）===")
    n_wins_base = 0
    n_wins_ts = 0
    for r in fold_results:
        beats_base = r["new"]["risk_adj"] > r["base"]["risk_adj"]
        beats_ts = r["new"]["risk_adj"] > r["ts"]["risk_adj"]
        n_wins_base += int(beats_base)
        n_wins_ts += int(beats_ts)
        print(f"  {r['holdout']:12s} (bp={r['bp']}% consec2={r['c2']} accel={r['acc']}): "
              f"new risk-adj={r['new']['risk_adj']:+.3f} 損平={r['new']['breakeven_bps']:5.1f}bps  "
              f"vs baseline risk-adj={r['base']['risk_adj']:+.3f}（{'贏' if beats_base else '輸'}）  "
              f"vs tight-scalp risk-adj={r['ts']['risk_adj']:+.3f}（{'贏' if beats_ts else '輸'}）")
    print(f"\n  4折中有{n_wins_base}折 new(a/b/c) 在holdout上risk-adj優於SSOT baseline")
    print(f"  4折中有{n_wins_ts}折 new(a/b/c) 在holdout上risk-adj優於tight-scalp代表點（無a/b/c）")


if __name__ == "__main__":
    main()
