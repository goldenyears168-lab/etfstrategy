"""2026-08-13：ATR/波動度相對門檻取代固定百分比門檻。

假說：現行 breakout_pct=0.5%、trail_pct=1.0% 對全部12檔一視同仁，但這些股票
的波動特性差很多——低波動股用0.5%可能太寬鬆（常態雜訊就能觸發假訊號）、高波動
股用0.5%可能太窄（正常波動就觸發，訊噪比差）。

做法：對每檔股票，用「開盤後累積至今」的價格離散度（(price/open-1)*100 的
Welford 遞增標準差，只用當下tick之前的資料，避免用未來資訊）當作滾動波動度指標
（ATR概念的簡化版：不是分鐘K高低差，是累積至今tick價格相對開盤價偏離的標準差，
同樣單位是open的%，跟breakout_pct直接可比）。

dyn_breakout_pct(t) = max(floor_pct, k * cumulative_std_pct(t-))
在還沒累積到warmup_ticks筆之前用固定fallback_pct（避免開盤第一批tick因樣本太少
而门槛不穩定/過緊完全擋住早盤訊號，這通常是動能訊號最密集的時段）。

long_trigger/short_trigger 改用 dyn_breakout_pct 動態算（每檔、每個時間點都不同），
其餘（量能確認vol_confirm_mult、rearm_pct、min_overshoot_pct、min_vol_ratio、
preempt_mult搶佔機制、trail_pct移動停利）維持跟baseline一樣的固定值，這樣才能
乾淨隔離「相對波動度門檻」這一個變數的效果。

保留搶佔機制（使用者2026-08-13明確要求不測完全關掉搶佔的方向）。

跑法：
  PYTHONPATH=src:scripts/research .venv/bin/python -u \
    scripts/research/momentum_rotation_atr_relative_threshold.py
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


def simulate_day(
    stock_day_data: dict,
    *,
    trail_pct: float,
    vol_confirm_mult: float,
    rearm_pct: float,
    min_overshoot_pct: float,
    min_vol_ratio: float,
    preempt_mult: float,
    use_relative_threshold: bool,
    k: float = 1.0,
    floor_pct: float = 0.15,
    warmup_ticks: int = 20,
    fallback_pct: float = 0.5,
    fixed_breakout_pct: float = 0.5,
) -> list[dict]:
    """use_relative_threshold=False 時完全等同固定 fixed_breakout_pct 的 baseline
    （拿來在同一份程式碼路徑上做嚴格對照，避免兩支腳本邏輯漂移）。"""
    merged: list[tuple] = []
    meta: dict = {}
    for sid, (times, prices, volumes) in stock_day_data.items():
        if prices.size < 2:
            continue
        open_price = float(prices[0])
        meta[sid] = {
            "open": open_price,
            "rearm_hi": open_price * (1 + rearm_pct / 100.0),
            "rearm_lo": open_price * (1 - rearm_pct / 100.0),
        }
        for kk in range(1, len(times)):
            merged.append((times[kk], sid, float(prices[kk]), float(volumes[kk])))
    merged.sort(key=lambda x: x[0])

    vol_history = {sid: [] for sid in meta}
    vol_stats = {sid: {"n": 0, "mean": 0.0, "M2": 0.0} for sid in meta}
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

        # 動態門檻：只用「這個tick之前」已經累積的離散度，避免未來資訊
        vstat = vol_stats[sid]
        if use_relative_threshold:
            if vstat["n"] >= warmup_ticks:
                std = (vstat["M2"] / vstat["n"]) ** 0.5
                dyn_pct = max(floor_pct, k * std)
            else:
                dyn_pct = fallback_pct
        else:
            dyn_pct = fixed_breakout_pct
        long_trigger = st["open"] * (1 + dyn_pct / 100.0)
        short_trigger = st["open"] * (1 - dyn_pct / 100.0)

        # 用當下這個tick更新累積離散度統計（給"下一個" tick用，因果、不洩漏）
        x = (p / st["open"] - 1.0) * 100.0
        vstat["n"] += 1
        delta = x - vstat["mean"]
        vstat["mean"] += delta / vstat["n"]
        delta2 = x - vstat["mean"]
        vstat["M2"] += delta * delta2

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

        price_hits_long = p >= long_trigger
        price_hits_short = p <= short_trigger
        if not (price_hits_long or price_hits_short) or v < vol_confirm_mult * baseline:
            continue
        direction = "long" if price_hits_long else "short"
        trigger = long_trigger if direction == "long" else short_trigger
        overshoot = abs(p - trigger) / st["open"] * 100.0
        vol_ratio = v / baseline
        if overshoot < min_overshoot_pct or vol_ratio < min_vol_ratio:
            continue
        score = overshoot * vol_ratio
        fill = float(p)
        candidate = {
            "sid": sid, "direction": direction, "fill": fill, "entry": fill,
            "entry_time": t, "entry_score": score, "peak_trough": fill,
            "overshoot": overshoot, "vol_ratio": vol_ratio, "dyn_pct": dyn_pct,
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


def run_variant(name: str, windows_data: dict, **kwargs) -> dict:
    daily_gross: list[float] = []
    all_trades: list[dict] = []
    for _wname, (all_by_stock, all_days) in windows_data.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            day_trades = simulate_day(day_data, **kwargs)
            all_trades.extend(day_trades)
            daily_gross.append(sum(tr["ret_pct"] for tr in day_trades))

    n_days = len(daily_gross)
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    daily = np.array(daily_gross) if daily_gross else np.array([])
    if len(rets) == 0 or n_days == 0:
        print(f"{name}: 無交易")
        return {"name": name, "n_trades": 0}

    breakeven_bps = (rets.sum() / len(rets)) * 100.0
    net_at = {c: (rets - c / 100.0).sum() / n_days for c in COST_SCENARIOS_BPS}
    risk_adj = daily.mean() / daily.std() if daily.std() > 0 else float("nan")

    result = {
        "name": name,
        "n_trades": len(rets),
        "trades_per_day": len(rets) / n_days,
        "n_days": n_days,
        "trade_ret_mean": float(rets.mean()),
        "trade_ret_std": float(rets.std()),
        "trade_ret_max_loss": float(rets.min()),
        "trade_ret_max_gain": float(rets.max()),
        "win_rate": float(np.mean(rets > 0) * 100),
        "daily_gross_mean": float(daily.mean()),
        "daily_gross_std": float(daily.std()),
        "daily_worst": float(daily.min()),
        "daily_best": float(daily.max()),
        "pct_losing_days": float(np.mean(daily < 0) * 100),
        "risk_adj": float(risk_adj),
        "breakeven_bps": float(breakeven_bps),
        "net_at": net_at,
    }
    print(
        f"{name:34s}: n={result['n_trades']:4d} 筆/天={result['trades_per_day']:4.2f} "
        f"勝率={result['win_rate']:5.1f}% 損平={breakeven_bps:5.1f}bps "
        f"日均={result['daily_gross_mean']:+7.3f}% 日std={result['daily_gross_std']:.3f}% "
        f"risk-adj={risk_adj:.3f} 最差日={result['daily_worst']:+.2f}% "
        f"虧損日={result['pct_losing_days']:4.1f}% "
        + " ".join(f"{c}bps={v:+.2f}%" for c, v in net_at.items())
    )
    return result


def print_full_report(r: dict) -> None:
    if r.get("n_trades", 0) == 0:
        print(f"  {r['name']}: 無交易")
        return
    print(f"\n--- {r['name']} 完整數字 ---")
    print(f"  n交易={r['n_trades']}  筆/天={r['trades_per_day']:.2f}  n天={r['n_days']}")
    print(f"  單筆報酬: mean={r['trade_ret_mean']:+.4f}%  std={r['trade_ret_std']:.4f}%  "
          f"max_loss={r['trade_ret_max_loss']:+.3f}%  max_gain={r['trade_ret_max_gain']:+.3f}%  勝率={r['win_rate']:.1f}%")
    print(f"  日報酬(gross加總): mean={r['daily_gross_mean']:+.4f}%  std={r['daily_gross_std']:.4f}%  "
          f"最差={r['daily_worst']:+.3f}%  最好={r['daily_best']:+.3f}%  虧損天比例={r['pct_losing_days']:.1f}%")
    print(f"  risk-adjusted(日均/日std) = {r['risk_adj']:.4f}")
    print(f"  損平成本 = {r['breakeven_bps']:.2f} bps")
    print("  淨日均 @ 5/10/20/29bps: " + " / ".join(f"{c}bps={v:+.3f}%" for c, v in r["net_at"].items()))


def diagnose_vol_metric(windows_data: dict) -> None:
    """開跑前先看一下累積std的量級，避免亂猜k。"""
    samples = []
    for _wname, (all_by_stock, all_days) in windows_data.items():
        for sid, days in all_by_stock.items():
            for d, (times, prices, volumes) in days.items():
                if prices.size < 30:
                    continue
                open_price = float(prices[0])
                x = (prices / open_price - 1.0) * 100.0
                # 累積std在整個交易日結束時的值（近似上界，給個量級感覺）
                samples.append(float(np.std(x[1:])))
    arr = np.array(samples)
    print(f"[診斷] 全日累積離散度std（開盤到收盤全樣本）: "
          f"median={np.median(arr):.3f}% p25={np.percentile(arr,25):.3f}% "
          f"p75={np.percentile(arr,75):.3f}% mean={arr.mean():.3f}%")
    print(f"       => 若k*std要落在baseline固定0.5%附近，k約落在 "
          f"{0.5/np.percentile(arr,75):.2f}~{0.5/np.percentile(arr,25):.2f} 之間\n")


def main() -> None:
    print("載入4窗口資料...")
    windows_data = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    print()

    diagnose_vol_metric(windows_data)

    common = dict(trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
                  min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0)

    print("=== 對照組：固定0.5%門檻（同一份程式碼路徑跑baseline，驗證邏輯等價）===")
    base_result = run_variant("baseline(固定0.5%,同路徑)", windows_data,
                               **common, use_relative_threshold=False, fixed_breakout_pct=0.5)
    print()

    print("=== ATR/相對波動度門檻：掃k值（floor=0.15%, fallback=0.5%, warmup=20 ticks）===")
    results = []
    for k in [0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0]:
        r = run_variant(f"relative k={k}", windows_data, **common,
                         use_relative_threshold=True, k=k, floor_pct=0.15,
                         warmup_ticks=20, fallback_pct=0.5)
        if r.get("n_trades"):
            results.append(r)
    print()

    print("=== 敏感度：warmup_ticks（固定選一個中段k值）===")
    best_k = None
    if results:
        # 先抓risk_adj最高的k當作warmup敏感度分析的基準k
        best_k_result = max(results, key=lambda r: r["risk_adj"] if r["risk_adj"] == r["risk_adj"] else -999)
        best_k = float(best_k_result["name"].split("=")[1])
        print(f"(以risk-adj最佳的 k={best_k} 為基準)")
        for wu in [10, 20, 40, 80]:
            r = run_variant(f"k={best_k} warmup={wu}ticks", windows_data, **common,
                             use_relative_threshold=True, k=best_k, floor_pct=0.15,
                             warmup_ticks=wu, fallback_pct=0.5)
            if r.get("n_trades"):
                results.append(r)
    print()

    print("=== 敏感度：floor_pct（固定best_k，warmup=20）===")
    if best_k is not None:
        for fl in [0.05, 0.15, 0.25, 0.35]:
            r = run_variant(f"k={best_k} floor={fl}%", windows_data, **common,
                             use_relative_threshold=True, k=best_k, floor_pct=fl,
                             warmup_ticks=20, fallback_pct=0.5)
            if r.get("n_trades"):
                results.append(r)
    print()

    print("=== risk-adjusted(日均/日std)排行（最重要的比較基準，不是只看損平）===")
    all_results = [base_result] + results
    all_results = [r for r in all_results if r.get("n_trades")]
    all_results.sort(key=lambda r: -(r["risk_adj"] if r["risk_adj"] == r["risk_adj"] else -999))
    for r in all_results:
        print(f"  {r['name']:34s} risk-adj={r['risk_adj']:.4f}  損平={r['breakeven_bps']:5.1f}bps  "
              f"日均={r['daily_gross_mean']:+.3f}%  日std={r['daily_gross_std']:.3f}%  勝率={r['win_rate']:.1f}%")

    print()
    print_full_report(base_result)
    if results:
        top = max(results, key=lambda r: r["risk_adj"] if r["risk_adj"] == r["risk_adj"] else -999)
        print_full_report(top)


if __name__ == "__main__":
    main()
