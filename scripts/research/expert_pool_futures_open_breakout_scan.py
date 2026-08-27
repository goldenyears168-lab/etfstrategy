#!/usr/bin/env python3
"""分點專家池個股期貨「相對當日開盤價突破X%判斷多空 + 移動停利」掃描（exploratory · 非採納）.

進場基準＝當日開盤價（第一筆tick，08:45確定，非look-ahead）：
  - 漲過 開盤價×(1+X%) → 做多（動能突破）
  - 跌破 開盤價×(1-X%) → 做空（動能跌破）
  - 兩個方向哪個先觸發就進哪邊（同一天只進一次，觸發後不切換方向）；成交價＝
    觸發門檻本身（不取更有利的實際成交價，避免 BUG-5 fill clamp）。
  - 出場：進場後方向性追蹤高/低點，反向移動 trail% 觸發停利；當日結束前沒觸發
    就強制在最後一筆 tick 平倉（當天平倉）。

跟前兩版（前一日收盤、當日高點回檔）不同：這版是動能突破（追價），不是逆勢接刀；
且雙向都測，可以看多空表現是否不對稱。

限制同前兩版腳本：22天樣本、不含手續費滑價、grid search 多重比較。

用法：
  PYTHONPATH=src .venv/bin/python \
    scripts/research/expert_pool_futures_open_breakout_scan.py
"""

from __future__ import annotations

import math
import statistics
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from expert_pool_futures_dip_buy_trail_scan import DATA_DIR, load_stock  # noqa: E402

THRESHOLDS = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
TRAILS = [1.0, 1.5, 2.0, 3.0, 5.0]


def simulate_day(prices: np.ndarray, x_pct: float, trail_pct: float) -> dict | None:
    if prices.size < 2:
        return None
    open_price = float(prices[0])
    long_trigger = open_price * (1 + x_pct / 100.0)
    short_trigger = open_price * (1 - x_pct / 100.0)

    long_hits = np.where(prices >= long_trigger)[0]
    short_hits = np.where(prices <= short_trigger)[0]
    long_idx = int(long_hits[0]) if long_hits.size else None
    short_idx = int(short_hits[0]) if short_hits.size else None

    if long_idx is None and short_idx is None:
        return None
    if short_idx is None or (long_idx is not None and long_idx < short_idx):
        direction, entry_idx, fill = "long", long_idx, long_trigger
    else:
        direction, entry_idx, fill = "short", short_idx, short_trigger

    remainder = prices[entry_idx + 1 :]
    if remainder.size == 0:
        return {"direction": direction, "entry": fill, "exit": fill, "ret_pct": 0.0, "reason": "no_ticks_after_entry"}

    seq = np.concatenate(([fill], remainder))
    if direction == "long":
        post_peak = np.maximum.accumulate(seq)[1:]
        stops = post_peak * (1 - trail_pct / 100.0)
        breach = np.where(remainder <= stops)[0]
        if breach.size > 0:
            exit_price = float(stops[int(breach[0])])
            reason = "trail_stop"
        else:
            exit_price = float(remainder[-1])
            reason = "day_end_forced"
        ret_pct = (exit_price - fill) / fill * 100.0
    else:
        post_trough = np.minimum.accumulate(seq)[1:]
        stops = post_trough * (1 + trail_pct / 100.0)
        breach = np.where(remainder >= stops)[0]
        if breach.size > 0:
            exit_price = float(stops[int(breach[0])])
            reason = "trail_stop"
        else:
            exit_price = float(remainder[-1])
            reason = "day_end_forced"
        ret_pct = (fill - exit_price) / fill * 100.0

    return {"direction": direction, "entry": fill, "exit": exit_price, "ret_pct": ret_pct, "reason": reason}


def simulate(days: dict[str, tuple[np.ndarray, str]], x_pct: float, trail_pct: float) -> list[dict]:
    trades = []
    for d in sorted(days.keys()):
        prices, _contract = days[d]
        res = simulate_day(prices, x_pct, trail_pct)
        if res:
            res["date"] = d
            trades.append(res)
    return trades


def naive_ttest(returns: list[float]) -> tuple[float, float]:
    n = len(returns)
    if n < 2:
        return float("nan"), float("nan")
    mean = statistics.mean(returns)
    sd = statistics.stdev(returns)
    if sd == 0:
        return float("nan"), float("nan")
    t = mean / (sd / math.sqrt(n))
    from math import erf

    p = 2 * (1 - 0.5 * (1 + erf(abs(t) / math.sqrt(2))))
    return t, p


def stats_line(rets: list[float]) -> tuple[int, float, float, float, float, float, float]:
    n = len(rets)
    if n == 0:
        return 0, float("nan"), float("nan"), float("nan"), 0.0, float("nan"), float("nan")
    win = sum(1 for r in rets if r > 0) / n * 100
    mean_r = statistics.mean(rets)
    median_r = statistics.median(rets)
    sum_r = sum(rets)
    t_stat, p_val = naive_ttest(rets)
    return n, win, mean_r, median_r, sum_r, t_stat, p_val


