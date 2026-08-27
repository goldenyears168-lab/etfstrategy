"""2026-08-13：全新訊號框架 —— 開盤跳空回補（fade the gap）.

跟現有的「盤中突破延續」訊號完全獨立：不掃盤中tick找突破，只用**開盤瞬間**
單一數字 —— 個股期貨當日第一筆tick價 vs 前一交易日收盤價的跳空幅度 —— 決定
今天要不要進場、往哪個方向進（跳空幅度夠大就賭「回補」：跳高→空、跳低→多）。

跟12檔輪動突破策略的關鍵差異，記在這裡避免之後被誤讀成同一種東西：
  - 訊號只在每天開盤那一刻算一次（不是逐tick掃描找突破），12檔互相獨立、
    同一天可能好幾檔同時觸發 —— 不像突破策略是逐tick搶時間，這裡從設計上就
    沒有「單槽位搶佔」的排隊問題，所以**沒有沿用單槽位/搶佔機制**（使用者
    在任務說明裡明確允許：跟輪動框架完全獨立的新框架不強制保留搶佔）。
    這裡採的部位管理是「每天有幾檔跳空過門檻就開幾筆、彼此不排擠」，
    risk-adj 的每日加總方式跟其他研究腳本一致（把同一天所有trade的ret_pct
    加總），純粹是延續既有比較口徑，不代表真的假設資金無限（見下方
    「已知簡化」）。
  - entry_price 用當天第一筆tick的真實成交價（times[0]/prices[0]），不是
    任何理論價 —— 這就是「開盤價」本身，沒有理論值可假；exit_price 全部
    用命中出場條件那一筆的真實tick價 float(p)，不是理論停利/停損價，直接
    照今天早上修好的bug教訓辦（entry/exit都必須是實際能成交的tick價）。
  - prev_close 用「同一檔股票、同一個窗口內、日期排序上緊接在前一個有資料
    的交易日」的最後一筆tick價 —— 每個窗口(IS/OOS/W3/W4)彼此間隔數月，
    故意不跨窗口找前一天，否則會抓到根本不連續的兩個交易日當「前一天」。
    因此每個窗口的第一個交易日沒有window內可用的prev_close、直接跳過。

出場規則三選一（用法說明要求的「固定時間/固定目標價/跟到收盤」都做）：
  - ride_close：不主動出場，跟到收盤最後一筆tick強制平倉。
  - target_X%：目標價＝prev_close + (1-X)*gap（X=1.0代表目標是完全回補到
    prev_close，X=0.3代表只要回補30%幅度就獲利了結），命中即出場。
  - timeout_Ns：進場後固定N秒，不管賺賠強制出場。
  三種出場模式都可以疊加一個保護性停損 stop_loss_pct（跳空方向繼續噴而不是
  回補時停損），跟只做纯粹「跟到收盤」比較會差很多，一起sweep。

已知簡化（跟其他研究腳本一致，這裡不重複造輪子解決）：
  - 沒有建模真實滑價／流動性衝擊，entry用開盤第一筆tick價，早盤流動性通常
    比盤中薄，這比盤中突破策略更可能低估真實成交難度。
  - per-day風險加總用「同一天所有trade的ret_pct直接加總」，隱含各trade等
    金額且不互相排擠資金的簡化假設，跟 momentum_rotation_*_holdout_cv.py
    系列口徑一致，只是這裡的「不排擠」比輪動策略更貼近事實（本來就是各自
    獨立的12個部位，不是搶同一個槽位）。

4折留一窗口交叉驗證（leave-one-window-out）：每次留一個窗口當holdout，
在其餘3個窗口 sweep gap_threshold_pct × exit_spec × stop_loss_pct 找
risk-adj最佳點，套到holdout窗口驗證一次；baseline＝現行live規格的
momentum_breakout_strategy.simulate_portfolio_day（同一套holdout窗口跑一次
當對照，口徑一致：per-day加總ret_pct、risk-adj=日均/日std）。
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import (  # noqa: E402
    UNIVERSE,
    load_day_bars_with_times,
    simulate_portfolio_day as baseline_simulate,
)

TICK_DIR_NAME = "reports/research/expert_pool_futures_tick"
from pathlib import Path  # noqa: E402

TICK_DIR = Path(TICK_DIR_NAME)

WINDOWS = {
    "IS(漲盤)": "2026-07-13_2026-08-11",
    "OOS(拉回)": "2025-10-20_2025-11-24",
    "W3(盤整)": "2025-11-28_2025-12-19",
    "W4": "2026-02-09_2026-03-06",
}

BASE_FIXED = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
                   min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0)

EXIT_SPECS: list[tuple[str, dict]] = [
    ("ride_close", dict(exit_mode="close")),
    ("target_30%", dict(exit_mode="target", fade_target_frac=0.3)),
    ("target_50%", dict(exit_mode="target", fade_target_frac=0.5)),
    ("target_70%", dict(exit_mode="target", fade_target_frac=0.7)),
    ("target_100%", dict(exit_mode="target", fade_target_frac=1.0)),
    ("timeout_60s", dict(exit_mode="timeout", timeout_sec=60.0)),
    ("timeout_300s", dict(exit_mode="timeout", timeout_sec=300.0)),
    ("timeout_900s", dict(exit_mode="timeout", timeout_sec=900.0)),
]
GAP_THRESHOLD_GRID = [0.1, 0.2, 0.3, 0.5, 0.8]
STOP_LOSS_GRID = [None, 1.0]


def load_window(wdate: str) -> tuple[dict, list[str]]:
    all_by_stock: dict[str, dict] = {}
    for sid in UNIVERSE:
        matches = list(TICK_DIR.glob(f"*{sid}_*{wdate}*.csv"))
        if not matches:
            continue
        days: dict = {}
        for p in matches:
            days.update(load_day_bars_with_times(p))
        all_by_stock[sid] = days
    all_days = sorted(set().union(*[set(d.keys()) for d in all_by_stock.values()]))
    return all_by_stock, all_days


def build_prev_close_by_day(all_by_stock: dict) -> dict[str, dict[str, float]]:
    """{day: {sid: prev_trading_day_close}}；每檔股票各自用自己窗口內排序後
    緊接在前的那個交易日收盤價，不跨窗口、不假設所有股票同步缺席同一天。
    """
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for sid, days in all_by_stock.items():
        sorted_days = sorted(days.keys())
        for i in range(1, len(sorted_days)):
            d = sorted_days[i]
            prev_d = sorted_days[i - 1]
            prev_prices = days[prev_d][1]
            if prev_prices.size == 0:
                continue
            out[d][sid] = float(prev_prices[-1])
    return out


def simulate_stock_day_gapfade(
    times: list[str], prices: np.ndarray, volumes: np.ndarray, prev_close: float, *,
    gap_threshold_pct: float, exit_mode: str,
    fade_target_frac: float | None = None, timeout_sec: float | None = None,
    stop_loss_pct: float | None = None,
) -> dict | None:
    if prices.size < 2 or prev_close <= 0:
        return None
    open_price = float(prices[0])
    gap_pct = (open_price - prev_close) / prev_close * 100.0
    if abs(gap_pct) < gap_threshold_pct:
        return None
    direction = "short" if gap_pct > 0 else "long"  # 跳高賭回補->空；跳低賭回補->多
    fill = open_price
    entry_time = times[0]
    entry_dt = datetime.fromisoformat(entry_time)

    target_price = None
    if exit_mode == "target":
        gap_abs = open_price - prev_close
        target_price = prev_close + (1.0 - fade_target_frac) * gap_abs

    stop_price = None
    if stop_loss_pct is not None:
        stop_price = fill * (1 + stop_loss_pct / 100.0) if direction == "short" else fill * (1 - stop_loss_pct / 100.0)

    exit_price = None
    exit_time = None
    reason = None
    for i in range(1, len(times)):
        p = float(prices[i])
        hit_target = False
        if target_price is not None:
            hit_target = (p <= target_price) if direction == "short" else (p >= target_price)
        hit_stop = False
        if stop_price is not None:
            hit_stop = (p >= stop_price) if direction == "short" else (p <= stop_price)
        timed_out = False
        if exit_mode == "timeout":
            elapsed = (datetime.fromisoformat(times[i]) - entry_dt).total_seconds()
            timed_out = elapsed >= timeout_sec
        if hit_target or hit_stop or timed_out:
            exit_price = p
            exit_time = times[i]
            reason = "target" if hit_target else ("stop_loss" if hit_stop else "timeout")
            break

    if exit_price is None:
        exit_price = float(prices[-1])
        exit_time = times[-1]
        reason = "day_end_forced"

    ret_pct = (fill - exit_price) / fill * 100.0 if direction == "short" else (exit_price - fill) / fill * 100.0
    return {
        "direction": direction, "entry": fill, "entry_time": entry_time,
        "exit": exit_price, "exit_time": exit_time, "ret_pct": ret_pct, "reason": reason,
        "gap_pct": gap_pct,
    }


def simulate_portfolio_day_gapfade(
    stock_day_data: dict[str, tuple[list[str], np.ndarray, np.ndarray]],
    prev_close_map: dict[str, float],
    **kwargs,
) -> list[dict]:
    trades = []
    for sid, (times, prices, volumes) in stock_day_data.items():
        if sid not in prev_close_map:
            continue
        res = simulate_stock_day_gapfade(times, prices, volumes, prev_close_map[sid], **kwargs)
        if res is not None:
            trades.append({**res, "sid": sid})
    return trades


def _run_gapfade(windows_subset: dict, **kwargs) -> dict:
    all_trades = []
    per_day: dict[str, float] = {}
    for _wname, (all_by_stock, all_days) in windows_subset.items():
        prev_close_by_day = build_prev_close_by_day(all_by_stock)
        for d in all_days:
            if d not in prev_close_by_day:
                continue
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            trades = simulate_portfolio_day_gapfade(day_data, prev_close_by_day[d], **kwargs)
            all_trades.extend(trades)
            per_day[d] = per_day.get(d, 0.0) + sum(t["ret_pct"] for t in trades)
    return _metrics(all_trades, per_day)


def _run_baseline(windows_subset: dict, **kwargs) -> dict:
    all_trades = []
    per_day: dict[str, float] = {}
    for _wname, (all_by_stock, all_days) in windows_subset.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            trades = baseline_simulate(day_data, **kwargs)
            all_trades.extend(trades)
            per_day[d] = per_day.get(d, 0.0) + sum(t["ret_pct"] for t in trades)
    return _metrics(all_trades, per_day)


def _metrics(all_trades: list[dict], per_day: dict[str, float]) -> dict:
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    day_rets = np.array(list(per_day.values())) if per_day else np.array([])
    if len(rets) < 10 or len(day_rets) == 0 or day_rets.std() == 0:
        return {"n": len(rets), "risk_adj": float("-inf"), "breakeven_bps": 0.0, "win_rate": 0.0,
                "day_mean": 0.0, "day_std": 0.0}
    breakeven = (rets.sum() / len(rets)) * 100
    win = float(np.mean(rets > 0) * 100)
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    return {"n": len(rets), "risk_adj": day_mean / day_std, "breakeven_bps": breakeven,
            "win_rate": win, "day_mean": day_mean, "day_std": day_std}


def _gap_diagnostic(all_windows: dict) -> None:
    """先看原始跳空統計（不套策略規則）：跳空後到收盤，價格是回補還是延續？"""
    print("\n### 跳空原始統計（診斷用，不是策略回測）###")
    for wname, (all_by_stock, _all_days) in all_windows.items():
        prev_close_by_day = build_prev_close_by_day(all_by_stock)
        gaps, fade_frac_at_close = [], []
        for sid, days in all_by_stock.items():
            for d, (times, prices, volumes) in days.items():
                if d not in prev_close_by_day or sid not in prev_close_by_day[d]:
                    continue
                prev_close = prev_close_by_day[d][sid]
                if prices.size < 2 or prev_close <= 0:
                    continue
                open_price = float(prices[0])
                close_price = float(prices[-1])
                gap_pct = (open_price - prev_close) / prev_close * 100.0
                if abs(gap_pct) < 0.05:
                    continue
                gaps.append(gap_pct)
                # 回補比例：正=有回補、負=延續(跳空方向繼續走)、以gap本身正規化
                move_from_open = close_price - open_price
                fade_component = -np.sign(gap_pct) * move_from_open
                fade_frac = fade_component / abs(open_price - prev_close)
                fade_frac_at_close.append(fade_frac)
        if gaps:
            gaps_a = np.array(gaps)
            fade_a = np.array(fade_frac_at_close)
            print(f"  {wname}: n_gap(≥0.05%)={len(gaps)} mean|gap|={np.mean(np.abs(gaps_a)):.3f}% "
                  f"收盤前平均回補比例={np.mean(fade_a):+.2f} (正=有回補,負=延續) "
                  f"回補比例中位數={np.median(fade_a):+.2f}")


def main() -> None:
    print("載入4窗口資料...")
    all_windows = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    wnames = list(all_windows.keys())

    _gap_diagnostic(all_windows)

    n_combos = len(GAP_THRESHOLD_GRID) * len(EXIT_SPECS) * len(STOP_LOSS_GRID)
    print(f"\nsweep grid: gap_threshold={GAP_THRESHOLD_GRID} x exit_spec({len(EXIT_SPECS)}) "
          f"x stop_loss={STOP_LOSS_GRID} = {n_combos}組")
    print("=" * 100)

    fold_results = []
    for holdout_name in wnames:
        train_windows = {k: v for k, v in all_windows.items() if k != holdout_name}
        holdout_windows = {holdout_name: all_windows[holdout_name]}
        print(f"\n### Fold: holdout={holdout_name} · train={list(train_windows.keys())} ###")

        best = None
        for gt in GAP_THRESHOLD_GRID:
            for exit_name, exit_kwargs in EXIT_SPECS:
                for sl in STOP_LOSS_GRID:
                    m = _run_gapfade(train_windows, gap_threshold_pct=gt, stop_loss_pct=sl, **exit_kwargs)
                    if best is None or m["risk_adj"] > best[3]["risk_adj"]:
                        best = (gt, exit_name, exit_kwargs, m, sl)
        gt_best, exit_name_best, exit_kwargs_best, train_m, sl_best = best
        print(f"  train最佳點: gap_threshold={gt_best}% exit={exit_name_best} stop_loss={sl_best} "
              f"(train risk-adj={train_m['risk_adj']:.3f} 損平={train_m['breakeven_bps']:.1f}bps "
              f"勝率={train_m['win_rate']:.1f}% n={train_m['n']})")

        holdout_new = _run_gapfade(holdout_windows, gap_threshold_pct=gt_best, stop_loss_pct=sl_best, **exit_kwargs_best)
        holdout_base = _run_baseline(holdout_windows, **BASE_FIXED)
        print(f"  >>> HOLDOUT({holdout_name}) gap_fade : n={holdout_new['n']:4d} "
              f"勝率={holdout_new['win_rate']:5.1f}% 損平={holdout_new['breakeven_bps']:6.1f}bps "
              f"risk-adj={holdout_new['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) baseline : n={holdout_base['n']:4d} "
              f"勝率={holdout_base['win_rate']:5.1f}% 損平={holdout_base['breakeven_bps']:6.1f}bps "
              f"risk-adj={holdout_base['risk_adj']:+.3f}")
        fold_results.append({
            "holdout": holdout_name, "gt": gt_best, "exit": exit_name_best, "sl": sl_best,
            "new": holdout_new, "base": holdout_base,
        })

    print("\n" + "=" * 100)
    print("=== 4折總結：開盤跳空回補在「沒看過」的窗口上是否穩定優於baseline(現行live突破輪動)？ ===")
    n_wins = 0
    for r in fold_results:
        beats = r["new"]["risk_adj"] > r["base"]["risk_adj"]
        n_wins += int(beats)
        print(f"  {r['holdout']:12s} (gap≥{r['gt']}%/{r['exit']}/sl={r['sl']}): "
              f"new risk-adj={r['new']['risk_adj']:+.3f} 損平={r['new']['breakeven_bps']:5.1f}bps  "
              f"vs baseline risk-adj={r['base']['risk_adj']:+.3f} 損平={r['base']['breakeven_bps']:5.1f}bps  "
              f"{'贏' if beats else '輸'}")
    print(f"\n  4折中有{n_wins}折開盤跳空回補在holdout上risk-adj優於baseline")


if __name__ == "__main__":
    main()
