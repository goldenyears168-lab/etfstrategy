"""2026-08-13：把「真實執行延遲」建進momentum-rotation backtest，量化回測與真實
狀況之間還差多遠。

背景：今天稍早已經把simulate_day的fill從「理論trigger價」改成「訊號當下實際
tick價」——這比原本樂觀，但**還是比live執行樂觀**：真實live有worker輪詢間隔
（現在1秒）＋送單到broker確認成交的延遲（2026-08-13實測約780ms），這段時間內
價格可能又走了一段，先前的backtest完全沒模擬這個延遲。

這支腳本做的事：
  1. entry/exit的「決策」仍然用訊號成立那個tick的觀測價格（overshoot／
     vol_ratio／要不要出場／要不要搶佔——這些都是即時看盤會做的判斷，delay
     不影響「看到了沒」，只影響「成交在哪」）。
  2. 但實際**成交價**改成：訊號成立時間點 + 模擬延遲後，該標的下一筆（或最
     接近那個時間點之後）的tick價格——用datetime64[ms] + np.searchsorted對
     每檔個股自己的時間序列做時間戳查找，不是簡單array位移。
  3. 掃過延遲 0(對照)/0.5/1/2/3 秒，對「現行生產參數」跟「今天找到的最佳候選
     overshoot∈[0.5%,1.0%]」都各跑一次，看損平成本／勝率／risk-adjusted隨延遲
     怎麼惡化。
  4. 資料本身只有整秒解析度（tick timestamp無毫秒），所以0.5秒與1.0秒理論上
     會被『下一筆整秒tick』取整成同一個門檻（target= T+0.5s或T+1.0s，兩者的
     第一個滿足的整數秒tick時間相同，通常都是T+1s那一筆）——這是資料解析度
     的先天限制，不是程式bug，報告時要老實註記；2秒／3秒的差異則資料解析度
     足以呈現。

跑法：
  PYTHONPATH=src:scripts/research .venv/bin/python -u \
    scripts/research/momentum_rotation_execution_delay_reaudit.py
"""

from __future__ import annotations

import sys
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
DELAYS_S = [0.0, 0.5, 1.0, 2.0, 3.0]

BASE = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
            min_overshoot_pct=0.15, max_overshoot_pct=999.0, min_vol_ratio=1.5, preempt_mult=2.0)
BEST_CANDIDATE = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
                      min_overshoot_pct=0.5, max_overshoot_pct=1.0, min_vol_ratio=1.5, preempt_mult=2.0)


def _delayed_lookup(
    series: dict, sid: str, start_idx: int, signal_dt: np.datetime64, delay_ms: int, fallback: float,
) -> tuple[float, bool, float]:
    """回傳 (成交價, 是否真的找到延遲後的tick, 該tick與訊號時間實際相差秒數)。

    ``start_idx`` 是訊號tick在該標的自己時間序列裡的已知正確索引（由呼叫端在
    合併時間軸時一併記錄）——查找一律從這裡往後找，不對整個陣列做搜尋。這是
    必要的：tick timestamp只有整秒解析度，同一秒常有多筆tick，若對整個陣列做
    searchsorted，delay=0時target會等於訊號tick自己的timestamp，遇到「同一秒
    有更早的tie」就會被搜尋結果帶回更早那一筆、價格不等於訊號當下觀測到的p，
    污染了delay=0應該精確重現「fill=訊號當下tick價」的對照組。從start_idx往
    後找（用局部searchsorted，只在 times_dt[start_idx:] 這段做二分搜尋）就能
    保證delay=0時精確落回start_idx本身。

    找不到（訊號太靠近當天收盤，資料已經沒有後續tick）時用fallback（=當下
    觀測價p）頂替，並回傳found=False，供另外統計「延遲後無資料可用」的比例。
    """
    times_dt, prices = series[sid]
    target = signal_dt + np.timedelta64(delay_ms, "ms")
    rel_idx = int(np.searchsorted(times_dt[start_idx:], target, side="left"))
    idx = start_idx + rel_idx
    if idx >= len(prices):
        return fallback, False, float("nan")
    actual_lag_s = (times_dt[idx] - signal_dt) / np.timedelta64(1, "ms") / 1000.0
    return float(prices[idx]), True, float(actual_lag_s)