def main() -> int:
    files = sorted(DATA_DIR.glob("*_tick_*.csv"))
    print(f"標的數：{len(files)}")

    per_stock_days: dict[str, dict] = {}
    for path in files:
        sid = path.name.split("_")[0]
        per_stock_days[sid] = load_stock(path)

    print(f"\n{'x%':>5} {'trail%':>6} {'n_all':>6} {'win%':>6} {'mean%':>8} {'t':>6} {'p':>6}"
          f" | {'n_long':>6} {'L_mean%':>8} {'L_t':>6} | {'n_short':>7} {'S_mean%':>8} {'S_t':>6}")
    rows = []
    for x in THRESHOLDS:
        for trail in TRAILS:
            pooled = []
            for sid, days in per_stock_days.items():
                for t in simulate(days, x, trail):
                    t["stock_id"] = sid
                    pooled.append(t)
            all_rets = [t["ret_pct"] for t in pooled]
            long_rets = [t["ret_pct"] for t in pooled if t["direction"] == "long"]
            short_rets = [t["ret_pct"] for t in pooled if t["direction"] == "short"]

            n, win, mean_r, median_r, sum_r, t_stat, p_val = stats_line(all_rets)
            ln, lwin, lmean, lmed, lsum, lt, lp = stats_line(long_rets)
            sn, swin, smean, smed, ssum, st, sp = stats_line(short_rets)

            rows.append((x, trail, n, win, mean_r, t_stat, p_val, ln, lmean, lt, sn, smean, st))
            print(
                f"{x:>5.1f} {trail:>6.1f} {n:>6} {win:>6.1f} {mean_r:>8.3f} {t_stat:>6.2f} {p_val:>6.3f}"
                f" | {ln:>6} {lmean:>8.3f} {lt:>6.2f} | {sn:>7} {smean:>8.3f} {st:>6.2f}"
            )

    n_combos = len(THRESHOLDS) * len(TRAILS)
    print(f"\n共 {n_combos} 組合 · 純雜訊下 p<0.05 預期約 {n_combos*0.05:.1f} 組會通過")

    MIN_N = 30
    ranked = [r for r in rows if r[2] >= MIN_N]
    ranked.sort(key=lambda r: r[4], reverse=True)
    print(f"\n=== 全部(多+空)平均報酬%最高（n≥{MIN_N}）前5 ===")
    for x, trail, n, win, mean_r, t_stat, p_val, ln, lmean, lt, sn, smean, st in ranked[:5]:
        print(f"  x={x}% trail={trail}%: n={n} win={win:.1f}% mean={mean_r:.3f}% t={t_stat:.2f} p={p_val:.3f}"
              f" (long n={ln} mean={lmean:.3f}% · short n={sn} mean={smean:.3f}%)")

    print(f"\n=== 全部(多+空)平均報酬%最低（n≥{MIN_N}）後5 ===")
    for x, trail, n, win, mean_r, t_stat, p_val, ln, lmean, lt, sn, smean, st in ranked[-5:]:
        print(f"  x={x}% trail={trail}%: n={n} win={win:.1f}% mean={mean_r:.3f}% t={t_stat:.2f} p={p_val:.3f}"
              f" (long n={ln} mean={lmean:.3f}% · short n={sn} mean={smean:.3f}%)")

    # long-only vs short-only best combo, ranked separately
    long_ranked = [r for r in rows if r[7] >= MIN_N]
    long_ranked.sort(key=lambda r: r[8], reverse=True)
    print(f"\n=== 只看多方向 平均報酬%最高（n≥{MIN_N}）前5 ===")
    for x, trail, n, win, mean_r, t_stat, p_val, ln, lmean, lt, sn, smean, st in long_ranked[:5]:
        print(f"  x={x}% trail={trail}%: long n={ln} mean={lmean:.3f}% t={lt:.2f}")

    short_ranked = [r for r in rows if r[10] >= MIN_N]
    short_ranked.sort(key=lambda r: r[11], reverse=True)
    print(f"\n=== 只看空方向 平均報酬%最高（n≥{MIN_N}）前5 ===")
    for x, trail, n, win, mean_r, t_stat, p_val, ln, lmean, lt, sn, smean, st in short_ranked[:5]:
        print(f"  x={x}% trail={trail}%: short n={sn} mean={smean:.3f}% t={st:.2f}")

    best_x, best_trail = ranked[0][0], ranked[0][1]
    print(f"\n=== 逐檔明細 x={best_x}% trail={best_trail}%（全域最佳格，多空合計）===")
    print(f"{'stock':>6} {'n':>4} {'n_long':>6} {'n_short':>7} {'win%':>6} {'mean%':>8} {'sum%':>8}")
    for sid, days in per_stock_days.items():
        trades = simulate(days, best_x, best_trail)
        if not trades:
            print(f"{sid:>6} {0:>4} {0:>6} {0:>7} {'--':>6} {'--':>8} {'--':>8}")
            continue
        rets = [t["ret_pct"] for t in trades]
        nl = sum(1 for t in trades if t["direction"] == "long")
        ns = sum(1 for t in trades if t["direction"] == "short")
        win = sum(1 for r in rets if r > 0) / len(rets) * 100
        print(f"{sid:>6} {len(rets):>4} {nl:>6} {ns:>7} {win:>6.1f} {statistics.mean(rets):>8.3f} {sum(rets):>8.1f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
