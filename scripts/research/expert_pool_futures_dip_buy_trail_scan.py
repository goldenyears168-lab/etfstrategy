#!/usr/bin/env python3
"""分點專家池個股期貨「隔日限價買跌 + 移動停利」掃描（exploratory · 非採納）.

策略定義（用 tick 資料誠實模擬，避免 research-integrity-checklist 的常見坑）：
  - 進場：前一交易日（同合約，跨月轉倉日 skip）收盤價 × (1 - dip%) 掛限價買單；
    當日 tick 若曾觸及該價，視為以「限價本身」成交（不取「當根更有利的實際成交價」，
    避免 BUG-5 fill clamp）。
  - 出場：進場後追蹤高點，跌破 高點 × (1 - trail%) 視為停利觸發，成交價＝觸發價本身
    （不取更有利價）；若當日結束前都沒觸發，強制在當日最後一筆 tick 平倉（day-trade，
    因個股期貨無夜盤、且無法跨轉倉日延續同一合約的 tick 序列 → BUG-1 session-end
    accounting gap 對策：明確 force-close，計入交易列表，不靜默丟棄）。
  - 樣本：22 個交易日、30 檔個股期貨的逐筆成交（見
    scripts/research/fetch_expert_pool_futures_tick_recent_month.py 的下載結果）。

已知限制（誠實揭露，不要在下游引用時漏掉）：
  - 樣本極短（22 天），任何顯著性檢定的檢定力都很弱；本腳本只做 naive t-test 並明確
    標示「不做過度推論」，不宣稱 HAC 顯著性。
  - 不含手續費／期交稅／滑價／保證金成本。
  - Grid search 48 組合（7 dip × ~5 trail）跨 30 檔篩最佳值，屬於多重比較，純雜訊下也會
    有幾組「看起來」不錯——報告時附上這個提醒，不要把單一最佳格當結論。
  - 轉倉日（近月合約換月）當天因缺乏同合約前收盤價，直接 skip 該日進場評估。

用法：
  PYTHONPATH=src .venv/bin/python \
    scripts/research/expert_pool_futures_dip_buy_trail_scan.py
"""

from __future__ import annotations

import csv
import glob
import math
import statistics
from collections import defaultdict
from pathlib import Path

import numpy as np

DATA_DIR = Path(__file__).resolve().parents[2] / "reports/research/expert_pool_futures_tick"
DIPS = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
TRAILS = [1.0, 1.5, 2.0, 3.0, 5.0, 8.0, 10.0, 15.0, 20.0]


def load_stock(path: Path) -> dict[str, tuple[np.ndarray, str]]:
    day_rows: dict[str, list[tuple[str, float, str]]] = defaultdict(list)
    with path.open() as f:
        r = csv.DictReader(f)
        for row in r:
            d = row["date"][:10]
            day_rows[d].append((row["date"], float(row["price"]), row["contract_date"]))
    out: dict[str, tuple[np.ndarray, str]] = {}
    for d, rows in day_rows.items():
        rows.sort(key=lambda x: x[0])
        prices = np.array([p for _, p, _ in rows], dtype=float)
        out[d] = (prices, rows[0][2])
    return out


def simulate(days: dict[str, tuple[np.ndarray, str]], dip_pct: float, trail_pct: float) -> list[dict]:
    trades: list[dict] = []
    dates = sorted(days.keys())
    prev_close: float | None = None
    prev_contract: str | None = None
    skipped_rollover = 0
    for d in dates:
        prices, contract = days[d]
        if prices.size == 0:
            continue
        if prev_close is not None:
            if contract != prev_contract:
                skipped_rollover += 1
            else:
                entry_price = prev_close * (1 - dip_pct / 100.0)
                touch = np.where(prices <= entry_price)[0]
                if touch.size > 0:
                    entry_idx = int(touch[0])
                    fill = entry_price
                    remainder = prices[entry_idx + 1 :]
                    if remainder.size == 0:
                        exit_price = fill
                        reason = "no_ticks_after_entry"
                    else:
                        running_peak = np.maximum.accumulate(
                            np.concatenate(([fill], remainder))
                        )[1:]
                        stops = running_peak * (1 - trail_pct / 100.0)
                        breach = np.where(remainder <= stops)[0]
                        if breach.size > 0:
                            exit_price = float(stops[int(breach[0])])
                            reason = "trail_stop"
                        else:
                            exit_price = float(remainder[-1])
                            reason = "day_end_forced"
                    ret_pct = (exit_price - fill) / fill * 100.0
                    trades.append(
                        {"date": d, "entry": fill, "exit": exit_price, "ret_pct": ret_pct, "reason": reason}
                    )
        prev_close = float(prices[-1])
        prev_contract = contract
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
    # crude two-sided p via normal approx (n too small for real t-table precision claims)
    from math import erf

    p = 2 * (1 - 0.5 * (1 + erf(abs(t) / math.sqrt(2))))
    return t, p