def simulate_day(
    stock_day_data: dict,
    *,
    delay_s: float,
    breakout_pct: float, trail_pct: float, vol_confirm_mult: float, rearm_pct: float,
    min_overshoot_pct: float, max_overshoot_pct: float, min_vol_ratio: float, preempt_mult: float,
) -> tuple[list[dict], int, int]:
    """跟momentum_rotation_max_overshoot_cap.simulate_day的規則完全一樣（決策
    邏輯不變），差別只在：entry fill／exit fill 一律改成「訊號時間+delay_s後，
    該標的下一筆tick價」，不是訊號當下那個tick的價。回傳 (trades, n_delayed_fills,
    n_fallback_no_future_tick)。"""
    delay_ms = int(round(delay_s * 1000))
    series: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    merged: list[tuple] = []
    meta: dict = {}
    for sid, (times, prices, volumes) in stock_day_data.items():
        if prices.size < 2:
            continue
        times_dt = np.array(times, dtype="datetime64[ms]")
        series[sid] = (times_dt, prices)
        open_price = float(prices[0])
        meta[sid] = {
            "open": open_price,
            "long_trigger": open_price * (1 + breakout_pct / 100.0),
            "short_trigger": open_price * (1 - breakout_pct / 100.0),
            "rearm_hi": open_price * (1 + rearm_pct / 100.0),
            "rearm_lo": open_price * (1 - rearm_pct / 100.0),
        }
        for k in range(1, len(times)):
            merged.append((times_dt[k], sid, float(prices[k]), float(volumes[k]), k))
    merged.sort(key=lambda x: x[0])

    vol_history = {sid: [] for sid in meta}
    armed = {sid: True for sid in meta}
    last_price = {sid: meta[sid]["open"] for sid in meta}
    last_idx = {sid: 0 for sid in meta}
    trades: list[dict] = []
    position: dict | None = None
    n_delayed = 0
    n_fallback = 0

    def fill_at(sid: str, start_idx: int, signal_dt: np.datetime64, fallback: float) -> float:
        nonlocal n_delayed, n_fallback
        price, found, _lag = _delayed_lookup(series, sid, start_idx, signal_dt, delay_ms, fallback)
        n_delayed += 1
        if not found:
            n_fallback += 1
        return price

    for t, sid, p, v, k in merged:
        st = meta[sid]
        last_price[sid] = p
        last_idx[sid] = k
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
                exit_price = fill_at(sid, last_idx[sid], t, p)
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
        if overshoot < min_overshoot_pct or overshoot > max_overshoot_pct or vol_ratio < min_vol_ratio:
            continue
        score = overshoot * vol_ratio
        signal_time = t

        if position is None:
            fill = fill_at(sid, k, signal_time, p)
            candidate = {
                "sid": sid, "direction": direction, "fill": fill, "entry": p,
                "entry_time": t, "entry_score": score, "peak_trough": fill,
                "overshoot": overshoot, "vol_ratio": vol_ratio,
            }
            position = candidate
            armed[sid] = False
        elif score >= preempt_mult * position["entry_score"]:
            held_sid = position["sid"]
            exit_price = fill_at(held_sid, last_idx[held_sid], signal_time, last_price[held_sid])
            ret_pct = (
                (exit_price - position["fill"]) / position["fill"] * 100.0
                if position["direction"] == "long"
                else (position["fill"] - exit_price) / position["fill"] * 100.0
            )
            trades.append({**position, "exit_time": t, "exit": exit_price, "ret_pct": ret_pct, "reason": "preempted"})
            armed[held_sid] = False
            fill = fill_at(sid, k, signal_time, p)
            candidate = {
                "sid": sid, "direction": direction, "fill": fill, "entry": p,
                "entry_time": t, "entry_score": score, "peak_trough": fill,
                "overshoot": overshoot, "vol_ratio": vol_ratio,
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
    return trades, n_delayed, n_fallback


def run_variant(name: str, windows_data: dict, **kwargs) -> dict:
    all_trades = []
    total_days = 0
    n_delayed_total = 0
    n_fallback_total = 0
    for _wname, (all_by_stock, all_days) in windows_data.items():
        total_days += len(all_days)
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            trades, n_delayed, n_fallback = simulate_day(day_data, **kwargs)
            all_trades.extend(trades)
            n_delayed_total += n_delayed
            n_fallback_total += n_fallback
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    if len(rets) == 0:
        print(f"{name}: 無交易")
        return {}
    gross = rets.sum() / total_days
    win = float(np.mean(rets > 0) * 100)
    std = float(rets.std())
    risk_adj = gross / std if std > 0 else float("nan")
    breakeven = (rets.sum() / len(rets)) * 100
    # 日報酬（gross加總）分佈：用 entry_time 所屬日期分桶
    day_rets: dict[str, float] = {}
    for t in all_trades:
        et = t["entry_time"]
        dkey = str(et)[:10]
        day_rets[dkey] = day_rets.get(dkey, 0.0) + t["ret_pct"]
    day_arr = np.array(list(day_rets.values())) if day_rets else np.array([])
    worst_day = float(day_arr.min()) if day_arr.size else float("nan")
    best_day = float(day_arr.max()) if day_arr.size else float("nan")
    losing_day_pct = float(np.mean(day_arr < 0) * 100) if day_arr.size else float("nan")
    net_lines = [f"{c}bps={(rets - c/100.0).sum()/total_days:+.2f}%" for c in COST_SCENARIOS_BPS]
    fallback_pct = (n_fallback_total / n_delayed_total * 100.0) if n_delayed_total else 0.0
    print(f"{name:34s}: n={len(rets):4d} 筆/天={len(rets)/total_days:5.2f} "
          f"勝率={win:5.1f}% gross日均={gross:+7.3f}% 損平={breakeven:5.1f}bps "
          f"單筆std={std:.3f}% risk-adj={risk_adj:.3f} 最差日={worst_day:+.2f}% "
          f"虧損日%={losing_day_pct:4.1f}% no-fill-fallback={fallback_pct:4.1f}%  " + " ".join(net_lines))
    return {
        "name": name, "n_trades": len(rets), "trades_per_day": len(rets) / total_days,
        "win_rate": win, "gross_day_mean": gross, "breakeven_bps": breakeven, "std": std,
        "risk_adj": risk_adj, "worst_day": worst_day, "best_day": best_day,
        "losing_day_pct": losing_day_pct, "fallback_pct": fallback_pct,
    }


def main() -> None:
    print("載入4窗口資料...")
    windows_data = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}

    print()
    print("=== A. 現行生產參數（min_overshoot=0.15%、無上限）延遲掃描 ===")
    a_results = []
    for delay in DELAYS_S:
        r = run_variant(f"生產參數 delay={delay}s", windows_data, delay_s=delay, **BASE)
        if r:
            a_results.append(r)

    print()
    print("=== B. 今天最佳候選 overshoot∈[0.5%,1.0%] 延遲掃描（保留搶佔） ===")
    b_results = []
    for delay in DELAYS_S:
        r = run_variant(f"最佳候選 delay={delay}s", windows_data, delay_s=delay, **BEST_CANDIDATE)
        if r:
            b_results.append(r)

    print()
    print("=== 延遲惡化幅度摘要（相對delay=0） ===")
    for label, results in (("生產參數", a_results), ("最佳候選", b_results)):
        if not results:
            continue
        base0 = results[0]
        print(f"\n{label}（delay=0基準：損平={base0['breakeven_bps']:.1f}bps 勝率={base0['win_rate']:.1f}% "
              f"risk-adj={base0['risk_adj']:.3f}）")
        for r in results[1:]:
            d_be = r["breakeven_bps"] - base0["breakeven_bps"]
            d_win = r["win_rate"] - base0["win_rate"]
            d_ra = r["risk_adj"] - base0["risk_adj"]
            print(f"  delay={r['name'].split('delay=')[1]:6s} 損平={r['breakeven_bps']:5.1f}bps({d_be:+.1f}) "
                  f"勝率={r['win_rate']:5.1f}%({d_win:+.1f}pp) risk-adj={r['risk_adj']:.3f}({d_ra:+.3f}) "
                  f"最差日={r['worst_day']:+.2f}%")


if __name__ == "__main__":
    main()