def main() -> int:
    files = sorted(DATA_DIR.glob("*_tick_*.csv"))
    print(f"標的數：{len(files)}")

    per_stock_days: dict[str, dict] = {}
    for path in files:
        sid = path.name.split("_")[0]
        per_stock_days[sid] = load_stock(path)

    grid_results: dict[tuple[float, float], list[dict]] = {}
    for dip in DIPS:
        for trail in TRAILS:
            pooled: list[dict] = []
            for sid, days in per_stock_days.items():
                trades = simulate(days, dip, trail)
                for t in trades:
                    t["stock_id"] = sid
                pooled.extend(trades)
            grid_results[(dip, trail)] = pooled

    print(f"\n{'dip%':>5} {'trail%':>6} {'n_trades':>8} {'win%':>6} {'mean_ret%':>10} {'median%':>9} {'sum%':>8} {'t':>6} {'p':>6}")
    rows = []
    for (dip, trail), pooled in grid_results.items():
        rets = [t["ret_pct"] for t in pooled]
        n = len(rets)
        if n == 0:
            rows.append((dip, trail, 0, float("nan"), float("nan"), float("nan"), 0.0, float("nan"), float("nan")))
            continue
        win = sum(1 for r in rets if r > 0) / n * 100
        mean_r = statistics.mean(rets)
        median_r = statistics.median(rets)
        sum_r = sum(rets)
        t_stat, p_val = naive_ttest(rets)
        rows.append((dip, trail, n, win, mean_r, median_r, sum_r, t_stat, p_val))

    for dip, trail, n, win, mean_r, median_r, sum_r, t_stat, p_val in rows:
        print(
            f"{dip:>5.1f} {trail:>6.1f} {n:>8} {win:>6.1f} {mean_r:>10.3f} {median_r:>9.3f} "
            f"{sum_r:>8.1f} {t_stat:>6.2f} {p_val:>6.3f}"
        )

    # multiple-comparison base rate reminder
    n_combos = len(DIPS) * len(TRAILS)
    print(f"\n共 {n_combos} 組合（{len(DIPS)} dip × {len(TRAILS)} trail）· 純雜訊下 p<0.05 預期約 {n_combos*0.05:.1f} 組會通過")

    # best combos by mean return (with min trade count filter to avoid tiny-n noise)
    MIN_N = 30
    ranked = [r for r in rows if r[2] >= MIN_N]
    ranked.sort(key=lambda r: r[4], reverse=True)
    print(f"\n=== 平均報酬%最高（n≥{MIN_N}）前5 ===")
    for dip, trail, n, win, mean_r, median_r, sum_r, t_stat, p_val in ranked[:5]:
        print(f"  dip={dip}% trail={trail}%: n={n} win={win:.1f}% mean={mean_r:.3f}% t={t_stat:.2f} p={p_val:.3f}")

    print(f"\n=== 平均報酬%最低（n≥{MIN_N}）後5（供對照，不是要拿來做空）===")
    for dip, trail, n, win, mean_r, median_r, sum_r, t_stat, p_val in ranked[-5:]:
        print(f"  dip={dip}% trail={trail}%: n={n} win={win:.1f}% mean={mean_r:.3f}% t={t_stat:.2f} p={p_val:.3f}")

    # per-stock detail at user-suggested dip=3%, a couple of trail choices
    print("\n=== dip=3% 逐檔明細（trail=2% 與 trail=3%）===")
    for trail in (2.0, 3.0):
        print(f"\n-- trail={trail}% --")
        print(f"{'stock':>6} {'n':>4} {'win%':>6} {'mean%':>8} {'sum%':>8}")
        for sid, days in per_stock_days.items():
            trades = simulate(days, 3.0, trail)
            if not trades:
                print(f"{sid:>6} {0:>4} {'--':>6} {'--':>8} {'--':>8}")
                continue
            rets = [t["ret_pct"] for t in trades]
            win = sum(1 for r in rets if r > 0) / len(rets) * 100
            print(f"{sid:>6} {len(rets):>4} {win:>6.1f} {statistics.mean(rets):>8.3f} {sum(rets):>8.1f}")

    # sanity: distribution of intraday max drawdown from prior close (how often is each dip level even touched)
    print("\n=== 每檔『當日觸及前收盤下跌 X%』天數比例（sanity check：dip門檻合不合理）===")
    print(f"{'stock':>6}" + "".join(f"{d:>7.1f}%" for d in DIPS))
    for sid, days in per_stock_days.items():
        dates = sorted(days.keys())
        prev_close = None
        prev_contract = None
        touch_counts = {d: 0 for d in DIPS}
        n_days_eval = 0
        for d in dates:
            prices, contract = days[d]
            if prices.size == 0:
                continue
            if prev_close is not None and contract == prev_contract:
                n_days_eval += 1
                low = float(prices.min())
                drop_pct = (prev_close - low) / prev_close * 100
                for dip in DIPS:
                    if drop_pct >= dip:
                        touch_counts[dip] += 1
            prev_close = float(prices[-1])
            prev_contract = contract
        if n_days_eval == 0:
            continue
        line = f"{sid:>6}"
        for d in DIPS:
            pct = touch_counts[d] / n_days_eval * 100
            line += f"{pct:>7.1f}%" if False else f"{pct:>8.1f}"
        print(line + f"   (n_days={n_days_eval})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
